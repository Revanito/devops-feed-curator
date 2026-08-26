"""One-off maintenance: wipe stored eBay-backed listings (both mini-PC deals and RAM deals) so the
next poll re-fetches and re-classifies everything from scratch under the current ebay.py/
classifier.py logic. Needed after a change to how deals are priced/parsed/classified, since
insert_new_items() only ever inserts new rows - it never touches ones already in the DB, so old rows
would otherwise keep their stale price/spec data forever.

Run with: docker compose exec feed-curator python reset_deals.py
"""

import db

db.init_db()
with db.get_conn() as conn:
    deleted = conn.execute("DELETE FROM items WHERE kind IN ('deal', 'ram')").rowcount

print(f"cleared {deleted} deal + RAM listings - next poll (or POST /refresh) will re-fetch and "
      f"re-classify everything under the current logic")
