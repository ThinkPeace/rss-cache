# rss-cache

Archive a curated OPML list of RSS and Atom feeds, persist article full text into the GitHub repository, and publish public JSON and RSS artifacts via GitHub Pages.

The repository is designed for a GitHub Actions scheduled run:

1. Read feed definitions from `feeds.opml`.
2. Fetch each source feed from GitHub-hosted runners.
3. Parse feed items and assign stable `article_id` values.
4. Reuse full text directly from RSS when available; otherwise fetch the article page and extract the main content.
5. Persist article JSON files under `archive/articles/<feed>/<article-id>.json`.
6. Commit `archive/` back into `main` so the corpus survives future feed churn.
7. Publish a static site to GitHub Pages with:
   - raw mirrored feed files under `feeds/<slug>.xml`
   - archive index at `archive/index.json`
   - archived article JSON files under `archive/articles/...`
   - combined item JSON at `feeds/combined.json`
   - a full-text RSS feed at `feeds/fulltext.xml`

After Pages is enabled, the default URLs are:

- `https://ThinkPeace.github.io/rss-cache/`
- `https://ThinkPeace.github.io/rss-cache/archive/index.json`
- `https://ThinkPeace.github.io/rss-cache/feeds/index.json`
- `https://ThinkPeace.github.io/rss-cache/feeds/combined.json`
- `https://ThinkPeace.github.io/rss-cache/feeds/fulltext.xml`

## Local run

Install dependencies first:

```bash
python3 -m pip install -r requirements.txt
```

Then build:

```bash
python3 scripts/build_site.py \
  --opml feeds.opml \
  --archive-dir archive \
  --output dist \
  --site-url https://ThinkPeace.github.io/rss-cache
```

For a smaller smoke test:

```bash
python3 scripts/build_site.py \
  --opml feeds.opml \
  --archive-dir /tmp/rss-cache-archive \
  --output /tmp/rss-cache-smoke \
  --site-url https://example.com/rss-cache \
  --max-feeds 2
```

## Archive format

Each article JSON includes:

- source feed metadata
- canonical article link and GUID
- published timestamp
- summary
- extracted `content_text`
- extracted or synthesized `content_html`
- archive JSON URL for public access

This structure is intended to make later custom RSS generation trivial: the next layer can read `archive/index.json` and emit whatever feed format you want without touching the origin sites again.
