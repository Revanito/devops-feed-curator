import hashlib
import logging

import feedparser
import yaml

from config import settings

log = logging.getLogger("feeds")


def _load_sources() -> list[dict]:
    with open(settings.sources_file) as f:
        data = yaml.safe_load(f)
    sources = []
    for group in data.values():
        sources.extend(group)
    return sources


def _item_id(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()[:16]


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
                "summary": entry.get("summary", "")[:500],
                "published": entry.get("published", "") or entry.get("updated", ""),
            })
    return items
