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
- The CPU is Intel Core i7 (or i9) 8th generation or newer (model numbers like i7-8700T, i7-9700, i7-10700,
  i7-1165G7, etc - the digit(s) right after "i7-" must be 8 or higher, or a 4-digit model starting 10/11/
  12/13/14), OR the AMD equivalent: Ryzen 5/7/9 from the 3000 series or newer. Anything older (i7-7xxx,
  i5, i3, Ryzen 2000-series or older) is a DROP even if cheap.
- The platform is DDR4-based, which is true by default for every model family listed above from 2018
  onward - only drop for this reason if the listing explicitly says DDR3.

Prefer, but do not require, listings that explicitly state 32GB RAM already installed - these mini PC
platforms all support DDR4 SO-DIMM upgrades to 32GB regardless, so a lower or unstated RAM amount alone is
NOT a reason to drop an otherwise-matching listing.

Some listings are eBay "choose your configuration" listings: the title/price shown is just the cheapest
option, and the item text may include a line like "Configurations: RAM : 8 Go, Disque dur : 128 Go, ... |
RAM : 16 Go, Disque dur : 256 Go, ... | ..." (French/German labels - RAM/Arbeitsspeicher, Disque dur/
Festplatte = storage) listing every selectable combination, sometimes with "price range X-Y EUR". Treat
that the same as title text for extraction purposes.

For every kept item, also extract from the title and condition/configuration text:
- "cpu": the exact CPU model as written (e.g. "i7-8700T"), followed by its core/thread count in parentheses
  if you know it from general knowledge of that model (e.g. "i7-8700T (6C/12T)"). If no CPU model is
  written anywhere in the text, use "".
- "ram": RAM size(s) as stated. A single listing: "16GB". A configurable listing with several distinct RAM
  values across its configurations: a compact range or slash list, e.g. "8-32GB" or "8GB/16GB/32GB". ""
  if genuinely not stated anywhere.
- "storage": storage size(s) and type as stated, same range/slash-list rule as ram, e.g. "256GB SSD" or
  "128GB-1TB SSD". "" if not stated.
Do not invent ram/storage numbers that are not written in the text anywhere - only cpu core/thread counts
may be filled in from your own knowledge of that CPU model, since those are fixed facts about a named part.
If a configurable listing's price range extends noticeably above the price shown for this item, mention
that by keeping storage/ram as the full range rather than just the cheapest configuration's numbers - the
reader needs to know what's actually achievable, not just what the cheapest option is.

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
