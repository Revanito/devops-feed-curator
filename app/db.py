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
    critical INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_keep_published ON items (keep, published);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "critical" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN critical INTEGER NOT NULL DEFAULT 0")


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
                """INSERT OR IGNORE INTO items (id, source, title, link, summary, published, seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (item["id"], item["source"], item["title"], item["link"],
                 item.get("summary", ""), item.get("published", ""), now),
            )
            inserted += cur.rowcount
    return inserted


def get_unclassified(limit: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, title, summary, source FROM items WHERE classified = 0 LIMIT ?",
            (limit,),
        ).fetchall()


def apply_classifications(results: dict[str, tuple[bool, str, bool]]) -> None:
    with get_conn() as conn:
        for item_id, (keep, tag, critical) in results.items():
            conn.execute(
                "UPDATE items SET classified = 1, keep = ?, tag = ?, critical = ? WHERE id = ?",
                (1 if keep else 0, tag, 1 if critical else 0, item_id),
            )


def get_curated(limit: int = 100) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM items WHERE classified = 1 AND keep = 1
               ORDER BY published DESC LIMIT ?""",
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
