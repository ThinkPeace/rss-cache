# rss-cache

Mirror a curated OPML list of RSS and Atom feeds to GitHub Pages.

The repository is designed for a GitHub Actions scheduled run:

1. Read feed definitions from `feeds.opml`.
2. Fetch each source feed from GitHub-hosted runners.
3. Publish a static site to GitHub Pages with:
   - raw mirrored feed files under `feeds/<slug>.xml`
   - a combined machine-readable catalog at `feeds/index.json`
   - a combined item stream at `feeds/combined.json`
   - a synthetic aggregated RSS feed at `feeds/combined.xml`

After Pages is enabled, the default URLs are:

- `https://ThinkPeace.github.io/rss-cache/`
- `https://ThinkPeace.github.io/rss-cache/feeds/index.json`
- `https://ThinkPeace.github.io/rss-cache/feeds/combined.json`
- `https://ThinkPeace.github.io/rss-cache/feeds/combined.xml`

## Local run

```bash
python3 scripts/build_site.py \
  --opml feeds.opml \
  --output dist \
  --site-url https://ThinkPeace.github.io/rss-cache
```

For a smaller local smoke test:

```bash
python3 scripts/build_site.py \
  --opml feeds.opml \
  --output /tmp/rss-cache-smoke \
  --site-url https://example.com/rss-cache \
  --max-feeds 3
```
