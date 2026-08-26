import hashlib
import html
import logging
import re
import time

import feedparser
import yaml

from config import settings

log = logging.getLogger("feeds")

_TAG_RE = re.compile(r"<[^>]+>")
_DANGLING_TAG_RE = re.compile(r"<[^>]*$")

# feedparser's default User-Agent gets rate-limited/blocked by Reddit after a couple of requests
# in quick succession (no error, just stale/empty responses) - a real browser UA plus a small delay
# between requests keeps every subreddit in sources.yaml fetching reliably, not just the first one.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_DELAY_SECONDS = 1.5


def _load_sources() -> list[dict]:
    with open(settings.sources_file) as f:
        data = yaml.safe_load(f)
    sources = []
    for group in data.values():
        sources.extend(group)
    return sources


def _item_id(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()[:16]


def _clean_summary(raw: str) -> str:
    """Strip HTML tags and decode entities before truncating, not after -
    truncating raw HTML first can cut mid-tag and leave malformed markup
    that neither displays nor sends to the classifier cleanly. Also drops a
    trailing unterminated tag (no closing '>' at all) - the regex above
    can't match those, and they show up in data already truncated by an
    older version of this function or upstream."""
    text = _TAG_RE.sub(" ", raw)
    text = _DANGLING_TAG_RE.sub("", text)
    text = html.unescape(text)
    return " ".join(text.split())[:500]


def fetch_all() -> list[dict]:
    items = []
    sources = _load_sources()
    for i, source in enumerate(sources):
        if i > 0:
            time.sleep(_REQUEST_DELAY_SECONDS)
        parsed = feedparser.parse(source["url"], request_headers={"User-Agent": _USER_AGENT})
        if parsed.bozo and not parsed.entries:
            log.warning("failed to parse feed %s (%s): %s", source["name"], source["url"], parsed.bozo_exception)
            continue
        if not parsed.entries:
            log.warning("feed %s (%s) returned no entries - possibly rate-limited/blocked", source["name"], source["url"])
            continue
        for entry in parsed.entries[: settings.max_items_per_source]:
            link = entry.get("link", "")
            if not link:
                continue
            items.append({
                "id": _item_id(link),
                "source": source["name"],
                "title": entry.get("title", "(no title)").strip(),
                "link": link,
                "summary": _clean_summary(entry.get("summary", "")),
                "published": entry.get("published", "") or entry.get("updated", ""),
            })
    return items
