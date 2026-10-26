import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    summary TEXT,
    published TEXT,
    seen_at TEXT NOT NULL,
    classified INTEGER NOT NULL DEFAULT 0,
    keep INTEGER,
    tag TEXT,
    critical INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'news',
    price REAL,
    shipping REAL,
    currency TEXT,
    cpu TEXT,
    ram TEXT,
    storage TEXT,
    last_checked_at TEXT,
    capacity TEXT,
    kit TEXT,
    severity TEXT,
    cvss REAL,
    exploited INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_keep_published ON items (keep, published);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "critical" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN critical INTEGER NOT NULL DEFAULT 0")
    if "kind" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN kind TEXT NOT NULL DEFAULT 'news'")
    if "price" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN price REAL")
    if "shipping" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN shipping REAL")
    if "currency" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN currency TEXT")
    if "cpu" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN cpu TEXT")
    if "ram" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN ram TEXT")
    if "storage" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN storage TEXT")
    if "last_checked_at" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN last_checked_at TEXT")
    if "capacity" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN capacity TEXT")
    if "kit" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN kit TEXT")
    if "severity" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN severity TEXT")
        # News kept by the old prompt has no severity: its "critical" items stay red, everything
        # else (general homelab/DevOps news) drops off the security page.
        conn.execute("UPDATE items SET severity = 'critical' WHERE kind = 'news' AND critical = 1")
    if "cvss" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN cvss REAL")
    if "exploited" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN exploited INTEGER NOT NULL DEFAULT 0")
    if "dismissed" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")

    # One-time backfill: items ingested before feeds.py stripped HTML from summaries can have
    # raw/mid-tag-truncated markup baked into this column - which was making 1min.ai reject the
    # whole batch outright (status: FAILURE) instead of just that one item. Only unclassified rows
    # matter here; already-classified ones already succeeded through the API with whatever they had.
    from feeds import _clean_summary
    dirty = conn.execute(
        "SELECT id, summary FROM items WHERE classified = 0 AND summary LIKE '%<%'"
    ).fetchall()
    for row in dirty:
        conn.execute("UPDATE items SET summary = ? WHERE id = ?", (_clean_summary(row["summary"]), row["id"]))


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def insert_new_items(items: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with get_conn() as conn:
        for item in items:
            cur = conn.execute(
                """INSERT OR IGNORE INTO items (id, source, title, link, summary, published, seen_at, kind, price, shipping, currency)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item["id"], item["source"], item["title"], item["link"],
                 item.get("summary", ""), item.get("published", ""), now,
                 item.get("kind", "news"), item.get("price"), item.get("shipping"), item.get("currency", "")),
            )
            inserted += cur.rowcount
    return inserted


def get_unclassified(limit: int, kind: str = "news") -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, title, summary, source, price, shipping, currency FROM items WHERE classified = 0 AND kind = ? LIMIT ?",
            (kind, limit),
        ).fetchall()


def apply_classifications(results: dict[str, dict]) -> None:
    with get_conn() as conn:
        for item_id, r in results.items():
            conn.execute(
                """UPDATE items SET classified = 1, keep = ?, tag = ?, critical = ?, severity = ?,
                       cpu = ?, ram = ?, storage = ?, capacity = ?, kit = ? WHERE id = ?""",
                (1 if r["keep"] else 0, r["tag"], 1 if r["critical"] else 0, r.get("severity") or None,
                 r.get("cpu"), r.get("ram"), r.get("storage"), r.get("capacity"), r.get("kit"), item_id),
            )
            # Only the RAM prompt's classifier ever fills this in (an English translation of a
            # non-English title) - leave the stored title alone otherwise rather than overwrite it
            # with an empty string.
            title_en = r.get("title_en")
            if title_en:
                conn.execute("UPDATE items SET title = ? WHERE id = ?", (title_en, item_id))


def get_curated(limit: int = 100, kind: str | None = None) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if kind is None:
            return conn.execute(
                """SELECT * FROM items WHERE classified = 1 AND keep = 1
                   ORDER BY seen_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return conn.execute(
            # Not ORDER BY published: that's a free-form feed string (RFC 822 or ISO), so SQLite
            # sorts it by weekday name. seen_at is our own ISO timestamp; callers re-sort by the
            # parsed published date.
            """SELECT * FROM items WHERE classified = 1 AND keep = 1 AND kind = ?
               ORDER BY seen_at DESC LIMIT ?""",
            (kind, limit),
        ).fetchall()


def get_curated_deals(limit: int = 300, min_total: float | None = None, max_total: float | None = None,
                       kind: str = "deal") -> list[sqlite3.Row]:
    with get_conn() as conn:
        clauses = ["classified = 1", "keep = 1", "kind = ?"]
        params: list = [kind]
        if min_total is not None:
            clauses.append("(price + COALESCE(shipping, 0)) > ?")
            params.append(min_total)
        if max_total is not None:
            clauses.append("(price + COALESCE(shipping, 0)) <= ?")
            params.append(max_total)
        params.append(limit)
        return conn.execute(
            f"""SELECT * FROM items WHERE {' AND '.join(clauses)}
                ORDER BY (price + COALESCE(shipping, 0)) ASC LIMIT ?""",
            params,
        ).fetchall()


def get_deals_to_recheck(limit: int = 50) -> list[sqlite3.Row]:
    """Kept eBay-backed listings (mini-PC deals and RAM deals share this sweep/budget) due for an
    availability check, never-checked ones first, then the ones checked longest ago - so a sweep
    of `limit` per poll steadily cycles through the whole set over time regardless of how many
    listings have piled up."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT id, link, source FROM items
               WHERE kind IN ('deal', 'ram') AND classified = 1 AND keep = 1
               ORDER BY (last_checked_at IS NULL) DESC, last_checked_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()


def mark_checked(ids: list[str]) -> None:
    if not ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.executemany("UPDATE items SET last_checked_at = ? WHERE id = ?", [(now, i) for i in ids])


def delete_items(ids: list[str]) -> None:
    if not ids:
        return
    with get_conn() as conn:
        conn.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in ids])


def delete_old_items(days: int, kind: str = "news") -> int:
    """News items have no availability re-check the way eBay-backed deals do, so without this a
    column with too little fresh volume just accumulates old items forever instead of going quiet -
    cutoff is based on seen_at (when this app first indexed the item), not the feed-supplied
    published date, since that's a free-form string some feeds format inconsistently."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM items WHERE kind = ? AND seen_at < ?", (kind, cutoff))
        return cur.rowcount


def get_meta(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def counts(kind: str = "news") -> dict[str, int]:
    """Per-kind, so the News page's "shown / filtered out" numbers aren't inflated by the thousands
    of eBay listings the deals pipelines reject."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN classified = 0 THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN classified = 1 AND keep = 1 THEN 1 ELSE 0 END) AS kept,
                   SUM(CASE WHEN classified = 1 AND keep = 0 THEN 1 ELSE 0 END) AS dropped
               FROM items WHERE kind = ?""",
            (kind,),
        ).fetchone()
        return dict(row)


_SEVERITY_RANK = {"high": 1, "critical": 2}


def upsert_advisories(advisories: list[dict]) -> tuple[int, int]:
    """Insert or merge KEV/NVD rows (one row per CVE). Returns (new, escalated).

    Merging: the worse severity wins, "exploited" sticks once set, KEV's wording (which says what
    to do) wins over NVD's, and a real product tag wins over "other". An escalation - High turning
    Critical, or a CVE newly added to KEV - resets seen_at so it comes back to the top of the page
    even if it had aged off, and un-dismisses it: "not my problem" was decided on less information."""
    now = datetime.now(timezone.utc).isoformat()
    new = escalated = 0
    with get_conn() as conn:
        for a in advisories:
            row = conn.execute(
                "SELECT severity, cvss, exploited, tag, source, title, summary, published, seen_at "
                "FROM items WHERE id = ?", (a["id"],),
            ).fetchone()
            if row is None:
                severity = "critical" if a["exploited"] and a["tag"] != "other" else a["severity"]
                conn.execute(
                    """INSERT INTO items (id, source, title, link, summary, published, seen_at, kind,
                           classified, keep, tag, critical, severity, cvss, exploited)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'advisory', 1, 1, ?, ?, ?, ?, ?)""",
                    (a["id"], a["source"], a["title"], a["link"], a["summary"], a["published"], now,
                     a["tag"], 1 if severity == "critical" else 0, severity, a["cvss"],
                     1 if a["exploited"] else 0),
                )
                new += 1
                continue

            exploited = bool(row["exploited"]) or bool(a["exploited"])
            tag = a["tag"] if a["tag"] != "other" else (row["tag"] or "other")
            cvss = a["cvss"] if a["cvss"] is not None else row["cvss"]
            old_rank = _SEVERITY_RANK.get(row["severity"] or "", 0)
            rank = max(old_rank, _SEVERITY_RANK.get(a["severity"], 0))
            if exploited and tag != "other":
                rank = _SEVERITY_RANK["critical"]
            severity = "critical" if rank == 2 else "high"
            # KEV text says what to do; keep it once we have it.
            use_new_text = a["source"] == "CISA KEV" or row["source"] != "CISA KEV"
            source, title, summary, published = (
                (a["source"], a["title"], a["summary"], a["published"]) if use_new_text
                else (row["source"], row["title"], row["summary"], row["published"])
            )
            is_escalation = rank > old_rank or (exploited and not row["exploited"])
            if is_escalation:
                escalated += 1
            conn.execute(
                """UPDATE items SET source = ?, title = ?, summary = ?, published = ?, tag = ?,
                       severity = ?, critical = ?, cvss = ?, exploited = ?, seen_at = ?,
                       dismissed = CASE WHEN ? THEN 0 ELSE dismissed END,
                       classified = 1, keep = 1 WHERE id = ?""",
                (source, title, summary, published, tag, severity, 1 if severity == "critical" else 0,
                 cvss, 1 if exploited else 0, now if is_escalation else row["seen_at"],
                 1 if is_escalation else 0, a["id"]),
            )
    return new, escalated


def get_security_items(days: int, limit: int = 500, dismissed: bool = False) -> list[sqlite3.Row]:
    """Everything flagged Critical or High (news and KEV/NVD rows) first seen - or escalated -
    in the last `days` days, newest first. dismissed=True returns only the ones you hid instead."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM items
               WHERE kind IN ('news', 'advisory') AND classified = 1 AND keep = 1
                 AND severity IN ('critical', 'high') AND seen_at >= ? AND dismissed = ?
               ORDER BY seen_at DESC LIMIT ?""",
            (cutoff, 1 if dismissed else 0, limit),
        ).fetchall()


def count_dismissed(days: int) -> int:
    return len(get_security_items(days, dismissed=True))


def set_dismissed(item_id: str, dismissed: bool) -> bool:
    """Returns False if there's no such item."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE items SET dismissed = ? WHERE id = ? AND kind IN ('news', 'advisory')",
            (1 if dismissed else 0, item_id),
        )
        return cur.rowcount > 0


_HEALTH_KEY = "source_health"


def record_source_health(name: str, ok: bool, error: str = "") -> None:
    """Last fetch result per source, shown on the page - a dead feed must be visible, not just a
    line in the logs, or "nothing new" and "not looking" look the same."""
    health = get_source_health()
    entry = health.get(name, {})
    now = datetime.now(timezone.utc).isoformat()
    entry.update({"ok": ok, "checked_at": now, "error": "" if ok else error})
    if ok:
        entry["last_ok_at"] = now
    health[name] = entry
    set_meta(_HEALTH_KEY, json.dumps(health))


def get_source_health() -> dict[str, dict]:
    raw = get_meta(_HEALTH_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}
