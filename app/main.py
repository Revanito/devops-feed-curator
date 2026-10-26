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

import advisories
import db
import ebay
import feeds
from classifier import (
    classify_batch_isolating_failures,
    classify_deals_batch_isolating_failures,
    classify_ram_batch_isolating_failures,
)
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler()

_last_manual_refresh: datetime | None = None
_css_version = hashlib.sha256(open("static/style.css", "rb").read()).hexdigest()[:10]
_favicon_version = hashlib.sha256(open("static/favicon.png", "rb").read()).hexdigest()[:10]
_news_lock = asyncio.Lock()
_deals_lock = asyncio.Lock()


async def poll_news() -> None:
    """Security news feeds + CISA KEV + NVD, every NEWS_POLL_MINUTES."""
    if _news_lock.locked():
        log.info("news poll already in progress, skipping")
        return
    async with _news_lock:
        log.info("polling feeds...")
        items = await asyncio.to_thread(feeds.fetch_all)
        reddit_items = await asyncio.to_thread(feeds.fetch_reddit)
        items += reddit_items
        # Some feeds keep weeks-old posts. Without this, an item deleted by the age cleanup below
        # gets re-inserted as "new" next poll, re-classified, and shown again forever.
        fresh = [i for i in items if _published_dt(i) is None or _is_recent(i, settings.news_max_age_days)]
        if len(fresh) < len(items):
            log.info("skipped %d feed items older than %d days", len(items) - len(fresh), settings.news_max_age_days)
        items = fresh
        inserted = db.insert_new_items(items)
        log.info("fetched %d items (%d reddit), %d new", len(items), len(reddit_items), inserted)

        deleted = db.delete_old_items(settings.news_max_age_days, kind="news")
        if deleted:
            log.info("deleted %d news items older than %d days", deleted, settings.news_max_age_days)

        # KEV/NVD first and before the LLM: these must land even if every model is down.
        log.info("polling CISA KEV and NVD...")
        found = await asyncio.to_thread(advisories.fetch_all)
        new, escalated = db.upsert_advisories(found)
        log.info("vulnerabilities: %d matched, %d new, %d escalated", len(found), new, escalated)
        db.delete_old_items(settings.advisory_max_age_days, kind="advisory")

        await _classify_pending("news", classify_batch_isolating_failures)


async def poll_deals() -> None:
    """eBay deals, once a day at POLL_HOUR:POLL_MINUTE."""
    if _deals_lock.locked():
        log.info("deals poll already in progress, skipping")
        return
    async with _deals_lock:
        if settings.ebay_client_id and settings.ebay_client_secret:
            await asyncio.to_thread(ebay.refresh_gbp_rate)

        log.info("polling eBay deals...")
        deal_items = await asyncio.to_thread(ebay.fetch_all)
        deal_inserted = db.insert_new_items(deal_items)
        log.info("fetched %d deal listings, %d new", len(deal_items), deal_inserted)

        log.info("polling eBay RAM deals...")
        ram_items = await asyncio.to_thread(ebay.fetch_ram_all)
        ram_inserted = db.insert_new_items(ram_items)
        log.info("fetched %d RAM listings, %d new", len(ram_items), ram_inserted)

        to_recheck = db.get_deals_to_recheck(settings.deal_recheck_batch_size)
        if to_recheck:
            gone_ids, checked_ids = await asyncio.to_thread(ebay.check_availability, to_recheck)
            db.mark_checked(checked_ids)
            db.delete_items(gone_ids)
            log.info("re-checked %d eBay listings, %d no longer available",
                      len(checked_ids), len(gone_ids))

        await _classify_pending("deal", classify_deals_batch_isolating_failures)
        await _classify_pending("ram", classify_ram_batch_isolating_failures)


