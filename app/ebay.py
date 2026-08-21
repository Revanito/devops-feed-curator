import base64
import hashlib
import logging
import re
import time

import httpx
import yaml

from config import settings

log = logging.getLogger("ebay")

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_ITEM_BY_LEGACY_ID_URL = "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id"
_ITEMS_BY_GROUP_URL = "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group"
_TIMEOUT = httpx.Timeout(30.0)

_LEGACY_ITEM_ID_RE = re.compile(r"/itm/(\d+)")
_SOURCE_MARKETPLACE_RE = re.compile(r"eBay \((\w+)\)")

# eBay prices listings in the marketplace's home currency - GB listings are GBP, not EUR, so
# searching EBAY_GB with priceCurrency:EUR would just return nothing.
_MARKETPLACE_CURRENCY = {"EBAY_GB": "GBP"}


def _to_eur(amount: float, currency: str) -> float:
    if currency == "GBP":
        return amount * settings.gbp_to_eur_rate
    return amount

# Marketplaces whose listings ship from outside the EU customs union relative to this app's
# France/EU audience - flagged on the card since the buyer may owe import VAT/duty at delivery
# that eBay's search API has no reliable way to estimate up front.
_CROSS_BORDER_MARKETPLACES = {"EBAY_GB"}

# Cached app access token, shared across polls - client-credentials tokens are valid ~2h and
# re-requesting one per search would be both slow and wasteful.
_token: str | None = None
_token_expires_at: float = 0.0


def _load_searches(path: str) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("searches", [])


