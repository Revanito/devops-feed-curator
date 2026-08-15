import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
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


@app.get("/")
def index(request: Request):
    items = db.get_curated(limit=300)
    stats = db.counts()
    reddit_items, blog_items, homelab_items = _split_columns(items)
    return templates.TemplateResponse("index.html", {
        "request": request, "stats": stats,
        "reddit_items": reddit_items, "blog_items": blog_items, "homelab_items": homelab_items,
    })


@app.post("/refresh")
async def refresh(x_admin_token: str = Header(default="")):
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=404)
    await poll_and_classify()
    return RedirectResponse("/", status_code=303)
