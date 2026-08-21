import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

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
    storage TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_keep_published ON items (keep, published);
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
                """UPDATE items SET classified = 1, keep = ?, tag = ?, critical = ?,
                       cpu = ?, ram = ?, storage = ? WHERE id = ?""",
                (1 if r["keep"] else 0, r["tag"], 1 if r["critical"] else 0,
                 r.get("cpu"), r.get("ram"), r.get("storage"), item_id),
            )


def get_curated(limit: int = 100, kind: str | None = None) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if kind is None:
            return conn.execute(
                """SELECT * FROM items WHERE classified = 1 AND keep = 1
                   ORDER BY published DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return conn.execute(
            """SELECT * FROM items WHERE classified = 1 AND keep = 1 AND kind = ?
               ORDER BY published DESC LIMIT ?""",
            (kind, limit),
        ).fetchall()


def get_curated_deals(limit: int = 300) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM items WHERE classified = 1 AND keep = 1 AND kind = 'deal'
               ORDER BY (price + COALESCE(shipping, 0)) ASC LIMIT ?""",
            (limit,),
        ).fetchall()


def counts() -> dict[str, int]:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN classified = 0 THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN classified = 1 AND keep = 1 THEN 1 ELSE 0 END) AS kept,
                   SUM(CASE WHEN classified = 1 AND keep = 0 THEN 1 ELSE 0 END) AS dropped
               FROM items"""
        ).fetchone()
        return dict(row)