def _item_id(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()[:16]


def _get_token(client: httpx.Client) -> str | None:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at:
        return _token

    creds = base64.b64encode(f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode()).decode()
    resp = client.post(
        _TOKEN_URL,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
    )
    if resp.status_code != 200:
        log.error("eBay OAuth token request failed (%s): %s", resp.status_code, resp.text[:500])
        return None

    body = resp.json()
    _token = body["access_token"]
    _token_expires_at = time.time() + body.get("expires_in", 7200) - 60
    return _token


# Variation aspects come back under all sorts of names depending on seller/marketplace language -
# match loosely on the aspect *name* rather than requiring an exact string.
_RAM_NAME_RE = re.compile(r"ram|memory|m[ée]moire|arbeitsspeicher", re.IGNORECASE)
_STORAGE_NAME_RE = re.compile(r"storage|ssd|hdd|disque|drive|festplatte|speicherkapazit", re.IGNORECASE)
_CPU_NAME_RE = re.compile(r"cpu|processor|prozessor", re.IGNORECASE)
_CPU_TIER_RE = re.compile(r"\bi[\s-]?([3579])\b", re.IGNORECASE)
_RYZEN_TIER_RE = re.compile(r"ryzen\D{0,4}([3579])", re.IGNORECASE)
# A size with a unit ("32GB", "32 Go", "1TB") or, within an aspect already identified as RAM/
# storage by name, a bare number ("32") - covers every variant the user asked for.
_SIZE_WITH_UNIT_RE = re.compile(r"(\d+)\s*(gb|go|tb|to)\b", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"(\d+)")


_INTEL_MODEL_RE = re.compile(r"\bi([3579])[\s-](\d{3,5})[a-z]{0,2}\b", re.IGNORECASE)
# eBay listings often name the generation in words instead of a model number ("Intel Core
# i5-7th Gen") - a separate pattern since there's no digit-count trick to lean on here.
_INTEL_WORDED_GEN_RE = re.compile(r"\bi([3579])[\s-](\d{1,2})(?:st|nd|rd|th)\s*gen", re.IGNORECASE)
# Core Ultra doesn't use the iN-NNNN scheme at all - "Ultra 5 125H" (Series 1, Meteor Lake) vs
# "Ultra 5 245K" (Series 2, Arrow Lake/Lunar Lake); the leading digit of the 3-digit suffix is
# the series. Captured separately from family since Ultra's HT story doesn't depend on tier at all.
_CORE_ULTRA_RE = re.compile(r"core\s*ultra\s*[579]\s*(\d)\d{2}[a-z]{0,2}\b", re.IGNORECASE)
_RYZEN_MODEL_RE = re.compile(r"ryzen\s*[3579]\s*[\s-]?(\d{4})", re.IGNORECASE)
# HP explicitly encodes chassis generation as "G<n>" right after the model number (EliteDesk/
# ProDesk 400/600/700/800 G1 through G6+) - G1-G3 are pre-2018 DDR3-era Haswell/Ivy Bridge chassis
# regardless of what CPU ended up in them, so this catches listings a CPU-model check would miss.
# The EliteDesk/ProDesk sub-brand name is optional - plenty of listings just say "HP 600 G1"
# without it, so requiring the sub-brand name would miss those entirely.
_HP_GEN_RE = re.compile(r"\bhp\s*(?:elitedesk|prodesk)?\s*[4678]\d{2}\s*g(\d)\b", re.IGNORECASE)
# eBay's condition field is a short standardized string ("New", "Used", "For parts or not
# working", etc) - a "for parts" listing is never a real deal regardless of specs/price.
_NOT_WORKING_RE = re.compile(r"for parts|not working", re.IGNORECASE)


def _not_working(condition: str) -> bool:
    return bool(_NOT_WORKING_RE.search(condition or ""))


# RAM deals: only DDR4/DDR5 qualify - DDR3-or-older is dropped outright regardless of price or
# capacity, since it isn't compatible with the platforms this feed is for. Unlike the CPU gate,
# there's no "keep-warn"/policy nuance here - it's a hard compatibility fact, not a judgment call.
_DDR_GEN_RE = re.compile(r"\bddr\s*([2-5])\b", re.IGNORECASE)
# "2x16GB" / "2 x 16 GB" style kit notation, captured separately from a bare total so the card can
# show the real stick configuration ("2x16GB") rather than just the total ("32GB").
_RAM_KIT_RE = re.compile(r"\b(\d+)\s*x\s*(\d+)\s*gb\b", re.IGNORECASE)
_RAM_CAPACITY_RE = re.compile(r"\b(\d+)\s*gb\b", re.IGNORECASE)


def _ram_generation_reason(text: str) -> str | None:
    m = _DDR_GEN_RE.search(text or "")
    if m and int(m.group(1)) < 4:
        return f"DDR{m.group(1)} (only DDR4/DDR5 accepted)"
    return None


def ram_disqualify_reason(text: str) -> str | None:
    """Public entry point for _ram_generation_reason, for re-validating a listing's title against
    what the RAM classifier decided - same pattern as cpu_disqualify_reason above."""
    return _ram_generation_reason(text)


def _parse_ram_kit(text: str) -> tuple[str, str]:
    """(capacity, kit) display strings, e.g. ("32GB", "2x16GB") for a kit, ("16GB", "") for a
    single stick, ("", "") if neither is stated anywhere."""
    text = text or ""
    m = _RAM_KIT_RE.search(text)
    if m:
        qty, each = int(m.group(1)), int(m.group(2))
        return f"{qty * each}GB", f"{qty}x{each}GB"
    m = _RAM_CAPACITY_RE.search(text)
    if m:
        return f"{m.group(1)}GB", ""
    return "", ""

# Intel Core keep/drop policy by generation+family. This isn't pure hyperthreading fact (see the
# comment on _intel_policy below for the two deliberate exceptions) - it's what's actually worth
# listing for homelab use. Only 7th-9th gen vary in an irregular way (Intel's naming got
# inconsistent about HT during Kaby/Coffee Lake); "keep-warn" means list it but flag the caveat on
# the card since it's still a reasonable cheap option despite lacking HT. Generations not listed
# here (pre-7th) are always dropped. 10th-gen-and-up all follow the same simple rule (see
# _intel_policy) so they're not spelled out per-generation here.
_INTEL_POLICY_BY_GEN = {
    7: {3: "keep", 5: "drop", 7: "keep"},
    8: {3: "drop", 5: "drop", 7: "keep"},
    9: {3: "drop", 5: "drop", 7: "keep-warn", 9: "keep"},
}
_INTEL_NO_HT_WARNING = "no hyperthreading (9th-gen i7)"


def _intel_generation(model_digits: str) -> int:
    """"620" (1st-gen 3-digit models like i7-620) -> 1. "4770" -> 4. "8700" -> 8.
    "10700"/"1165" (5-digit, or 4-digit starting 10-14 for mobile parts like 1165G7) -> 10/11."""
    if len(model_digits) == 3:
        return 1
    if len(model_digits) >= 4 and model_digits[:2] in ("10", "11", "12", "13", "14"):
        return int(model_digits[:2])
    return int(model_digits[0])


def _intel_policy(family: int, gen: int) -> str:
    """"keep", "keep-warn", or "drop" for a given Intel Core family+generation. Two deliberate
    departures from pure hyperthreading fact: 9th-gen i7 lacks HT but is common/cheap enough to
    still list with a caveat rather than exclude outright ("keep-warn"), and i3 is excluded from
    10th gen onward even though it does have HT there - a policy call, not a hyperthreading fact,
    since i3 core counts stay low-end regardless."""
    if gen >= 10:
        return "drop" if family == 3 else "keep"
    entry = _INTEL_POLICY_BY_GEN.get(gen)
    return entry.get(family, "drop") if entry else "drop"


def _intel_drop_reason(family: int, gen: int) -> str:
    if gen < 7:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(gen, "th")
        return f"Intel {gen}{suffix}-gen CPU (older than 7th gen)"
    if family == 3 and gen >= 10:
        return f"Intel Core i3 {gen}th-gen (i3 excluded regardless of hyperthreading)"
    return f"Intel Core i{family} {gen}th-gen (no hyperthreading)"


def _cpu_verdict(text: str) -> tuple[bool | None, str | None, str | None]:
    """(qualifies, disqualify_reason, warning). qualifies is True/False when the text names a
    recognizable CPU, or None when it doesn't (e.g. a listing that only says "Intel Core" with no
    more detail) - callers should treat None as "can't tell from this text", not as a pass.
    warning is a non-disqualifying caveat to surface on the card (currently just 9th-gen i7's
    missing hyperthreading) when qualifies is True."""
    text = text or ""

    m = _CORE_ULTRA_RE.search(text)
    if m:
        if m.group(1) != "1":
            return False, "Intel Core Ultra Series 2 (Arrow Lake/Lunar Lake - no hyperthreading at all)", None
        return True, None, None

    m_worded = _INTEL_WORDED_GEN_RE.search(text)
    m_numbered = None if m_worded else _INTEL_MODEL_RE.search(text)
    m = m_worded or m_numbered
    if m:
        family = int(m.group(1))
        gen = int(m.group(2)) if m is m_worded else _intel_generation(m.group(2))
        policy = _intel_policy(family, gen)
        if policy == "drop":
            return False, _intel_drop_reason(family, gen), None
        return True, None, (_INTEL_NO_HT_WARNING if policy == "keep-warn" else None)

    m = _RYZEN_MODEL_RE.search(text)
    if m:
        series = int(m.group(1)[0]) * 1000
        if series < 3000:
            return False, f"AMD Ryzen {series}-series (pre-Zen 2, limited/no SMT)", None
        # 3000-series (Zen 2) onward: virtually every model has SMT, including most Ryzen 3.
        return True, None, None

    return None, None, None


def _too_old(text: str) -> str | None:
    """Deterministic pre-filter for hardware that doesn't qualify regardless of what the LLM might
    conclude from title text alone. The CPU check (_cpu_verdict) is driven by whether that specific
    generation+family is actually worth listing for homelab use - see _intel_policy - not just
    brand tier or "8th-gen-or-newer". HP's pre-G4 chassis suffix is a separate, simpler check. This
    exists because the LLM alone got it wrong twice: a G1-chassis 2013 ProDesk slipped through on
    generation alone, and a listing titled "i7" whose only real CPU option turned out to be an
    i5-7th-gen slipped through via a multi-variation group (see _pick_best_variation) where the
    picker was trusting the shared title over the variation's own data. Returns a reason string if
    disqualified, else None; text with no recognizable CPU signal at all passes through unaffected
    for the LLM to judge from context."""
    text = text or ""

    qualifies, reason, _ = _cpu_verdict(text)
    if qualifies is False:
        return reason

    m = _HP_GEN_RE.search(text)
    if m and int(m.group(1)) < 4:
        return f"pre-G4 HP chassis ({m.group(0)})"
    return None


def cpu_disqualify_reason(text: str) -> str | None:
    """Public entry point for _too_old, for re-validating a CPU string the classifier extracted
    on its own (from context the deterministic title/aspect scan never saw - e.g. the LLM
    recognizing "Lenovo M93p" implies an i7-4790 even though the title just says "i7 vPro").
    The LLM sometimes gets the extraction right but the keep/drop call wrong; this catches that
    after the fact rather than trusting it silently."""
    return _too_old(text)


def _cpu_tier(text: str) -> int:
    """Higher is better. 0 means no recognizable Intel Core iX / Ryzen tier found."""
    text = text or ""
    m = _CPU_TIER_RE.search(text)
    if m:
        return int(m.group(1))
    m = _RYZEN_TIER_RE.search(text)
    if m:
        return int(m.group(1))
    return 0


def _parse_size_gb(text: str) -> tuple[str, int] | tuple[None, None]:
    """(display string, size in GB) for comparison, e.g. "1TB" -> ("1TB", 1024)."""
    if not text:
        return None, None
    m = _SIZE_WITH_UNIT_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return text.strip(), (n * 1024 if unit in ("tb", "to") else n)
    m = _BARE_NUMBER_RE.search(text)
    if m:
        n = int(m.group(1))
        return f"{n}GB", n
    return None, None


def _variation_specs(v: dict) -> dict:
    cpu = ram = storage = None
    ram_gb = storage_gb = None
    aspects = v.get("localizedAspects", [])

    for aspect in aspects:
        name, value = aspect.get("name", ""), aspect.get("value", "")
        if not value:
            continue
        if _RAM_NAME_RE.search(name):
            ram, ram_gb = _parse_size_gb(value)
        elif _STORAGE_NAME_RE.search(name):
            storage, storage_gb = _parse_size_gb(value)
        elif _CPU_NAME_RE.search(name):
            cpu = value

    # Fallback for listings that bundle everything into one combined aspect instead of
    # separate CPU/RAM/storage ones (e.g. "Configuration: RAM : 32 Go, Disque dur : 256 Go, ...").
    # These rarely name the CPU in the aspect itself (it's usually only in the title, picked up
    # via cpu_tier below) - leave cpu unset here rather than stuffing the raw aspect text into it.
    if cpu is None and ram is None and storage is None:
        combined = " ".join(a.get("value", "") for a in aspects)
        m = re.search(r"ram\D{0,4}(\d+)\s*(gb|go)\b", combined, re.IGNORECASE)
        if m:
            ram, ram_gb = f"{m.group(1)}{m.group(2).upper()}", int(m.group(1))
        m = re.search(r"(?:disque dur|storage|ssd|hdd)\D{0,4}(\d+)\s*(gb|go|tb|to)\b", combined, re.IGNORECASE)
        if m:
            n, unit = int(m.group(1)), m.group(2).lower()
            storage, storage_gb = f"{n}{m.group(2).upper()}", (n * 1024 if unit in ("tb", "to") else n)

    # Deliberately not blending in v["title"] here - it's the same shared listing title on every
    # variation, so a generically-optimistic title ("i7") would inflate every configuration's
    # verdict equally, defeating the point of judging them against each other. Only a variation's
    # own aspect data should count towards its own verdict.
    cpu_qualifies, _, cpu_warning = _cpu_verdict(cpu or "")

    price = v.get("price", {})
    return {
        "cpu": cpu, "cpu_tier": _cpu_tier(cpu or ""),
        "cpu_qualifies": cpu_qualifies, "cpu_warning": cpu_warning,
        "ram": ram, "ram_gb": ram_gb,
        "storage": storage, "storage_gb": storage_gb,
        "price": float(price["value"]) if price.get("value") else None,
        "currency": price.get("currency"),
        "shipping": _shipping_cost(v),
    }


def _pick_best_variation(variations: list[dict]) -> dict | None:
    """Among a listing's selectable configurations, deterministically pick the one worth
    reporting: a variation that passes the hyperthreading/generation bar (see _cpu_verdict), best
    CPU tier among those, then RAM as close to 32GB as possible (exact match preferred), then
    cheapest storage as the tiebreak since storage matters least. Returns that variation's own
    price - not the listing's cheapest-teaser price, which is often a lesser config than anyone
    actually wants. Returns None if variations exist but none of them qualify - the caller should
    treat that as the whole listing not qualifying, not fall back to the listing's shared title."""
    candidates = [v for v in variations if v["price"] is not None]
    if not candidates:
        return None

    known = [v for v in candidates if v["cpu_qualifies"] is not None]
    if known:
        # At least one variation states its own CPU explicitly - trust that over the shared
        # listing title (identical across every variation, so it can't distinguish between them,
        # and can overstate what a specific configuration actually offers - a listing titled "i7"
        # whose only real CPU option turned out to be an i5-7th-gen is exactly this case). If none
        # of the ones that state a CPU actually pass the hyperthreading bar, none of them qualify.
        qualifying = [v for v in known if v["cpu_qualifies"]]
        if not qualifying:
            return None
        best_cpu_tier = max(v["cpu_tier"] for v in qualifying)
        pool = [v for v in qualifying if v["cpu_tier"] == best_cpu_tier]
    else:
        # No variation states its own CPU at all (e.g. only RAM/storage vary; CPU is fixed and
        # only named in the shared title) - nothing to rank or verify by here, so keep every
        # candidate and let the title carry the CPU judgment downstream, same as a non-variation
        # listing.
        pool = candidates

    exact_32 = [v for v in pool if v["ram_gb"] == 32]
    if exact_32:
        pool = exact_32
    else:
        ram_values = [v["ram_gb"] for v in pool if v["ram_gb"] is not None]
        if ram_values:
            pool = [v for v in pool if v["ram_gb"] == max(ram_values)]

    pool.sort(key=lambda v: (v["storage_gb"] if v["storage_gb"] is not None else float("inf"), v["price"]))
    return pool[0]


def _resolve_group(client: httpx.Client, token: str, group_href: str, cache: dict) -> dict | None:
    """Multi-variation ('choose your configuration') listings show one price/title in search
    results - the cheapest variation - and hide the actual RAM/storage/CPU options behind a
    dropdown the search API never returns. Fetching the item group once per unique listing gets
    every variation's own price and specs, so we can pick and report the specific configuration
    that matches what's actually wanted, at its own real price."""
    if group_href in cache:
        return cache[group_href]

    result = None
    try:
        resp = client.get(group_href, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            raw_variations = resp.json().get("items", [])
            parsed = [_variation_specs(v) for v in raw_variations]
            chosen = _pick_best_variation(parsed)
            if chosen:
                result = {"chosen": chosen, "variation_count": len(raw_variations)}
        else:
            log.warning("eBay item group fetch failed (%s): %s", resp.status_code, resp.text[:300])
    except httpx.HTTPError as exc:
        log.warning("eBay item group fetch errored: %s", exc)

    cache[group_href] = result
    return result


def _search(client: httpx.Client, token: str, marketplace: str, keywords: str, max_price_eur: int) -> list[dict]:
    currency = _MARKETPLACE_CURRENCY.get(marketplace, "EUR")
    resp = client.get(
        _SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
        },
        params={
            "q": keywords,
            "filter": f"price:[..{max_price_eur}],priceCurrency:{currency},buyingOptions:{{FIXED_PRICE}}",
            "sort": "newlyListed",
            "limit": "25",
        },
    )
    if resp.status_code != 200:
        log.warning("eBay search failed on %s for %r (%s): %s", marketplace, keywords, resp.status_code, resp.text[:300])
        return []
    return resp.json().get("itemSummaries", [])


def _shipping_cost(raw: dict) -> float | None:
    """Cheapest listed shipping option's cost, or None if unstated/only known at checkout
    (CALCULATED shipping, which depends on the buyer's address that we don't have here)."""
    for option in raw.get("shippingOptions", []):
        if option.get("shippingCostType") != "FIXED":
            continue
        cost = option.get("shippingCost", {})
        if cost.get("value") is not None:
            return float(cost["value"])
    return None


def fetch_all() -> list[dict]:
    if not settings.ebay_client_id or not settings.ebay_client_secret:
        return []

    marketplaces = [m.strip() for m in settings.ebay_marketplaces.split(",") if m.strip()]
    items = []
    group_cache: dict[str, dict | None] = {}
    with httpx.Client(timeout=_TIMEOUT) as client:
        token = _get_token(client)
        if not token:
            return []

        for search in _load_searches(settings.deals_file):
            for marketplace in marketplaces:
                for raw in _search(client, token, marketplace, search["keywords"], settings.deal_extended_max_price_eur):
                    link = raw.get("itemWebUrl", "")
                    price = raw.get("price", {})
                    condition = raw.get("condition", "")
                    title = raw.get("title", "(no title)").strip()
                    if not link or not price.get("value"):
                        continue
                    if _too_old(title):
                        continue
                    if _not_working(condition):
                        continue

                    # For a multi-variation listing, resolve which specific configuration is
                    # worth reporting (best CPU, ~32GB RAM, cheapest storage) and use *that* SKU's
                    # own price - not the listing's cheapest-teaser price, which is often a lesser
                    # config than what anyone actually wants and would otherwise get shown as if
                    # it were the real price.
                    group_href = raw.get("itemGroupHref")
                    if group_href:
                        resolved = _resolve_group(client, token, group_href, group_cache)
                        if not resolved:
                            # Either the group fetch failed, or none of its variations meet the
                            # CPU bar (_pick_best_variation found only i5-or-below options, say) -
                            # for a listing we know has multiple configurations, don't fall back
                            # to trusting the shared title on its own; skip it entirely.
                            continue
                        chosen = resolved["chosen"]
                    else:
                        chosen = None

                    if chosen and chosen["price"] is not None:
                        # The chosen variation's own aspect text can name a CPU model the title
                        # didn't (or vice versa) - re-check now that we have it.
                        if _too_old(chosen["cpu"] or ""):
                            continue
                        item_price = chosen["price"]
                        native_currency = chosen["currency"] or price.get("currency", "EUR")
                        shipping = chosen["shipping"] if chosen["shipping"] is not None else _shipping_cost(raw)
                        cpu_hint, ram_hint, storage_hint = chosen["cpu"], chosen["ram"], chosen["storage"]
                        cpu_warning = chosen["cpu_warning"]
                    else:
                        item_price = float(price["value"])
                        native_currency = price.get("currency", "EUR")
                        shipping = _shipping_cost(raw)
                        cpu_hint = ram_hint = storage_hint = None
                        _, _, cpu_warning = _cpu_verdict(title)

                    price_eur = _to_eur(item_price, native_currency)
                    shipping_eur = _to_eur(shipping, native_currency) if shipping is not None else None
                    total_eur = price_eur + (shipping_eur or 0.0)
                    # The search API's own price filter only looks at the cheapest variation's
                    # item price, not shipping, and can't filter across currencies - re-check the
                    # true EUR-equivalent total of the actual chosen configuration here. Listings
                    # over DEAL_MAX_PRICE_EUR but still under the extended ceiling are kept (they
                    # show up on the separate "beyond budget" page); only genuinely irrelevant,
                    # far-over-budget listings get dropped here.
                    if total_eur > settings.deal_extended_max_price_eur:
                        continue

                    summary = condition
                    if cpu_hint or ram_hint or storage_hint:
                        picked = ", ".join(x for x in [cpu_hint, f"{ram_hint} RAM" if ram_hint else None, storage_hint] if x)
                        # Some sellers run storage as a third, independent selector eBay's own
                        # variation-group data doesn't capture (only CPU x RAM show up as priced
                        # SKUs) - when that happens don't claim the price accounts for a storage
                        # size we never actually resolved.
                        note = ("price shown is for this exact configuration" if storage_hint else
                                "price may not include storage - size wasn't resolved for this configuration, check listing")
                        summary = f"Selected configuration: {picked} ({note}). {summary}".strip()
                    if shipping:
                        summary = f"{summary} | +{shipping:.0f} {native_currency} shipping".strip(" |")
                    elif shipping is None:
                        summary = f"{summary} | shipping cost shown at checkout".strip(" |")
                    if native_currency != "EUR":
                        summary = (f"{summary} | converted from {item_price + (shipping or 0):.0f} "
                                   f"{native_currency} at ~{settings.gbp_to_eur_rate:.2f} EUR/GBP").strip(" |")
                    if marketplace in _CROSS_BORDER_MARKETPLACES:
                        summary = f"{summary} | ships from UK - possible import VAT/duty on delivery".strip(" |")
                    if cpu_warning:
                        summary = f"{summary} | {cpu_warning}".strip(" |")

                    items.append({
                        "id": _item_id(link),
                        "source": f"eBay ({marketplace.removeprefix('EBAY_')})",
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "published": raw.get("itemCreationDate", ""),
                        "price": price_eur,
                        "shipping": shipping_eur,
                        "currency": "EUR",
                        "kind": "deal",
                    })
    return items


def fetch_ram_all() -> list[dict]:
    """RAM deals reuse the same fetch/pricing/currency/availability machinery as the mini-PC
    pipeline above, but deliberately skip multi-variation ("choose your capacity") resolution -
    that machinery exists to pick the best *CPU*, which has no analogue here. A listing offering
    several capacities as one eBay variation group is reported using its base search-result price
    and title, same as any other simple listing; the price/capacity pairing for such a listing may
    not line up precisely (same class of caveat as the storage-not-resolved note on mini-PC deals)."""
    if not settings.ebay_client_id or not settings.ebay_client_secret:
        return []

    marketplaces = [m.strip() for m in settings.ebay_marketplaces.split(",") if m.strip()]
    items = []
    with httpx.Client(timeout=_TIMEOUT) as client:
        token = _get_token(client)
        if not token:
            return []

        for search in _load_searches(settings.ram_deals_file):
            for marketplace in marketplaces:
                for raw in _search(client, token, marketplace, search["keywords"], settings.ram_extended_max_price_eur):
                    link = raw.get("itemWebUrl", "")
                    price = raw.get("price", {})
                    condition = raw.get("condition", "")
                    title = raw.get("title", "(no title)").strip()
                    if not link or not price.get("value"):
                        continue
                    if _not_working(condition):
                        continue
                    if _ram_generation_reason(title):
                        continue

                    native_currency = price.get("currency", "EUR")
                    item_price = float(price["value"])
                    shipping = _shipping_cost(raw)

                    price_eur = _to_eur(item_price, native_currency)
                    shipping_eur = _to_eur(shipping, native_currency) if shipping is not None else None
                    total_eur = price_eur + (shipping_eur or 0.0)
                    if total_eur > settings.ram_extended_max_price_eur:
                        continue

                    summary = condition
                    capacity, kit = _parse_ram_kit(title)
                    if capacity:
                        parsed = f"{kit} ({capacity} total)" if kit else capacity
                        summary = f"Parsed capacity: {parsed}. {summary}".strip()
                    if shipping:
                        summary = f"{summary} | +{shipping:.0f} {native_currency} shipping".strip(" |")
                    elif shipping is None:
                        summary = f"{summary} | shipping cost shown at checkout".strip(" |")
                    if native_currency != "EUR":
                        summary = (f"{summary} | converted from {item_price + (shipping or 0):.0f} "
                                   f"{native_currency} at ~{settings.gbp_to_eur_rate:.2f} EUR/GBP").strip(" |")
                    if marketplace in _CROSS_BORDER_MARKETPLACES:
                        summary = f"{summary} | ships from UK - possible import VAT/duty on delivery".strip(" |")

                    items.append({
                        "id": _item_id(link),
                        "source": f"eBay ({marketplace.removeprefix('EBAY_')})",
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "published": raw.get("itemCreationDate", ""),
                        "price": price_eur,
                        "shipping": shipping_eur,
                        "currency": "EUR",
                        "kind": "ram",
                    })
    return items


def _marketplace_from_source(source: str) -> str:
    m = _SOURCE_MARKETPLACE_RE.search(source or "")
    return f"EBAY_{m.group(1)}" if m else "EBAY_FR"


def check_availability(rows: list) -> tuple[list[str], list[str]]:
    """Re-checks previously-kept deal listings against eBay to catch ones that have sold or been
    taken down since we last saw them - nothing else does this, so without it a sold listing would
    just sit on the page forever. Returns (ids no longer available, ids successfully checked -
    including still-available ones - so the caller can update last_checked_at on all of them)."""
    if not settings.ebay_client_id or not settings.ebay_client_secret:
        return [], []

    gone, checked = [], []
    with httpx.Client(timeout=_TIMEOUT) as client:
        token = _get_token(client)
        if not token:
            return [], []

        for row in rows:
            m = _LEGACY_ITEM_ID_RE.search(row["link"] or "")
            if not m:
                checked.append(row["id"])
                continue

            headers = {
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": _marketplace_from_source(row["source"]),
            }
            try:
                resp = client.get(_ITEM_BY_LEGACY_ID_URL, headers=headers, params={"legacy_item_id": m.group(1)})
                if resp.status_code == 404:
                    gone.append(row["id"])
                elif resp.status_code == 200:
                    availabilities = resp.json().get("estimatedAvailabilities", [])
                    if any(a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK" for a in availabilities):
                        gone.append(row["id"])
                elif resp.status_code == 400 and "get_items_by_item_group" in resp.text:
                    # This id turned out to be a multi-variation group id, not a single item's
                    # legacy id (eBay's error message says as much and names the right endpoint) -
                    # a listing we resolved to one specific configuration at insert time. Falling
                    # back to that endpoint: any variation still listed means the group is still
                    # live, so treat that as available rather than a hard "gone".
                    group_resp = client.get(_ITEMS_BY_GROUP_URL, headers=headers, params={"item_group_id": m.group(1)})
                    if group_resp.status_code == 404:
                        gone.append(row["id"])
                    elif group_resp.status_code == 200:
                        if not group_resp.json().get("items"):
                            gone.append(row["id"])
                    else:
                        log.warning("eBay group availability check failed for %s (%s): %s",
                                    row["id"], group_resp.status_code, group_resp.text[:200])
                else:
                    log.warning("eBay availability check failed for %s (%s): %s",
                                row["id"], resp.status_code, resp.text[:200])
            except httpx.HTTPError as exc:
                log.warning("eBay availability check errored for %s: %s", row["id"], exc)

            checked.append(row["id"])
    return gone, checked
