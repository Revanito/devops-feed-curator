import json
import logging
import time
from pathlib import Path

import httpx

import advisories
import ebay
from config import settings

log = logging.getLogger("classifier")

_TIMEOUT = httpx.Timeout(60.0)

# Prompts live as plain text files in app/prompts/ rather than embedded string constants, so
# they're a standalone reference for anyone using this repo as a template, and editable/volume-
# mountable the same way as sources.yaml/deals.yaml - no rebuild needed to tune them.
_PROMPTS_DIR = Path(__file__).parent / "prompts"
_SYSTEM_PROMPT = (_PROMPTS_DIR / "news_system_prompt.txt").read_text(encoding="utf-8").strip()
_DEALS_SYSTEM_PROMPT = (_PROMPTS_DIR / "deals_system_prompt.txt").read_text(encoding="utf-8").strip()
_RAM_SYSTEM_PROMPT = (_PROMPTS_DIR / "ram_system_prompt.txt").read_text(encoding="utf-8").strip()


def _build_user_prompt(batch: list) -> str:
    lines = []
    for i, row in enumerate(batch, start=1):
        summary = (row["summary"] or "").strip().replace("\n", " ")[:300]
        lines.append(f"{i}. [{row['source']}] {row['title']}\n   {summary}")
    return "\n".join(lines)


def _build_deals_user_prompt(batch: list) -> str:
    """Shared by both eBay-backed prompts (mini-PC deals and RAM deals) - same price/shipping/
    condition formatting works for either, since it's just item metadata, not deal-type-specific."""
    lines = []
    for i, row in enumerate(batch, start=1):
        if row["price"]:
            total = row["price"] + (row["shipping"] or 0)
            price = f"{total:.0f} {row['currency']} total (item {row['price']:.0f} + shipping)"
        else:
            price = "price unknown"
        condition = (row["summary"] or "").strip()
        lines.append(f"{i}. {row['title']} - {price}{f', {condition}' if condition else ''}")
    return "\n".join(lines)


