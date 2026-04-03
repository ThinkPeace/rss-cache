#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape, unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

try:
    import trafilatura
except ImportError:  # pragma: no cover - optional dependency in local dev
    trafilatura = None


DEFAULT_USER_AGENT = "rss-cache/1.1 (+https://github.com/ThinkPeace/rss-cache)"
DEFAULT_FEED_TIMEOUT_SECONDS = 12
DEFAULT_ARTICLE_TIMEOUT_SECONDS = 15
DEFAULT_FEED_WORKERS = 12
DEFAULT_ARTICLE_WORKERS = 16
MAX_ITEMS_PER_FEED = 30
MAX_COMBINED_ITEMS = 250
SUMMARY_PREVIEW_LIMIT = 360
MIN_FULLTEXT_CHARS = 900
MIN_ARTICLE_EXTRACT_CHARS = 300


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
    archive_dir = Path(args.archive_dir).resolve()
    feeds_dir = output_dir / "feeds"

    if output_dir.exists():
        shutil.rmtree(output_dir)
    feeds_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    generated_at = utc_now_iso()
    feed_specs = parse_opml(Path(args.opml), max_feeds=args.max_feeds)
    existing_articles = load_existing_articles(archive_dir)

    combined_items: list[dict[str, object]] = []
    feed_index = fetch_all_feeds(
        feed_specs=feed_specs,
        combined_items=combined_items,
        feeds_dir=feeds_dir,
        site_url=site_url,
        timeout_seconds=args.feed_timeout,
        workers=args.feed_workers,
        user_agent=args.user_agent,
    )

    combined_items.sort(key=combined_sort_key, reverse=True)
    archived_articles = archive_articles(
        feed_items=combined_items,
        existing_articles=existing_articles,
        archive_dir=archive_dir,
        site_url=site_url,
        generated_at=generated_at,
        article_timeout_seconds=args.article_timeout,
        article_workers=args.article_workers,
        user_agent=args.user_agent,
    )

    archived_articles.sort(key=combined_sort_key, reverse=True)
    recent_articles = archived_articles[:MAX_COMBINED_ITEMS]
    archive_index = build_archive_index(archived_articles)
    write_json(archive_dir / "index.json", archive_index)
    copy_directory(archive_dir, output_dir / "archive")

    stats = build_stats(feed_specs, feed_index, combined_items, archived_articles, generated_at)
    write_json(feeds_dir / "index.json", {"generated_at": generated_at, "stats": stats, "feeds": feed_index})
    write_json(
        feeds_dir / "combined.json",
        {
            "generated_at": generated_at,
            "site_url": site_url,
            "stats": stats,
            "items": [public_article_view(article) for article in recent_articles],
        },
    )
    write_text(
        feeds_dir / "combined.xml",
        build_combined_rss(site_url=site_url, generated_at=generated_at, items=recent_articles, fulltext=False),
    )
    write_text(
        feeds_dir / "fulltext.xml",
        build_combined_rss(site_url=site_url, generated_at=generated_at, items=recent_articles, fulltext=True),
    )
    write_text(
        output_dir / "index.html",
        build_home_page(
            site_url=site_url,
            generated_at=generated_at,
            stats=stats,
            feeds=feed_index,
            articles=recent_articles,
        ),
    )

    print(
        f"Generated {len(feed_index)} feeds, {len(recent_articles)} recent items, "
        f"and {len(archived_articles)} archived full-text articles into {output_dir}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a persistent RSS full-text archive and Pages site.")
    parser.add_argument("--opml", default="feeds.opml", help="Path to the source OPML file.")
    parser.add_argument("--output", default="dist", help="Directory where the static site is written.")
    parser.add_argument("--archive-dir", default="archive", help="Repository directory used for persistent article storage.")
    parser.add_argument("--site-url", required=True, help="Public base URL for the generated site.")
    parser.add_argument("--max-feeds", type=int, help="Optional limit for local smoke tests.")
    parser.add_argument("--feed-timeout", type=int, default=DEFAULT_FEED_TIMEOUT_SECONDS, help="Per-feed HTTP timeout in seconds.")
    parser.add_argument("--article-timeout", type=int, default=DEFAULT_ARTICLE_TIMEOUT_SECONDS, help="Per-article HTTP timeout in seconds.")
    parser.add_argument("--feed-workers", type=int, default=DEFAULT_FEED_WORKERS, help="Concurrent feed fetch worker count.")
    parser.add_argument("--article-workers", type=int, default=DEFAULT_ARTICLE_WORKERS, help="Concurrent article fetch worker count.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent sent to origin sites.")
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