async def poll_and_classify() -> None:
    await poll_news()
    await poll_deals()


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
        poll_news, "interval",
        minutes=settings.news_poll_minutes, next_run_time=datetime.now(),
    )
    scheduler.add_job(
        poll_deals, "cron",
        hour=settings.poll_hour, minute=settings.poll_minute, next_run_time=datetime.now(),
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


def _published_dt(item) -> datetime | None:
    """Feeds use a mix of RFC 822 and ISO dates, so parse them rather than compare raw strings."""
    published = item["published"]
    if not published:
        return None
    try:
        dt = date_parser.parse(published)
    except (ValueError, TypeError, OverflowError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _is_recent(item, days: int) -> bool:
    dt = _published_dt(item)
    return dt is not None and datetime.now(timezone.utc) - dt <= timedelta(days=days)


def _refresh_available() -> bool:
    if _last_manual_refresh is None:
        return True
    cooldown = timedelta(minutes=settings.refresh_cooldown_minutes)
    return datetime.now() - _last_manual_refresh >= cooldown


def _deals_label() -> str:
    return f"Homelab Deals under {settings.deal_max_price_eur}€"


def _beyond_label() -> str:
    return f"Homelab Deals beyond {settings.deal_max_price_eur}€"


def _ram_label() -> str:
    return f"RAM Deals under {settings.ram_max_price_eur}€"


def _ram_beyond_label() -> str:
    return f"RAM Deals beyond {settings.ram_max_price_eur}€"


def _nav_context() -> dict:
    """Shared nav-button labels, passed to every page's template context."""
    return {
        "deals_label": _deals_label(), "beyond_label": _beyond_label(),
        "ram_label": _ram_label(), "ram_beyond_label": _ram_beyond_label(),
    }


def _source_health() -> tuple[list[dict], list[dict]]:
    """(failing, key) - failing: every source whose last fetch failed; key: KEV/NVD status, always
    shown since they're the backbone. Entries not checked in 2 days are sources that were removed
    from sources.yaml, so they're left out."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    failing, key = [], []
    for name, h in sorted(db.get_source_health().items()):
        try:
            checked = datetime.fromisoformat(h.get("checked_at", ""))
        except ValueError:
            continue
        if checked < cutoff:
            continue
        entry = {"name": name, "ok": h.get("ok"), "error": h.get("error", ""),
                 "last_ok": _ago(h.get("last_ok_at"))}
        if name in (advisories.KEV_SOURCE, advisories.NVD_SOURCE):
            key.append(entry)
        if not h.get("ok"):
            failing.append(entry)
    return failing, key


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    except ValueError:
        return "never"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


@app.get("/")
def index(request: Request):
    items = db.get_security_items(days=settings.news_max_age_days)
    # Red always above orange. Red: actively exploited first. Orange: your stack first, then
    # exploited-elsewhere. Then newest flagged first (the query sorts by seen_at; sorted() is stable).
    critical_items = sorted((i for i in items if i["severity"] == "critical"), key=lambda i: not i["exploited"])
    high_items = sorted((i for i in items if i["severity"] == "high"),
                        key=lambda i: ((i["tag"] or "").lower() == advisories.OTHER_TAG, not i["exploited"]))
    all_tags = sorted({(item["tag"] or "").lower() for item in items if item["tag"]})
    failing_sources, key_sources = _source_health()
    return templates.TemplateResponse("index.html", {
        "request": request, "stats": db.counts(kind="news"), "refresh_available": _refresh_available(),
        "critical_items": critical_items, "high_items": high_items,
        "failing_sources": failing_sources, "key_sources": key_sources,
        "days": settings.news_max_age_days,
        "all_tags": all_tags, "css_version": _css_version, "favicon_version": _favicon_version,
        **_nav_context(),
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
        **_nav_context(),
        "total": n,
        "low_items": low_items, "mid_items": mid_items, "high_items": high_items,
        "low_range": _price_range_label(low_items),
        "mid_range": _price_range_label(mid_items),
        "high_range": _price_range_label(high_items),
    })


@app.get("/deals")
def deals(request: Request):
    items = db.get_curated_deals(limit=300, max_total=settings.deal_max_price_eur, kind="deal")
    return _render_deals(request, _deals_label(), "deals", items)


@app.get("/deals/beyond")
def deals_beyond(request: Request):
    items = db.get_curated_deals(
        limit=300, min_total=settings.deal_max_price_eur, max_total=settings.deal_extended_max_price_eur,
        kind="deal",
    )
    return _render_deals(request, _beyond_label(), "beyond", items)


@app.get("/ram")
def ram_deals(request: Request):
    items = db.get_curated_deals(limit=300, max_total=settings.ram_max_price_eur, kind="ram")
    return _render_deals(request, _ram_label(), "ram", items)


@app.get("/ram/beyond")
def ram_deals_beyond(request: Request):
    items = db.get_curated_deals(
        limit=300, min_total=settings.ram_max_price_eur, max_total=settings.ram_extended_max_price_eur,
        kind="ram",
    )
    return _render_deals(request, _ram_beyond_label(), "ram-beyond", items)


@app.post("/refresh")
async def refresh(background_tasks: BackgroundTasks, x_admin_token: str = Header(default="")):
    global _last_manual_refresh
    is_admin = bool(settings.admin_token) and x_admin_token == settings.admin_token
    if is_admin or _refresh_available():
        _last_manual_refresh = datetime.now()
        background_tasks.add_task(poll_and_classify)
    return RedirectResponse("/", status_code=303)