async def _classify(system_prompt: str, batch: list, build_prompt, model: str) -> dict[str, dict]:
    if not batch:
        return {}

    prompt = system_prompt + "\n\nItems:\n" + build_prompt(batch)
    url = f"{settings.one_min_base_url}/api/chat-with-ai"
    body = {
        "type": "UNIFY_CHAT_WITH_AI",
        "model": model,
        "promptObject": {"prompt": prompt},
    }
    headers = {"API-KEY": settings.one_min_api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                # Not a model problem - the fallback can't help, every model will get the same answer.
                log.error("1min.ai rejected the API key (HTTP %d) - check ONE_MIN_API_KEY in .env",
                          exc.response.status_code)
            else:
                log.error("1min.ai classify request failed (model %s): %s", model, exc)
            return {}

    try:
        data = resp.json()
    except ValueError as exc:
        log.error("1min.ai response was not valid JSON (%s): %s", exc, resp.text[:2000])
        return {}

    try:
        raw = data["aiRecord"]["aiRecordDetail"]["resultObject"][0]
    except (KeyError, IndexError, TypeError) as exc:
        log.error(
            "1min.ai response missing reply text (%s: %s); top-level keys=%s; full body: %s",
            type(exc).__name__, exc,
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            json.dumps(data)[:4000],
        )
        return {}

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.error("could not parse classifier JSON: %s", raw[:300])
        return {}

    results = {}
    for entry in parsed:
        idx = entry.get("i")
        if not isinstance(idx, int) or not (1 <= idx <= len(batch)):
            continue
        item_id = batch[idx - 1]["id"]
        # The news prompt answers with a severity (critical/high/none) and no "keep": keep follows
        # from it. The deals prompts answer with "keep" and no severity.
        severity = str(entry.get("severity") or "").strip().lower()
        if severity not in ("critical", "high"):
            severity = ""
        keep = bool(entry["keep"]) if "keep" in entry else bool(severity)
        results[item_id] = {
            "keep": keep,
            "tag": str(entry.get("tag", ""))[:30],
            "critical": severity == "critical" or bool(entry.get("critical", False)),
            "severity": severity,
            # cpu/ram/storage are the mini-PC deals fields; capacity/kit are the RAM deals fields.
            # Harmless no-ops for whichever kind's prompt didn't ask for a given field - the LLM
            # just never returns that key, and .get() here defaults it to "".
            "cpu": str(entry.get("cpu") or "")[:60],
            "ram": str(entry.get("ram") or "")[:30],
            "storage": str(entry.get("storage") or "")[:30],
            "capacity": str(entry.get("capacity") or "")[:30],
            "kit": str(entry.get("kit") or "")[:30],
            # Only the RAM prompt asks for this (an English translation of a non-English title) -
            # empty/absent for every other kind, and apply_classifications() leaves the stored
            # title alone when it's empty rather than overwriting it with nothing.
            "title_en": str(entry.get("title_en") or "")[:200],
        }
    return results


def _news_system_prompt() -> str:
    """The news prompt gets stack.yaml's product names filled in, read fresh each call so editing
    the stack applies on the next poll, same as for the KEV/NVD matching."""
    try:
        names = [p.name for p in advisories.load_stack()]
    except Exception as exc:  # noqa: BLE001 - a broken stack.yaml is already flagged on the page
        log.error("could not load stack for the news prompt: %s", exc)
        names = []
    stack = "\n".join(f"- {n}" for n in names) or "- (stack.yaml could not be loaded)"
    return _SYSTEM_PROMPT.replace("{{STACK}}", stack)


async def classify_batch(batch: list, model: str) -> dict[str, dict]:
    return await _classify(_news_system_prompt(), batch, _build_user_prompt, model)


async def classify_deals_batch(batch: list, model: str) -> dict[str, dict]:
    results = await _classify(_DEALS_SYSTEM_PROMPT, batch, _build_deals_user_prompt, model)
    # The LLM sometimes extracts a CPU model correctly (from context beyond the title/aspect text
    # ebay.py's own deterministic gate ever saw - e.g. recognizing "Lenovo M93p" as an i7-4790 vPro
    # box) but doesn't consistently apply its own keep/drop rule to what it just extracted. Re-check
    # deterministically here rather than trust that self-consistency held.
    for r in results.values():
        if r["keep"] and ebay.cpu_disqualify_reason(r.get("cpu") or ""):
            r["keep"] = False
    return results


async def classify_ram_batch(batch: list, model: str) -> dict[str, dict]:
    results = await _classify(_RAM_SYSTEM_PROMPT, batch, _build_deals_user_prompt, model)
    # Same deterministic re-check pattern as classify_deals_batch: re-verify DDR generation
    # against the actual listing title after the fact, rather than trust that the LLM's keep
    # decision and its own "tag" output stayed consistent with what the title actually says.
    titles = {row["id"]: row["title"] for row in batch}
    for item_id, r in results.items():
        if r["keep"] and ebay.ram_disqualify_reason(titles.get(item_id, "")):
            r["keep"] = False
    return results


# A harmless item used to tell "this model is down / rejecting everything" apart from "one item in
# this batch tripped moderation". Has every column any of the prompt builders reads.
_CANARY = {
    "id": "__canary__", "source": "canary", "title": "Docker Engine 27.1 released",
    "summary": "Maintenance release with bug fixes.", "price": None, "shipping": None, "currency": "",
}

# Models whose canary failed recently, with the time they failed - skipped until _MODEL_DOWN_SECONDS
# have passed, so a dead primary doesn't cost two failed calls on every single batch of a poll.
_MODEL_DOWN_SECONDS = 3600
_down_since: dict[str, float] = {}


def _models() -> list[str]:
    """Primary first, then the fallback (if set and different)."""
    models = [settings.model_classifier]
    fallback = settings.model_classifier_fallback.strip()
    if fallback and fallback not in models:
        models.append(fallback)
    return models


def _is_down(model: str) -> bool:
    since = _down_since.get(model)
    if since is None:
        return False
    if time.monotonic() - since >= _MODEL_DOWN_SECONDS:
        del _down_since[model]
        return False
    return True


async def _isolating_failures(classify_fn, batch: list) -> dict[str, dict]:
    """Classify a batch with the primary model, falling back to MODEL_CLASSIFIER_FALLBACK if the
    primary is down (retired model ID, provider outage...).

    When a model fails a batch, a known-harmless canary item is sent to it on its own:
    - canary fails too: the model itself is down - mark it down for an hour, try the next model.
    - canary works: the model is fine and one item in the batch is the problem (e.g. it trips
      1min.ai's content moderation, which rejects the whole batch with no per-item detail) -
      bisect with that model to isolate it. A single item that still fails alone gets
      quarantined (dropped and marked classified) so it stops burning a retry every poll.
    If every model is down, return nothing so the batch stays pending for next poll, instead of
    quarantining every item as dropped."""
    if not batch:
        return {}

    for model in _models():
        if _is_down(model):
            continue
        results = await classify_fn(batch, model)
        if results:
            if model != settings.model_classifier:
                log.warning("classified a batch of %d with fallback model %s", len(batch), model)
            return results

        if not await classify_fn([_CANARY], model):
            log.error("classifier canary failed on model %s - marking it down for %ds",
                      model, _MODEL_DOWN_SECONDS)
            _down_since[model] = time.monotonic()
            continue

        results = await _bisect(classify_fn, batch, model, already_failed=True)
        # Belt and braces: if every single item ended up quarantined, that's an outage that started
        # right after the canary passed, not N separately-bad items - keep them all pending.
        if all(r["tag"] == "unclassifiable" for r in results.values()):
            log.error("every item in a batch of %d failed alone on %s - treating as outage, leaving pending",
                      len(batch), model)
            return {}
        return results

    log.error("all classifier models down (%s) - leaving %d items pending", ", ".join(_models()), len(batch))
    return {}


async def _bisect(classify_fn, batch: list, model: str, already_failed: bool = False) -> dict[str, dict]:
    if not already_failed:
        results = await classify_fn(batch, model)
        if results:
            return results

    if len(batch) == 1:
        item = batch[0]
        retry = await classify_fn(batch, model)
        if retry:
            return retry
        log.warning("quarantining unclassifiable item %s (%s)", item["id"], item["title"][:80])
        return {item["id"]: {"keep": False, "tag": "unclassifiable", "critical": False}}

    mid = len(batch) // 2
    left = await _bisect(classify_fn, batch[:mid], model)
    right = await _bisect(classify_fn, batch[mid:], model)
    return {**left, **right}


async def classify_batch_isolating_failures(batch: list) -> dict[str, dict]:
    return await _isolating_failures(classify_batch, batch)


async def classify_deals_batch_isolating_failures(batch: list) -> dict[str, dict]:
    return await _isolating_failures(classify_deals_batch, batch)


async def classify_ram_batch_isolating_failures(batch: list) -> dict[str, dict]:
    return await _isolating_failures(classify_ram_batch, batch)
