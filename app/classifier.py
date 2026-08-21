import json
import logging

import httpx

from config import settings

log = logging.getLogger("classifier")

_TIMEOUT = httpx.Timeout(60.0)

_SYSTEM_PROMPT = """You are a content filter for a personal IT/DevOps/homelab news feed.

KEEP items that are substantively about:
- self-hosting, homelab builds, Docker/Kubernetes/containers, DevOps tooling and practices, CI/CD,
  Linux/sysadmin, networking, storage/NAS, observability, infrastructure-as-code, open-source server
  software, cloud/on-prem infra, security/hardening for the above.
- IT-admin-relevant world news: major vendor announcements and outages affecting sysadmins (Microsoft,
  Windows Server, Active Directory, Entra ID/Azure, VMware/Broadcom, Google Workspace, AWS/Azure/GCP
  outages), security incidents and breaches with operational impact (CrowdStrike-style outages, major
  CVEs, ransomware campaigns, supply-chain compromises), and significant open-source/infra project news
  (major CVEs or releases in Linux, nginx, OpenSSL, systemd, Docker, Kubernetes, etc).

DROP items that are: "look at this deal / buy this laptop/GPU/router for $X" posts, product marketing
or affiliate content, hardware reviews unrelated to running services (phones, consumer gadgets), memes,
"what should I buy" / shopping-advice threads, off-topic career/salary chat, generic consumer tech news
with no admin/operational substance (phone launches, gaming hardware, app store news), politics unrelated
to IT operations, and low-effort "look what I got in the mail" posts even if the item pictured is a
server (no operational content).

Also flag "critical": true for items describing a major ongoing or recent incident with broad real-world
impact that a sysadmin would want to know about immediately - widespread outages (cloud providers, major
SaaS, CrowdStrike-style crashes), actively-exploited zero-days, major ransomware/breach events, or
similarly big operational news. This should be rare - most kept items are critical: false. Routine
releases, blog posts, and "how I built X" posts are never critical.

Tagging: if an item specifically discloses or reports on one or more CVEs / named vulnerabilities in a
major, widely-deployed product or platform (Microsoft/Windows/Entra ID, the Linux kernel, nginx, Apache,
OpenSSL, Docker, Kubernetes, major cloud providers, etc), use the tag "cve" instead of a generic tag like
"security" - this lets the vulnerability specifically be surfaced separately from general security news.
A generic "N vulnerabilities patched this month" roundup still qualifies as "cve" if it names a major
product family. Don't use "cve" for vague "security incident" items with no actual vulnerability/CVE.

Given a numbered list of items (title + short summary), respond with ONLY a JSON array, one object per
item, in the same order, each shaped exactly like:
{"i": <item number>, "keep": true|false, "critical": true|false, "tag": "<one short lowercase topic tag, e.g. docker, kubernetes, homelab, networking, storage, security, cve, ci-cd, linux, microsoft, outage, news>"}

No prose, no markdown fences, just the JSON array.
"""

