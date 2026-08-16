import hashlib
import logging
from datetime import datetime, timedelta, timezone

from dateutil import parser as date_parser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header
from fastapi.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import feeds
from classifier import classify_batch
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler()

_last_manual_refresh: datetime | None = None
_css_version = hashlib.sha256(open("static/style.css", "rb").read()).hexdigest()[:10]
_favicon_version = hashlib.sha256(open("static/favicon.png", "rb").read()).hexdigest()[:10]


async def poll_and_classify() -> None:
    log.info("polling feeds...")
    items = feeds.fetch_all()
    inserted = db.insert_new_items(items)
    log.info("fetched %d items, %d new", len(items), inserted)

    while True:
        batch = db.get_unclassified(settings.classify_batch_size)
        if not batch:
            break
        results = await classify_batch(batch)
        if not results:
            log.warning("classification failed for a batch of %d, will retry next poll", len(batch))
            break
        db.apply_classifications(results)
        log.info("classified %d items", len(results))


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


@app.get("/")
def index(request: Request):
    items = db.get_curated(limit=300)
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
    })


@app.post("/refresh")
async def refresh(x_admin_token: str = Header(default="")):
    global _last_manual_refresh
    is_admin = bool(settings.admin_token) and x_admin_token == settings.admin_token
    if is_admin or _refresh_available():
        _last_manual_refresh = datetime.now()
        await poll_and_classify()
    return RedirectResponse("/", status_code=303)
