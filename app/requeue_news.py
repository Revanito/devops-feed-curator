"""One-off maintenance: mark recently-dropped news items as unclassified again, so the next poll
re-runs them through the classifier. Use after a classifier outage (items quarantined as
`unclassifiable`) or a prompt change that should rescue items the old prompt dropped.

Only touches dropped news seen in the last N days (default 4) - kept items and deals are left alone.

Run with: docker compose exec feed-curator python requeue_news.py [days]
Then POST /refresh (or wait for the midnight poll) to re-classify them.
"""

import sys
from datetime import datetime, timedelta, timezone

import db

days = int(sys.argv[1]) if len(sys.argv) > 1 else 4
cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

db.init_db()
with db.get_conn() as conn:
    quarantined = conn.execute(
        "SELECT COUNT(*) FROM items WHERE kind = 'news' AND keep = 0 AND tag = 'unclassifiable' AND seen_at >= ?",
        (cutoff,),
    ).fetchone()[0]
    requeued = conn.execute(
        "UPDATE items SET classified = 0, keep = NULL, tag = NULL, critical = 0, severity = NULL "
        "WHERE kind = 'news' AND classified = 1 AND keep = 0 AND seen_at >= ?",
        (cutoff,),
    ).rowcount

print(f"re-queued {requeued} dropped news items from the last {days} days "
      f"({quarantined} of them were quarantined as unclassifiable)")
