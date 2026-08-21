import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from math import ceil

from dateutil import parser as date_parser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, Header
from fastapi.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import ebay
import feeds
from classifier import classify_batch_isolating_failures, classify_deals_batch_isolating_failures
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler()

_last_manual_refresh: datetime | None = None
_css_version = hashlib.sha256(open("static/style.css", "rb").read()).hexdigest()[:10]
_favicon_version = hashlib.sha256(open("static/favicon.png", "rb").read()).hexdigest()[:10]
_poll_lock = asyncio.Lock()


async def poll_and_classify() -> None:
    if _poll_lock.locked():
        log.info("poll already in progress, skipping")
        return
    async with _poll_lock:
        log.info("polling feeds...")
        items = await asyncio.to_thread(feeds.fetch_all)
        inserted = db.insert_new_items(items)
        log.info("fetched %d items, %d new", len(items), inserted)

        log.info("polling eBay deals...")
        deal_items = await asyncio.to_thread(ebay.fetch_all)
        deal_inserted = db.insert_new_items(deal_items)
        log.info("fetched %d deal listings, %d new", len(deal_items), deal_inserted)

        to_recheck = db.get_deals_to_recheck(settings.deal_recheck_batch_size)
        if to_recheck:
            gone_ids, checked_ids = await asyncio.to_thread(ebay.check_availability, to_recheck)
            db.mark_checked(checked_ids)
            db.delete_items(gone_ids)
            log.info("re-checked %d deal listings, %d no longer available on eBay",
                      len(checked_ids), len(gone_ids))

        await _classify_pending("news", classify_batch_isolating_failures)
        await _classify_pending("deal", classify_deals_batch_isolating_failures)


async def _classify_pending(kind: str, classify_fn) -> None:
    while True:
        batch = db.get_unclassified(settings.classify_batch_size, kind=kind)
        if not batch:
            break
        results = await classify_fn(batch)
        if not results:
            log.warning("classification failed for a %s batch of %d, will retry next poll", kind, len(batch))
            break
        db.apply_classifications(results)
        quarantined = len(batch) - len(results)
        log.info("classified %d %s items%s", len(results), kind,
                  f" ({quarantined} quarantined as unclassifiable)" if quarantined else "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.add_job(
        poll_and_classify, "interval",
        minutes=settings.poll_interval_minutes, next_run_time=datetime.now(),
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


def _split_columns(items: list) -> tuple[list, list, list]:
    reddit, blogs, homelab = [], [], []
    for item in items:
        if (item["tag"] or "").lower() == "homelab":
            homelab.append(item)
        elif item["source"].startswith("r/"):
            reddit.append(item)
        else:
            blogs.append(item)
    return reddit, blogs, homelab


def _is_recent(item, days: int) -> bool:
    published = item["published"]
    if not published:
        return False
    try:
        dt = date_parser.parse(published)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt <= timedelta(days=days)


def _refresh_available() -> bool:
    if _last_manual_refresh is None:
        return True
    cooldown = timedelta(minutes=settings.refresh_cooldown_minutes)
    return datetime.now() - _last_manual_refresh >= cooldown


def _deals_label() -> str:
    return f"Homelab Deals under {settings.deal_max_price_eur}€"


def _beyond_label() -> str:
    return f"Homelab Deals beyond {settings.deal_max_price_eur}€"


@app.get("/")
def index(request: Request):
    items = db.get_curated(limit=300, kind="news")
    stats = db.counts()
    reddit_items, blog_items, homelab_items = _split_columns(items)
    must_read_items = [item for item in items if item["critical"] and _is_recent(item, days=7)][:8]
    cve_items = [item for item in items if (item["tag"] or "").lower() == "cve"][:3]
    all_tags = sorted({(item["tag"] or "").lower() for item in items if item["tag"]})
    return templates.TemplateResponse("index.html", {
        "request": request, "stats": stats, "refresh_available": _refresh_available(),
        "must_read_items": must_read_items, "cve_items": cve_items,
        "all_tags": all_tags, "css_version": _css_version, "favicon_version": _favicon_version,
        "reddit_items": reddit_items, "blog_items": blog_items, "homelab_items": homelab_items,
        "deals_label": _deals_label(), "beyond_label": _beyond_label(),
    })


def _price_range_label(items: list) -> str:
    if not items:
        return ""
    totals = [item["price"] + (item["shipping"] or 0) for item in items]
    lo, hi = min(totals), max(totals)
    currency = items[0]["currency"] or "EUR"
    if round(lo) == round(hi):
        return f"~{lo:.0f} {currency}"
    return f"{lo:.0f}–{hi:.0f} {currency}"


def _render_deals(request: Request, title: str, active: str, items: list):
    n = len(items)
    third = ceil(n / 3)
    low_items, mid_items, high_items = items[:third], items[third:2 * third], items[2 * third:]
    return templates.TemplateResponse("deals.html", {
        "request": request, "refresh_available": _refresh_available(),
        "css_version": _css_version, "favicon_version": _favicon_version,
        "page_title": title, "active_nav": active,
        "deals_label": _deals_label(), "beyond_label": _beyond_label(),
        "total": n,
        "low_items": low_items, "mid_items": mid_items, "high_items": high_items,
        "low_range": _price_range_label(low_items),
        "mid_range": _price_range_label(mid_items),
        "high_range": _price_range_label(high_items),
    })


@app.get("/deals")
def deals(request: Request):
    items = db.get_curated_deals(limit=300, max_total=settings.deal_max_price_eur)
    return _render_deals(request, _deals_label(), "deals", items)


@app.get("/deals/beyond")
def deals_beyond(request: Request):
    items = db.get_curated_deals(
        limit=300, min_total=settings.deal_max_price_eur, max_total=settings.deal_extended_max_price_eur,
    )
    return _render_deals(request, _beyond_label(), "beyond", items)


@app.post("/refresh")
async def refresh(background_tasks: BackgroundTasks, x_admin_token: str = Header(default="")):
    global _last_manual_refresh
    is_admin = bool(settings.admin_token) and x_admin_token == settings.admin_token
    if is_admin or _refresh_available():
        _last_manual_refresh = datetime.now()
        background_tasks.add_task(poll_and_classify)
    return RedirectResponse("/", status_code=303)
