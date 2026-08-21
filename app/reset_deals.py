"""One-off maintenance: wipe stored eBay deal listings so the next poll re-fetches and
re-classifies everything from scratch under the current ebay.py/classifier.py logic. Needed after
a change to how deals are priced/parsed/classified, since insert_new_items() only ever inserts new
rows - it never touches ones already in the DB, so old rows would otherwise keep their stale price/
spec data forever.

Run with: docker compose exec feed-curator python reset_deals.py
"""

import db

db.init_db()
with db.get_conn() as conn:
    deleted = conn.execute("DELETE FROM items WHERE kind = 'deal'").rowcount

print(f"cleared {deleted} deal listings - next poll (or POST /refresh) will re-fetch and "
      f"re-classify everything under the current logic")