def fetch_all_feeds(
    feed_specs: list[FeedSpec],
    combined_items: list[dict[str, object]],
    feeds_dir: Path,
    site_url: str,
    timeout_seconds: int,
    workers: int,
    user_agent: str,
) -> list[dict[str, object]]:
    ordered_results: list[tuple[dict[str, object], list[dict[str, object]]] | None] = [None] * len(feed_specs)
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
        future_map = {
            executor.submit(
                fetch_and_parse_feed,
                spec=spec,
                output_dir=feeds_dir,
                site_url=site_url,
                timeout_seconds=timeout_seconds,
                user_agent=user_agent,
            ): index
            for index, spec in enumerate(feed_specs)
        }
        for future in as_completed(future_map):
            ordered_results[future_map[future]] = future.result()

    feed_index: list[dict[str, object]] = []
    for result in ordered_results:
        if result is None:
            continue
        record, items = result
        feed_index.append(record)
        combined_items.extend(items)
    return feed_index


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
        payload, response_info = fetch_bytes(
            spec.xml_url,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            accept_header="application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        )
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


def fetch_bytes(
    url: str,
    timeout_seconds: int,
    user_agent: str,
    accept_header: str,
) -> tuple[bytes, dict[str, object]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": accept_header,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read()
            return payload, {
                "http_status": getattr(response, "status", None),
                "content_type": response.headers.get_content_type(),
                "content_charset": response.headers.get_content_charset(),
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
    feed_home = resolve_url(spec.html_url, atom_link(root) or spec.html_url)
    feed_description = child_text(root, "subtitle")
    items: list[dict[str, object]] = []
    for entry in iter_children(root, "entry"):
        title = child_text(entry, "title") or "(untitled)"
        link = resolve_url(feed_home, atom_link(entry) or feed_home or spec.html_url)
        guid = child_text(entry, "id") or link or title
        summary_html = child_inner_xml(entry, "summary")
        content_html = child_inner_xml(entry, "content") or summary_html
        summary_text = clean_summary(summary_html or content_html)
        published = normalize_timestamp(child_text(entry, "published") or child_text(entry, "updated"))
        items.append(
            {
                "id": build_article_id(spec.slug, guid or link or title),
                "source_slug": spec.slug,
                "source_title": feed_title,
                "source_xml_url": spec.xml_url,
                "source_html_url": spec.html_url,
                "feed_home_url": feed_home,
                "title": title,
                "link": link,
                "guid": guid,
                "published_at": published,
                "summary": summary_text,
                "summary_html": summary_html,
                "feed_content_html": content_html,
                "feed_content_text": html_to_text(content_html),
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
    feed_home = resolve_url(spec.html_url, child_text(channel, "link") or spec.html_url)
    feed_description = child_text(channel, "description")

    item_nodes = list(iter_children(channel, "item"))
    if not item_nodes and local_name(root.tag) == "RDF":
        item_nodes = list(iter_children(root, "item"))

    items: list[dict[str, object]] = []
    for item in item_nodes:
        title = child_text(item, "title") or "(untitled)"
        link = resolve_url(feed_home, child_text(item, "link") or feed_home or spec.html_url)
        guid = child_text(item, "guid") or link or title
        encoded_html = child_inner_xml(item, "encoded")
        description_html = child_inner_xml(item, "description")
        summary_html = description_html or encoded_html
        content_html = encoded_html or description_html
        summary_text = clean_summary(summary_html or content_html)
        published = normalize_timestamp(
            child_text(item, "pubDate")
            or child_text(item, "date")
            or child_text(item, "published")
            or child_text(item, "updated")
        )
        items.append(
            {
                "id": build_article_id(spec.slug, guid or link or title),
                "source_slug": spec.slug,
                "source_title": feed_title,
                "source_xml_url": spec.xml_url,
                "source_html_url": spec.html_url,
                "feed_home_url": feed_home,
                "title": title,
                "link": link,
                "guid": guid,
                "published_at": published,
                "summary": summary_text,
                "summary_html": summary_html,
                "feed_content_html": content_html,
                "feed_content_text": html_to_text(content_html),
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


def archive_articles(
    feed_items: list[dict[str, object]],
    existing_articles: dict[str, dict[str, object]],
    archive_dir: Path,
    site_url: str,
    generated_at: str,
    article_timeout_seconds: int,
    article_workers: int,
    user_agent: str,
) -> list[dict[str, object]]:
    current_items = dedupe_items(feed_items)
    work_results: list[dict[str, object] | None] = [None] * len(current_items)
    with ThreadPoolExecutor(max_workers=max(article_workers, 1)) as executor:
        future_map = {}
        for index, item in enumerate(current_items):
            existing = existing_articles.get(str(item["id"]))
            if existing and has_archived_content(existing):
                work_results[index] = merge_article_record(existing, item, site_url)
                continue
            future_map[
                executor.submit(
                build_article_record,
                item=item,
                site_url=site_url,
                generated_at=generated_at,
                timeout_seconds=article_timeout_seconds,
                user_agent=user_agent,
                )
            ] = index
        for future in as_completed(future_map):
            work_results[future_map[future]] = future.result()

    for index, item in enumerate(current_items):
        record = work_results[index]
        if record is None:
            record = merge_article_record(existing_articles.get(str(item["id"]), {}), item, site_url)
        existing_articles[str(item["id"])] = record
        write_json(article_json_path(archive_dir, record), record)
    return list(existing_articles.values())


def build_article_record(
    item: dict[str, object],
    site_url: str,
    generated_at: str,
    timeout_seconds: int,
    user_agent: str,
) -> dict[str, object]:
    record = merge_article_record({}, item, site_url)
    record["archived_at"] = generated_at

    feed_content = feed_fulltext_payload(item)
    if feed_content:
        record.update(feed_content)
        record["content_source"] = "feed"
        record["content_status"] = "ok"
        return record

    link = str(item.get("link") or "").strip()
    if not link:
        record["content_status"] = "error"
        record["error"] = "Missing article link"
        return record

    try:
        payload, response_info = fetch_bytes(
            link,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            accept_header="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        )
        html = decode_bytes(payload, response_info.get("content_charset"))
        extracted = extract_article_payload(html, link)
        if extracted:
            record.update(extracted)
            record["content_source"] = "article_page"
            record["content_status"] = "ok"
            record["article_fetch"] = response_info
            return record
        record["content_status"] = "error"
        record["article_fetch"] = response_info
        record["error"] = "Unable to extract article body"
        return record
    except Exception as exc:  # noqa: BLE001
        fallback = fallback_feed_payload(item)
        if fallback:
            record.update(fallback)
            record["content_source"] = "feed_fallback"
            record["content_status"] = "ok"
            record["warning"] = str(exc)
            return record
        record["content_status"] = "error"
        record["error"] = str(exc)
        return record


def merge_article_record(existing: dict[str, object], item: dict[str, object], site_url: str) -> dict[str, object]:
    record = dict(existing)
    article_url = archive_json_url(site_url, item)
    record.update(
        {
            "id": item["id"],
            "feed_slug": item["source_slug"],
            "feed_title": item["source_title"],
            "feed_home_url": item.get("feed_home_url"),
            "source_xml_url": item.get("source_xml_url"),
            "source_html_url": item.get("source_html_url"),
            "title": item.get("title"),
            "link": item.get("link"),
            "guid": item.get("guid"),
            "published_at": item.get("published_at"),
            "summary": item.get("summary"),
            "archive_json_url": article_url,
            "archive_relative_path": f"articles/{item['source_slug']}/{item['id']}.json",
        }
    )
    return record


def feed_fulltext_payload(item: dict[str, object]) -> dict[str, object] | None:
    content_html = sanitize_html_fragment(str(item.get("feed_content_html") or ""))
    content_text = normalize_space(str(item.get("feed_content_text") or ""))
    if not content_text:
        content_text = html_to_text(content_html)
    if not looks_like_fulltext(content_text):
        return None
    return {
        "content_html": content_html or text_to_html(content_text),
        "content_text": content_text,
        "content_length": len(content_text),
    }


def fallback_feed_payload(item: dict[str, object]) -> dict[str, object] | None:
    content_html = sanitize_html_fragment(str(item.get("feed_content_html") or item.get("summary_html") or ""))
    content_text = html_to_text(content_html)
    if not content_text:
        return None
    return {
        "content_html": content_html or text_to_html(content_text),
        "content_text": content_text,
        "content_length": len(content_text),
    }


def extract_article_payload(html: str, url: str) -> dict[str, object] | None:
    if trafilatura is not None:
        try:
            extracted_html = trafilatura.extract(
                html,
                url=url,
                output_format="html",
                include_comments=False,
                include_tables=True,
                include_links=True,
                include_images=True,
                fast=True,
            )
            extracted_text = trafilatura.extract(
                html,
                url=url,
                output_format="txt",
                include_comments=False,
                include_tables=True,
                fast=True,
            )
            extracted_text = normalize_space(extracted_text or "")
            if len(extracted_text) >= MIN_ARTICLE_EXTRACT_CHARS:
                return {
                    "content_html": sanitize_html_fragment(extracted_html or text_to_html(extracted_text)),
                    "content_text": extracted_text,
                    "content_length": len(extracted_text),
                    "extractor": "trafilatura",
                }
        except Exception:  # noqa: BLE001
            pass

    fragment = heuristic_article_fragment(html)
    text = html_to_text(fragment)
    if len(text) < MIN_ARTICLE_EXTRACT_CHARS:
        fragment = sanitize_html_fragment(strip_boilerplate(html))
        text = html_to_text(fragment)
    if len(text) < MIN_ARTICLE_EXTRACT_CHARS:
        return None
    return {
        "content_html": fragment or text_to_html(text),
        "content_text": text,
        "content_length": len(text),
        "extractor": "heuristic",
    }


def load_existing_articles(archive_dir: Path) -> dict[str, dict[str, object]]:
    articles: dict[str, dict[str, object]] = {}
    articles_root = archive_dir / "articles"
    if not articles_root.exists():
        return articles
    for path in sorted(articles_root.glob("*/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        article_id = str(payload.get("id") or path.stem)
        payload["archive_relative_path"] = str(path.relative_to(archive_dir)).replace("\\", "/")
        articles[article_id] = payload
    return articles


def build_archive_index(articles: list[dict[str, object]]) -> dict[str, object]:
    summaries = [archive_index_entry(article) for article in sorted(articles, key=combined_sort_key, reverse=True)]
    return {
        "article_count": len(summaries),
        "articles": summaries,
    }


def archive_index_entry(article: dict[str, object]) -> dict[str, object]:
    return {
        "id": article.get("id"),
        "feed_slug": article.get("feed_slug"),
        "feed_title": article.get("feed_title"),
        "title": article.get("title"),
        "link": article.get("link"),
        "published_at": article.get("published_at"),
        "summary": article.get("summary"),
        "content_length": article.get("content_length", 0),
        "content_source": article.get("content_source"),
        "content_status": article.get("content_status"),
        "archive_relative_path": article.get("archive_relative_path"),
        "archive_json_url": article.get("archive_json_url"),
    }


def build_stats(
    specs: list[FeedSpec],
    feed_index: list[dict[str, object]],
    combined_items: list[dict[str, object]],
    archived_articles: list[dict[str, object]],
    generated_at: str,
) -> dict[str, object]:
    ok_count = sum(1 for feed in feed_index if feed.get("status") == "ok")
    error_count = len(feed_index) - ok_count
    fulltext_ok = sum(1 for article in archived_articles if article.get("content_status") == "ok")
    return {
        "generated_at": generated_at,
        "configured_feed_count": len(specs),
        "successful_feed_count": ok_count,
        "failed_feed_count": error_count,
        "current_feed_item_count": len(combined_items),
        "archived_article_count": len(archived_articles),
        "archived_fulltext_count": fulltext_ok,
        "combined_item_count": min(len(archived_articles), MAX_COMBINED_ITEMS),
    }


def build_combined_rss(site_url: str, generated_at: str, items: list[dict[str, object]], fulltext: bool) -> str:
    pub_date = format_http_date(generated_at) or format_datetime(datetime.now(timezone.utc))
    namespace = ' xmlns:content="http://purl.org/rss/1.0/modules/content/"' if fulltext else ""
    title = "rss-cache fulltext feed" if fulltext else "rss-cache combined feed"
    description = "Full-text archived articles generated from the OPML source list." if fulltext else "Combined feed generated from the OPML source list."
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<rss version=\"2.0\"{namespace}>",
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(site_url)}</link>",
        f"<description>{escape(description)}</description>",
        "<language>en</language>",
        f"<lastBuildDate>{escape(pub_date)}</lastBuildDate>",
        f"<generator>{escape(DEFAULT_USER_AGENT)}</generator>",
    ]
    for item in items:
        description_text = clean_summary(str(item.get("summary") or item.get("content_text") or ""))
        if description_text:
            description_text = f"[{item.get('feed_title')}] {description_text}"
        pub = format_http_date(item.get("published_at"))
        lines.extend(
            [
                "<item>",
                f"<title>{escape(str(item.get('title') or '(untitled)'))}</title>",
                f"<link>{escape(str(item.get('link') or site_url))}</link>",
                f"<guid isPermaLink=\"false\">{escape(str(item.get('id') or item.get('guid') or item.get('link') or item.get('title')))}</guid>",
                f"<description>{escape(description_text)}</description>",
                f"<category>{escape(str(item.get('feed_title') or 'unknown'))}</category>",
            ]
        )
        if pub:
            lines.append(f"<pubDate>{escape(pub)}</pubDate>")
        if fulltext:
            lines.append(f"<content:encoded><![CDATA[{rss_content_html(item)}]]></content:encoded>")
        lines.append("</item>")
    lines.extend(["</channel>", "</rss>"])
    return "\n".join(lines) + "\n"


def build_home_page(
    site_url: str,
    generated_at: str,
    stats: dict[str, object],
    feeds: list[dict[str, object]],
    articles: list[dict[str, object]],
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
    for item in articles[:40]:
        source = escape(str(item.get("feed_title") or "unknown"))
        title = escape(str(item.get("title") or "(untitled)"))
        link = escape(str(item.get("link") or site_url))
        archive_json = escape(str(item.get("archive_json_url") or "#"))
        published = escape(str(item.get("published_at") or ""))
        latest_items.append(
            f"<li><a href=\"{link}\">{title}</a> <small>{source} {published}</small> "
            f"<small><a href=\"{archive_json}\">archive json</a></small></li>"
        )

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
        GitHub Actions fetches the OPML source list, archives article full text into repository-tracked JSON files, and publishes feed mirrors plus public archive files to GitHub Pages.
      </p>
      <div class="links">
        <a href="{escape(site_url)}/archive/index.json">archive/index.json</a>
        <a href="{escape(site_url)}/feeds/index.json">feeds/index.json</a>
        <a href="{escape(site_url)}/feeds/combined.json">feeds/combined.json</a>
        <a href="{escape(site_url)}/feeds/fulltext.xml">feeds/fulltext.xml</a>
      </div>
      <section class="stats">
        <div class="panel"><strong>{escape(str(stats.get("configured_feed_count")))}</strong>Configured feeds</div>
        <div class="panel"><strong>{escape(str(stats.get("successful_feed_count")))}</strong>Successful mirrors</div>
        <div class="panel"><strong>{escape(str(stats.get("archived_article_count")))}</strong>Archived articles</div>
        <div class="panel"><strong>{escape(str(stats.get("archived_fulltext_count")))}</strong>Archived with full text</div>
      </section>
      <section class="panel" style="margin-bottom: 1.5rem;">
        <strong>Generated at</strong>
        <div>{escape(generated_at)}</div>
      </section>
      <section style="margin-bottom: 2rem;">
        <h2>Latest Archived Articles</h2>
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


def public_article_view(article: dict[str, object]) -> dict[str, object]:
    return {
        "id": article.get("id"),
        "feed_slug": article.get("feed_slug"),
        "feed_title": article.get("feed_title"),
        "title": article.get("title"),
        "link": article.get("link"),
        "published_at": article.get("published_at"),
        "summary": article.get("summary"),
        "content_length": article.get("content_length", 0),
        "content_source": article.get("content_source"),
        "archive_json_url": article.get("archive_json_url"),
    }


def archive_json_url(site_url: str, item: dict[str, object]) -> str:
    return f"{site_url}/archive/articles/{item['source_slug']}/{item['id']}.json"


def article_json_path(archive_dir: Path, article: dict[str, object]) -> Path:
    return archive_dir / "articles" / str(article["feed_slug"]) / f"{article['id']}.json"


def build_article_id(source_slug: str, identity: str) -> str:
    digest = hashlib.sha1(f"{source_slug}::{identity}".encode("utf-8")).hexdigest()[:16]
    return f"{source_slug}-{digest}"


def dedupe_items(feed_items: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: dict[str, dict[str, object]] = {}
    for item in feed_items:
        article_id = str(item["id"])
        existing = deduped.get(article_id)
        if existing is None or combined_sort_key(item) > combined_sort_key(existing):
            deduped[article_id] = item
    return list(deduped.values())


def has_archived_content(article: dict[str, object]) -> bool:
    return bool(normalize_space(str(article.get("content_text") or "")) or normalize_space(str(article.get("content_html") or "")))


def child_inner_xml(parent: ET.Element, *names: str) -> str:
    child = first_child(parent, *names)
    if child is None:
        return ""
    pieces = []
    if child.text:
        pieces.append(child.text)
    for sub in list(child):
        pieces.append(ET.tostring(sub, encoding="unicode"))
    return "".join(pieces).strip()


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


def resolve_url(base: str, url: str) -> str:
    if not url:
        return ""
    if base:
        return urljoin(base, url)
    return url


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def html_to_text(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", " ", value)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def clean_summary(value: str) -> str:
    collapsed = normalize_space(html_to_text(value or ""))
    if len(collapsed) <= SUMMARY_PREVIEW_LIMIT:
        return collapsed
    return collapsed[: SUMMARY_PREVIEW_LIMIT - 1].rstrip() + "…"


def looks_like_fulltext(content_text: str) -> bool:
    return len(content_text) >= MIN_FULLTEXT_CHARS


def decode_bytes(payload: bytes, charset: object) -> str:
    if charset:
        try:
            return payload.decode(str(charset), errors="replace")
        except LookupError:
            pass
    return payload.decode("utf-8", errors="replace")


def sanitize_html_fragment(value: str) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"(?is)<(script|style|noscript|iframe|svg)[^>]*>.*?</\1>", "", value)
    cleaned = re.sub(r"(?i)\son\w+\s*=\s*(['\"]).*?\1", "", cleaned)
    cleaned = re.sub(r"(?i)\sstyle\s*=\s*(['\"]).*?\1", "", cleaned)
    return cleaned.strip()


def strip_boilerplate(html: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style|noscript|iframe|svg|header|footer|nav|aside|form)[^>]*>.*?</\1>", " ", html)
    body_match = re.search(r"(?is)<body[^>]*>(.*)</body>", cleaned)
    return body_match.group(1) if body_match else cleaned


def heuristic_article_fragment(html: str) -> str:
    for pattern in (
        r"(?is)<article\b[^>]*>(.*)</article>",
        r"(?is)<main\b[^>]*>(.*)</main>",
        r'(?is)<div\b[^>]+(?:id|class)=["\'][^"\']*(?:content|article|post|entry|main)[^"\']*["\'][^>]*>(.*)</div>',
    ):
        match = re.search(pattern, html)
        if match:
            return sanitize_html_fragment(match.group(1))
    return sanitize_html_fragment(strip_boilerplate(html))


def text_to_html(text: str) -> str:
    paragraphs = [segment.strip() for segment in re.split(r"\n{2,}", text) if segment.strip()]
    if not paragraphs:
        return ""
    return "".join(f"<p>{escape(segment)}</p>" for segment in paragraphs)


def rss_content_html(item: dict[str, object]) -> str:
    content_html = sanitize_html_fragment(str(item.get("content_html") or ""))
    if content_html:
        return content_html.replace("]]>", "]]&gt;")
    content_text = normalize_space(str(item.get("content_text") or ""))
    return text_to_html(content_text).replace("]]>", "]]&gt;")


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


def combined_sort_key(item: dict[str, object]) -> tuple[int, str, str]:
    published_at = str(item.get("published_at") or "")
    title = str(item.get("title") or "")
    if not published_at:
        return (0, "", title)
    return (1, published_at, title)


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


def copy_directory(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
