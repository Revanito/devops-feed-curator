import json
import logging

import httpx

from config import settings

log = logging.getLogger("classifier")

_TIMEOUT = httpx.Timeout(60.0)

_SYSTEM_PROMPT = """You are a content filter for a personal IT/DevOps/homelab news feed.

KEEP items that are substantively about: self-hosting, homelab builds, Docker/Kubernetes/containers,
DevOps tooling and practices, CI/CD, Linux/sysadmin, networking, storage/NAS, observability,
infrastructure-as-code, open-source server software, cloud/on-prem infra, security/hardening for the above.

DROP items that are: "look at this deal / buy this laptop/GPU/router for $X" posts, product marketing
or affiliate content, hardware reviews unrelated to running services (phones, consumer gadgets), memes,
"what should I buy" / shopping-advice threads, off-topic career/salary chat, general tech news with no
IT/DevOps/homelab substance, politics, and low-effort "look what I got in the mail" posts even if the
item pictured is a server (no operational content).

Given a numbered list of items (title + short summary), respond with ONLY a JSON array, one object per
item, in the same order, each shaped exactly like:
{"i": <item number>, "keep": true|false, "tag": "<one short lowercase topic tag, e.g. docker, kubernetes, homelab, networking, storage, security, ci-cd, linux, news>"}

No prose, no markdown fences, just the JSON array.
"""


def _build_user_prompt(batch: list) -> str:
    lines = []
    for i, row in enumerate(batch, start=1):
        summary = (row["summary"] or "").strip().replace("\n", " ")[:300]
        lines.append(f"{i}. [{row['source']}] {row['title']}\n   {summary}")
    return "\n".join(lines)


async def classify_batch(batch: list) -> dict[str, tuple[bool, str]]:
    if not batch:
        return {}

    prompt = _SYSTEM_PROMPT + "\n\nItems:\n" + _build_user_prompt(batch)
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
        raw = resp.json()["aiRecord"]["aiRecordDetail"]["resultObject"][0]
    except (KeyError, IndexError, TypeError):
        log.error("1min.ai response missing reply text: %s", resp.text[:300])
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
        keep = bool(entry.get("keep", False))
        tag = str(entry.get("tag", ""))[:30]
        results[item_id] = (keep, tag)
    return results
