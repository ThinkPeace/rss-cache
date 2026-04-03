#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from pathlib import Path
from typing import Iterable


DEFAULT_USER_AGENT = "rss-cache/1.0 (+https://github.com/ThinkPeace/rss-cache)"
DEFAULT_TIMEOUT_SECONDS = 25
MAX_ITEMS_PER_FEED = 30
MAX_COMBINED_ITEMS = 250
SUMMARY_PREVIEW_LIMIT = 360


@dataclass
class FeedSpec:
    slug: str
    title: str
    xml_url: str
    html_url: str


def main() -> int:
    args = parse_args()
    site_url = args.site_url.rstrip("/")
    output_dir = Path(args.output).resolve()
    feeds_dir = output_dir / "feeds"
    feeds_dir.mkdir(parents=True, exist_ok=True)

    generated_at = utc_now_iso()
    feed_specs = parse_opml(Path(args.opml), max_feeds=args.max_feeds)

    combined_items: list[dict[str, object]] = []
    feed_index: list[dict[str, object]] = []

    for spec in feed_specs:
        record, items = fetch_and_parse_feed(
            spec=spec,
            output_dir=feeds_dir,
            site_url=site_url,
            timeout_seconds=args.timeout,
            user_agent=args.user_agent,
        )
        feed_index.append(record)
        combined_items.extend(items)

    combined_items.sort(key=combined_sort_key, reverse=True)
    combined_items = combined_items[:MAX_COMBINED_ITEMS]

    stats = build_stats(feed_specs, feed_index, combined_items, generated_at)
    write_json(feeds_dir / "index.json", {"generated_at": generated_at, "stats": stats, "feeds": feed_index})
    write_json(
        feeds_dir / "combined.json",
        {"generated_at": generated_at, "site_url": site_url, "stats": stats, "items": combined_items},
    )
    write_text(feeds_dir / "combined.xml", build_combined_rss(site_url=site_url, generated_at=generated_at, items=combined_items))
    write_text(output_dir / "index.html", build_home_page(site_url=site_url, generated_at=generated_at, stats=stats, feeds=feed_index, items=combined_items))

    print(f"Generated {len(feed_index)} feeds and {len(combined_items)} combined items into {output_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a static RSS mirror site from OPML.")
    parser.add_argument("--opml", default="feeds.opml", help="Path to the source OPML file.")
    parser.add_argument("--output", default="dist", help="Directory where the static site is written.")
    parser.add_argument("--site-url", required=True, help="Public base URL for the generated site.")
    parser.add_argument("--max-feeds", type=int, help="Optional limit for local smoke tests.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-feed HTTP timeout in seconds.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent sent to feed origins.")
    return parser.parse_args()


def parse_opml(path: Path, max_feeds: int | None = None) -> list[FeedSpec]:
    tree = ET.parse(path)
    root = tree.getroot()
    seen: dict[str, int] = {}
    specs: list[FeedSpec] = []
    for outline in root.findall(".//outline[@xmlUrl]"):
        title = (outline.get("title") or outline.get("text") or outline.get("xmlUrl") or "feed").strip()
        xml_url = (outline.get("xmlUrl") or "").strip()
        if not xml_url:
            continue
        html_url = (outline.get("htmlUrl") or "").strip()
        slug = make_unique_slug(slugify(title), seen)
        specs.append(FeedSpec(slug=slug, title=title, xml_url=xml_url, html_url=html_url))
        if max_feeds and len(specs) >= max_feeds:
            break
    return specs