_DEALS_SYSTEM_PROMPT = """You are a deal filter for a homelab hardware shopping feed. Each item is a live
eBay listing (title, condition, price in EUR) already pre-filtered to be under the price ceiling - your
job is to judge whether it's an actual match for the hardware being hunted, not whether the price is low.

KEEP an item only if ALL of these hold:
- It is a genuine small-form-factor / mini / micro / tiny desktop PC - e.g. Lenovo ThinkCentre (M7xx/M9xx
  Tiny), HP EliteDesk/ProDesk (Mini/SFF G4 or newer), Dell OptiPlex Micro, Intel NUC, or an equivalent
  mini PC from another brand (Beelink, Minisforum, ASUS PN, etc). NOT a full tower, laptop, all-in-one, or
  bare motherboard/CPU-only listing.
- The CPU is worth listing for homelab use, per this exact reference (mostly about hyperthreading/SMT,
  which matters for VM/container workloads, but with two deliberate exceptions noted below):
  * Intel 7th gen (Kaby Lake): i3 KEEP, i5 DROP (no HT), i7 KEEP
  * Intel 8th gen (Coffee Lake): i3 DROP, i5 DROP, i7 KEEP
  * Intel 9th gen (Coffee Lake Refresh): i3 DROP, i5 DROP, i7 KEEP but note "no hyperthreading" (it's the
    one exception kept anyway - common/cheap enough to still be worth listing, just flagged), i9 KEEP
  * Intel 10th gen (Comet Lake) through 14th gen (Raptor Lake) and Core Ultra Series 1 (Meteor Lake, e.g.
    "Ultra 5 125H"): i3 DROP (excluded by policy regardless of hyperthreading - core counts stay low-end),
    i5/i7/i9 (or Ultra 5/7/9) all KEEP
  * Intel Core Ultra Series 2 (Arrow Lake/Lunar Lake, e.g. "Ultra 9 285K", "Ultra 5 226V"): DROP on every
    tier, no exceptions - even an "Ultra 9" lacks hyperthreading entirely
  * Intel pre-7th-gen (e.g. i7-4770): DROP regardless of tier
  * AMD Ryzen pre-3000-series (1000/2000, Zen/Zen+): Ryzen 3 DROP, Ryzen 5/7 KEEP
  * AMD Ryzen 3000-series (Zen 2) and newer (5000/7000/9000): KEEP virtually every model, including
    Ryzen 3
  When a listing states neither an exact model number nor a clear generation, use your general knowledge
  of the named CPU to judge it against this table.
- The platform is DDR4-based, which is true by default for every model family above from 2018 onward -
  only drop for this reason if the listing explicitly says DDR3.

Prefer, but do not require, listings that explicitly state 32GB RAM already installed - these mini PC
platforms all support DDR4 SO-DIMM upgrades to 32GB regardless, so a lower or unstated RAM amount alone is
NOT a reason to drop an otherwise-matching listing.

Some listings are eBay "choose your configuration" listings with several selectable CPU/RAM/storage
combinations. For those, the item text already includes a line like "Selected configuration: i7-8700T
(6C/12T), 32GB RAM, 120GB SSD (price shown is for this exact configuration)" - a specific configuration
already picked out for you (best CPU available, ~32GB RAM, cheapest storage), with the price given being
that exact configuration's real price, not the listing's cheapest teaser price. Judge the listing using
that selected configuration, and copy its cpu/ram/storage straight into your answer - trust it over the
title if they conflict, since the title often just describes the cheapest option.

For every kept item, extract from the item text (preferring a "Selected configuration" line when present,
else the title/condition text):
- "cpu": the exact CPU model as written (e.g. "i7-8700T"), followed by its core/thread count in parentheses
  if you know it from general knowledge of that model (e.g. "i7-8700T (6C/12T)") and it isn't already
  present. If no CPU model is written anywhere in the text, use "".
- "ram": RAM size as stated, e.g. "16GB". "" if genuinely not stated anywhere.
- "storage": storage size and type as stated, e.g. "256GB SSD". "" if not stated.
Do not invent a ram/storage number that is not written in the text anywhere - only cpu core/thread counts
may be filled in from your own knowledge of that CPU model, since those are fixed facts about a named part.

Given a numbered list of items (title, condition, price), respond with ONLY a JSON array, one object per
item, in the same order, each shaped exactly like:
{"i": <item number>, "keep": true|false, "critical": false, "tag": "<one of: thinkcentre, elitedesk, prodesk, nuc, optiplex, mini-pc>", "cpu": "<string>", "ram": "<string>", "storage": "<string>"}

"critical" is always false here - it's unused for deals but kept for a uniform shape. cpu/ram/storage may
be "" but must always be present. No prose, no markdown fences, just the JSON array.
"""


def _build_user_prompt(batch: list) -> str:
    lines = []
    for i, row in enumerate(batch, start=1):
        summary = (row["summary"] or "").strip().replace("\n", " ")[:300]
        lines.append(f"{i}. [{row['source']}] {row['title']}\n   {summary}")
    return "\n".join(lines)


def _build_deals_user_prompt(batch: list) -> str:
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
            "cpu": str(entry.get("cpu") or "")[:60],
            "ram": str(entry.get("ram") or "")[:30],
            "storage": str(entry.get("storage") or "")[:30],
        }
    return results


async def classify_batch(batch: list) -> dict[str, dict]:
    return await _classify(_SYSTEM_PROMPT, batch, _build_user_prompt)


async def classify_deals_batch(batch: list) -> dict[str, dict]:
    return await _classify(_DEALS_SYSTEM_PROMPT, batch, _build_deals_user_prompt)


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
