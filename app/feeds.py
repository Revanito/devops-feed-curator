import hashlib
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import yaml

import db
from config import settings

log = logging.getLogger("feeds")

_TAG_RE = re.compile(r"<[^>]+>")
_DANGLING_TAG_RE = re.compile(r"<[^>]*$")
_REDDIT_LINK_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)/", re.IGNORECASE)

# feedparser's default User-Agent gets rate-limited/blocked by Reddit after the first request or
# two (no HTTP error - just an empty-but-valid feed body), so a real browser UA plus a delay between
# requests keeps every subreddit in sources.yaml fetching reliably, not just the first one. Fetching
# with httpx first (rather than letting feedparser do its own networking) also lets us log the real
# HTTP status/headers when a fetch comes back empty, instead of guessing why.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REQUEST_DELAY_SECONDS = 1.5


def _load_source_groups() -> dict[str, list[dict]]:
    with open(settings.sources_file) as f:
        return yaml.safe_load(f)


def _load_sources() -> list[dict]:
    """Every source except Reddit, which is fetched separately (see fetch_reddit()) on its own
    clock - it can't tolerate being polled on the same cadence as everything else."""
    sources = []
    for group_name, group in _load_source_groups().items():
        if group_name == "reddit":
            continue
        sources.extend(group)
    return sources


def _reddit_source() -> dict | None:
    reddit = _load_source_groups().get("reddit") or []
    return reddit[0] if reddit else None


def _item_id(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()[:16]


def _entry_source_name(source: dict, entry) -> str:
    """A combined multireddit feed reports one static source["name"] for entries actually spanning
    several subreddits - pull the real r/name out of the entry's own link instead, so cards and the
    Reddit/Homelab column split still reflect the subreddit a post actually came from."""
    match = _REDDIT_LINK_RE.search(entry.get("link", ""))
    return f"r/{match.group(1)}" if match else source["name"]


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


def _fetch_one(client: httpx.Client, source: dict) -> tuple[list[dict], bool]:
    """Fetch and parse one source. Returns (items, reached_server) - reached_server is True as soon
    as we get any HTTP response at all (even an error one), so callers that need to respect a rate
    limit (see fetch_reddit()) can tell "we used up an attempt" apart from "the network dropped, this
    attempt didn't count"."""
    try:
        resp = client.get(source["url"])
    except httpx.HTTPError as exc:
        log.warning("failed to fetch feed %s (%s): %s", source["name"], source["url"], exc)
        return [], False
    if resp.status_code != 200:
        log.warning(
            "feed %s (%s) returned HTTP %d (retry-after=%s, remaining=%s)",
            source["name"], source["url"], resp.status_code,
            resp.headers.get("retry-after"), resp.headers.get("x-ratelimit-remaining"),
        )
        return [], True
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        log.warning("failed to parse feed %s (%s): %s", source["name"], source["url"], parsed.bozo_exception)
        return [], True
    if not parsed.entries:
        log.warning(
            "feed %s (%s) returned HTTP 200 but no entries (content-length=%s) - possibly rate-limited",
            source["name"], source["url"], len(resp.content),
        )
        return [], True
    items = []
    limit = source.get("limit", settings.max_items_per_source)
    for entry in parsed.entries[:limit]:
        link = entry.get("link", "")
        if not link:
            continue
        items.append({
            "id": _item_id(link),
            "source": _entry_source_name(source, entry),
            "title": entry.get("title", "(no title)").strip(),
            "link": link,
            "summary": _clean_summary(entry.get("summary", "")),
            "published": entry.get("published", "") or entry.get("updated", ""),
        })
    return items, True


def fetch_all() -> list[dict]:
    items = []
    sources = _load_sources()
    with httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=15, follow_redirects=True) as client:
        for i, source in enumerate(sources):
            if i > 0:
                time.sleep(_REQUEST_DELAY_SECONDS)
            source_items, _ = _fetch_one(client, source)
            items.extend(source_items)
    return items


_REDDIT_LAST_FETCH_KEY = "reddit_last_fetch_at"


def fetch_reddit() -> list[dict]:
    """Reddit gets its own, much longer clock (REDDIT_POLL_HOURS) instead of running on every poll
    like everything else - its anonymous RSS access can't tolerate that. Runs independently of
    fetch_all() and of manual refreshes, so neither burns down its budget."""
    source = _reddit_source()
    if source is None:
        return []
    last = db.get_meta(_REDDIT_LAST_FETCH_KEY)
    if last is not None:
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        if elapsed < timedelta(hours=settings.reddit_poll_hours):
            log.info("skipping reddit fetch - last attempt %.1fh ago, due again in %.1fh",
                      elapsed.total_seconds() / 3600,
                      settings.reddit_poll_hours - elapsed.total_seconds() / 3600)
            return []
    with httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=15, follow_redirects=True) as client:
        items, reached_server = _fetch_one(client, source)
    if reached_server:
        db.set_meta(_REDDIT_LAST_FETCH_KEY, datetime.now(timezone.utc).isoformat())
    return items