def fetch_and_parse_feed(
    spec: FeedSpec,
    output_dir: Path,
    site_url: str,
    timeout_seconds: int,
    user_agent: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    mirror_url = f"{site_url}/feeds/{spec.slug}.xml"
    record: dict[str, object] = {
        "slug": spec.slug,
        "title": spec.title,
        "source_xml_url": spec.xml_url,
        "source_html_url": spec.html_url,
        "mirror_xml_url": mirror_url,
        "status": "pending",
    }

    try:
        payload, response_info = fetch_bytes(spec.xml_url, timeout_seconds=timeout_seconds, user_agent=user_agent)
        write_bytes(output_dir / f"{spec.slug}.xml", payload)
        record.update(response_info)
        feed_meta, items = parse_feed_payload(payload, spec)
        record.update(feed_meta)
        record["status"] = "ok"
        record["item_count"] = len(items)
        record["latest_published_at"] = first_published_at(items)
        return record, items
    except Exception as exc:  # noqa: BLE001
        record["status"] = "error"
        record["error"] = str(exc)
        record["item_count"] = 0
        return record, []


def fetch_bytes(url: str, timeout_seconds: int, user_agent: str) -> tuple[bytes, dict[str, object]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read()
            return payload, {
                "http_status": getattr(response, "status", None),
                "content_type": response.headers.get_content_type(),
                "fetched_url": response.geturl(),
                "content_length": len(payload),
            }
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error: {exc.reason}") from exc


def parse_feed_payload(payload: bytes, spec: FeedSpec) -> tuple[dict[str, object], list[dict[str, object]]]:
    root = ET.fromstring(payload)
    root_name = local_name(root.tag)
    if root_name == "feed":
        return parse_atom_feed(root, spec)
    if root_name in {"rss", "RDF"}:
        return parse_rss_feed(root, spec)
    raise RuntimeError(f"Unsupported feed root: {root_name}")


def parse_atom_feed(root: ET.Element, spec: FeedSpec) -> tuple[dict[str, object], list[dict[str, object]]]:
    feed_title = child_text(root, "title") or spec.title
    feed_home = atom_link(root) or spec.html_url
    feed_description = child_text(root, "subtitle")
    items: list[dict[str, object]] = []
    for entry in iter_children(root, "entry"):
        title = child_text(entry, "title") or "(untitled)"
        link = atom_link(entry) or feed_home or spec.html_url
        guid = child_text(entry, "id") or link or title
        summary = clean_summary(child_text(entry, "summary") or child_text(entry, "content"))
        published = normalize_timestamp(child_text(entry, "published") or child_text(entry, "updated"))
        items.append(
            {
                "source_slug": spec.slug,
                "source_title": feed_title,
                "title": title,
                "link": link,
                "guid": guid,
                "published_at": published,
                "summary": summary,
            }
        )
        if len(items) >= MAX_ITEMS_PER_FEED:
            break
    return {
        "feed_title": feed_title,
        "feed_home_url": feed_home,
        "feed_description": clean_summary(feed_description),
        "feed_format": "atom",
    }, items


def parse_rss_feed(root: ET.Element, spec: FeedSpec) -> tuple[dict[str, object], list[dict[str, object]]]:
    channel = first_child(root, "channel")
    if channel is None:
        channel = root
    feed_title = child_text(channel, "title") or spec.title
    feed_home = child_text(channel, "link") or spec.html_url
    feed_description = child_text(channel, "description")

    item_nodes = list(iter_children(channel, "item"))
    if not item_nodes and local_name(root.tag) == "RDF":
        item_nodes = list(iter_children(root, "item"))

    items: list[dict[str, object]] = []
    for item in item_nodes:
        title = child_text(item, "title") or "(untitled)"
        link = child_text(item, "link") or feed_home or spec.html_url
        guid = child_text(item, "guid") or link or title
        summary = clean_summary(
            child_text(item, "description")
            or child_text(item, "encoded")
            or child_text(item, "summary")
        )
        published = normalize_timestamp(
            child_text(item, "pubDate")
            or child_text(item, "date")
            or child_text(item, "published")
            or child_text(item, "updated")
        )
        items.append(
            {
                "source_slug": spec.slug,
                "source_title": feed_title,
                "title": title,
                "link": link,
                "guid": guid,
                "published_at": published,
                "summary": summary,
            }
        )
        if len(items) >= MAX_ITEMS_PER_FEED:
            break
    return {
        "feed_title": feed_title,
        "feed_home_url": feed_home,
        "feed_description": clean_summary(feed_description),
        "feed_format": "rss",
    }, items


def build_stats(
    specs: list[FeedSpec],
    feed_index: list[dict[str, object]],
    combined_items: list[dict[str, object]],
    generated_at: str,
) -> dict[str, object]:
    ok_count = sum(1 for feed in feed_index if feed.get("status") == "ok")
    error_count = len(feed_index) - ok_count
    return {
        "generated_at": generated_at,
        "configured_feed_count": len(specs),
        "successful_feed_count": ok_count,
        "failed_feed_count": error_count,
        "combined_item_count": len(combined_items),
    }


def build_combined_rss(site_url: str, generated_at: str, items: list[dict[str, object]]) -> str:
    pub_date = format_http_date(generated_at) or format_datetime(datetime.now(timezone.utc))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<rss version=\"2.0\">",
        "<channel>",
        "<title>rss-cache combined feed</title>",
        f"<link>{escape(site_url)}</link>",
        "<description>Combined feed generated from the OPML source list.</description>",
        "<language>en</language>",
        f"<lastBuildDate>{escape(pub_date)}</lastBuildDate>",
        f"<generator>{escape(DEFAULT_USER_AGENT)}</generator>",
    ]
    for item in items:
        description = clean_summary(item.get("summary") or "")
        if description:
            description = f"[{item.get('source_title')}] {description}"
        pub = format_http_date(item.get("published_at"))
        lines.extend(
            [
                "<item>",
                f"<title>{escape(str(item.get('title') or '(untitled)'))}</title>",
                f"<link>{escape(str(item.get('link') or site_url))}</link>",
                f"<guid>{escape(str(item.get('guid') or item.get('link') or item.get('title')))}</guid>",
                f"<description>{escape(description)}</description>",
                f"<category>{escape(str(item.get('source_title') or 'unknown'))}</category>",
            ]
        )
        if pub:
            lines.append(f"<pubDate>{escape(pub)}</pubDate>")
        lines.append("</item>")
    lines.extend(["</channel>", "</rss>"])
    return "\n".join(lines) + "\n"


def build_home_page(
    site_url: str,
    generated_at: str,
    stats: dict[str, object],
    feeds: list[dict[str, object]],
    items: list[dict[str, object]],
) -> str:
    feed_rows = []
    for feed in feeds:
        status = str(feed.get("status"))
        row_class = "ok" if status == "ok" else "error"
        mirror_link = feed.get("mirror_xml_url") or "#"
        feed_rows.append(
            "<tr>"
            f"<td>{escape(str(feed.get('title') or ''))}</td>"
            f"<td class=\"{row_class}\">{escape(status)}</td>"
            f"<td>{escape(str(feed.get('item_count') or 0))}</td>"
            f"<td><a href=\"{escape(str(feed.get('source_xml_url') or '#'))}\">source</a></td>"
            f"<td><a href=\"{escape(str(mirror_link))}\">mirror</a></td>"
            f"<td>{escape(str(feed.get('error') or ''))}</td>"
            "</tr>"
        )
    latest_items = []
    for item in items[:40]:
        source = escape(str(item.get("source_title") or "unknown"))
        title = escape(str(item.get("title") or "(untitled)"))
        link = escape(str(item.get("link") or site_url))
        published = escape(str(item.get("published_at") or ""))
        latest_items.append(f"<li><a href=\"{link}\">{title}</a> <small>{source} {published}</small></li>")

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>rss-cache</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f4f1ea;
        --card: #fffdf8;
        --text: #1f2933;
        --muted: #52606d;
        --border: #d9cbb8;
        --accent: #b05a33;
        --accent-soft: #efe0d7;
        --ok: #1f7a4c;
        --error: #b42318;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
        background:
          radial-gradient(circle at top left, rgba(176, 90, 51, 0.12), transparent 34rem),
          linear-gradient(180deg, #fbf8f2 0%, var(--bg) 100%);
        color: var(--text);
      }}
      main {{
        max-width: 1100px;
        margin: 0 auto;
        padding: 3rem 1.25rem 4rem;
      }}
      h1 {{
        margin: 0 0 0.75rem;
        font-size: clamp(2.5rem, 6vw, 4.5rem);
        line-height: 0.95;
      }}
      p, li, td, th {{
        font-size: 0.98rem;
      }}
      .lede {{
        max-width: 48rem;
        color: var(--muted);
        margin-bottom: 1.5rem;
      }}
      .links {{
        display: flex;
        flex-wrap: wrap;
        gap: 0.75rem;
        margin-bottom: 2rem;
      }}
      .links a {{
        display: inline-flex;
        align-items: center;
        padding: 0.7rem 1rem;
        border-radius: 999px;
        background: var(--card);
        color: var(--text);
        border: 1px solid var(--border);
        text-decoration: none;
      }}
      .panel {{
        background: rgba(255, 253, 248, 0.88);
        border: 1px solid var(--border);
        border-radius: 20px;
        padding: 1.1rem 1.2rem;
        backdrop-filter: blur(6px);
      }}
      .stats {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
        gap: 1rem;
        margin: 1.5rem 0 2rem;
      }}
      .stats strong {{
        display: block;
        font-size: 1.7rem;
        margin-bottom: 0.25rem;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        background: var(--card);
        border-radius: 18px;
        overflow: hidden;
      }}
      th, td {{
        text-align: left;
        padding: 0.75rem 0.8rem;
        border-bottom: 1px solid #efe7da;
        vertical-align: top;
      }}
      th {{
        font-size: 0.82rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--muted);
      }}
      .ok {{ color: var(--ok); }}
      .error {{ color: var(--error); }}
      ul {{
        padding-left: 1.25rem;
      }}
      a {{
        color: var(--accent);
      }}
      @media (max-width: 720px) {{
        table {{
          display: block;
          overflow-x: auto;
        }}
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>rss-cache</h1>
      <p class="lede">
        GitHub Actions fetches the OPML source list on a schedule and publishes mirrored feed XML plus combined JSON and RSS artifacts to GitHub Pages.
      </p>
      <div class="links">
        <a href="{escape(site_url)}/feeds/index.json">feeds/index.json</a>
        <a href="{escape(site_url)}/feeds/combined.json">feeds/combined.json</a>
        <a href="{escape(site_url)}/feeds/combined.xml">feeds/combined.xml</a>
      </div>
      <section class="stats">
        <div class="panel"><strong>{escape(str(stats.get("configured_feed_count")))}</strong>Configured feeds</div>
        <div class="panel"><strong>{escape(str(stats.get("successful_feed_count")))}</strong>Successful mirrors</div>
        <div class="panel"><strong>{escape(str(stats.get("failed_feed_count")))}</strong>Failed mirrors</div>
        <div class="panel"><strong>{escape(str(stats.get("combined_item_count")))}</strong>Combined items</div>
      </section>
      <section class="panel" style="margin-bottom: 1.5rem;">
        <strong>Generated at</strong>
        <div>{escape(generated_at)}</div>
      </section>
      <section style="margin-bottom: 2rem;">
        <h2>Latest Items</h2>
        <div class="panel">
          <ul>
            {"".join(latest_items)}
          </ul>
        </div>
      </section>
      <section>
        <h2>Feed Mirrors</h2>
        <table>
          <thead>
            <tr>
              <th>Feed</th>
              <th>Status</th>
              <th>Items</th>
              <th>Source</th>
              <th>Mirror</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {"".join(feed_rows)}
          </tbody>
        </table>
      </section>
    </main>
  </body>
</html>
"""


def first_child(parent: ET.Element, *names: str) -> ET.Element | None:
    for child in list(parent):
        if local_name(child.tag) in names:
            return child
    return None


def iter_children(parent: ET.Element, *names: str) -> Iterable[ET.Element]:
    names_set = set(names)
    for child in list(parent):
        if local_name(child.tag) in names_set:
            yield child


def child_text(parent: ET.Element, *names: str) -> str:
    child = first_child(parent, *names)
    if child is None:
        return ""
    value = "".join(child.itertext()).strip()
    return normalize_space(value)


def atom_link(parent: ET.Element) -> str:
    fallback = ""
    for link in iter_children(parent, "link"):
        href = (link.attrib.get("href") or "").strip()
        rel = (link.attrib.get("rel") or "alternate").strip()
        if href and rel in {"alternate", ""}:
            return href
        if href and not fallback:
            fallback = href
        text_value = normalize_space("".join(link.itertext()).strip())
        if text_value and not fallback:
            fallback = text_value
    return fallback


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_summary(value: str) -> str:
    collapsed = normalize_space(re.sub(r"<[^>]+>", " ", value or ""))
    if len(collapsed) <= SUMMARY_PREVIEW_LIMIT:
        return collapsed
    return collapsed[: SUMMARY_PREVIEW_LIMIT - 1].rstrip() + "…"


def normalize_timestamp(raw_value: str) -> str | None:
    value = normalize_space(raw_value)
    if not value:
        return None
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def combined_sort_key(item: dict[str, object]) -> tuple[int, str]:
    published_at = str(item.get("published_at") or "")
    if not published_at:
        return (0, "")
    return (1, published_at)


def first_published_at(items: list[dict[str, object]]) -> str | None:
    values = [str(item.get("published_at") or "") for item in items if item.get("published_at")]
    if not values:
        return None
    return max(values)


def format_http_date(value: object) -> str | None:
    if not value:
        return None
    text = str(value)
    normalized = normalize_timestamp(text)
    if not normalized:
        return None
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    return format_datetime(parsed)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "feed"


def make_unique_slug(base: str, seen: dict[str, int]) -> str:
    counter = seen.get(base, 0)
    seen[base] = counter + 1
    if counter == 0:
        return base
    return f"{base}-{counter + 1}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
