import json
import logging
from pathlib import Path

import httpx

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


async def _classify(system_prompt: str, batch: list, build_prompt) -> dict[str, dict]:
    if not batch:
        return {}

    prompt = system_prompt + "\n\nItems:\n" + build_prompt(batch)
    url = f"{settings.one_min_base_url}/api/chat-with-ai"
    body = {
        "type": "UNIFY_CHAT_WITH_AI",
        "model": settings.model_classifier,
        "promptObject": {"prompt": prompt},
    }
    headers = {"API-KEY": settings.one_min_api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("1min.ai classify request failed: %s", exc)
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
        results[item_id] = {
            "keep": bool(entry.get("keep", False)),
            "tag": str(entry.get("tag", ""))[:30],
            "critical": bool(entry.get("critical", False)),
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


async def classify_batch(batch: list) -> dict[str, dict]:
    return await _classify(_SYSTEM_PROMPT, batch, _build_user_prompt)


async def classify_deals_batch(batch: list) -> dict[str, dict]:
    results = await _classify(_DEALS_SYSTEM_PROMPT, batch, _build_deals_user_prompt)
    # The LLM sometimes extracts a CPU model correctly (from context beyond the title/aspect text
    # ebay.py's own deterministic gate ever saw - e.g. recognizing "Lenovo M93p" as an i7-4790 vPro
    # box) but doesn't consistently apply its own keep/drop rule to what it just extracted. Re-check
    # deterministically here rather than trust that self-consistency held.
    for r in results.values():
        if r["keep"] and ebay.cpu_disqualify_reason(r.get("cpu") or ""):
            r["keep"] = False
    return results


async def _isolating_failures(classify_fn, batch: list) -> dict[str, dict]:
    """Classify a batch, bisecting on failure so one bad item (e.g. one that trips 1min.ai's
    own content moderation and gets the whole batch rejected with no per-item detail) can't
    block every other item in it. A single item that still fails alone gets quarantined -
    dropped and marked classified - so it stops burning a retry every poll forever."""
    if not batch:
        return {}

    results = await classify_fn(batch)
    if results:
        return results

    if len(batch) == 1:
        item = batch[0]
        retry = await classify_fn(batch)
        if retry:
            return retry
        log.warning("quarantining unclassifiable item %s (%s)", item["id"], item["title"][:80])
        return {item["id"]: {"keep": False, "tag": "unclassifiable", "critical": False}}

    mid = len(batch) // 2
    left = await _isolating_failures(classify_fn, batch[:mid])
    right = await _isolating_failures(classify_fn, batch[mid:])
    return {**left, **right}


async def classify_batch_isolating_failures(batch: list) -> dict[str, dict]:
    return await _isolating_failures(classify_batch, batch)


async def classify_deals_batch_isolating_failures(batch: list) -> dict[str, dict]:
    return await _isolating_failures(classify_deals_batch, batch)


async def classify_ram_batch(batch: list) -> dict[str, dict]:
    results = await _classify(_RAM_SYSTEM_PROMPT, batch, _build_deals_user_prompt)
    # Same deterministic re-check pattern as classify_deals_batch: re-verify DDR generation
    # against the actual listing title after the fact, rather than trust that the LLM's keep
    # decision and its own "tag" output stayed consistent with what the title actually says.
    titles = {row["id"]: row["title"] for row in batch}
    for item_id, r in results.items():
        if r["keep"] and ebay.ram_disqualify_reason(titles.get(item_id, "")):
            r["keep"] = False
    return results


async def classify_ram_batch_isolating_failures(batch: list) -> dict[str, dict]:
    return await _isolating_failures(classify_ram_batch, batch)
