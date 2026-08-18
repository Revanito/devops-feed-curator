import hashlib
import html
import logging
import re

import feedparser
import yaml

from config import settings

log = logging.getLogger("feeds")

_TAG_RE = re.compile(r"<[^>]+>")


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
    that neither displays nor sends to the classifier cleanly."""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return " ".join(text.split())[:500]


def fetch_all() -> list[dict]:
    items = []
    for source in _load_sources():
        parsed = feedparser.parse(source["url"])
        if parsed.bozo and not parsed.entries:
            log.warning("failed to parse feed %s (%s): %s", source["name"], source["url"], parsed.bozo_exception)
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
