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
_TIMEOUT = httpx.Timeout(30.0)

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


def _load_searches() -> list[dict]:
    with open(settings.deals_file) as f:
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


_INTEL_MODEL_RE = re.compile(r"\bi[3579][\s-](\d{3,5})[a-z]{0,2}\b", re.IGNORECASE)
_RYZEN_MODEL_RE = re.compile(r"ryzen\s*[3579]\s*[\s-]?(\d{4})", re.IGNORECASE)
# HP explicitly encodes chassis generation as "G<n>" right after the model number (EliteDesk/
# ProDesk 800/600/400 G1 through G6+) - G1-G3 are pre-2018 DDR3-era Haswell/Ivy Bridge chassis
# regardless of what CPU ended up in them, so this catches listings a CPU-model check would miss.
_HP_GEN_RE = re.compile(r"\b(?:elitedesk|prodesk)\s*\d{3}\s*g(\d)\b", re.IGNORECASE)


def _intel_generation(model_digits: str) -> int:
    """"620" (1st-gen 3-digit models like i7-620) -> 1. "4770" -> 4. "8700" -> 8.
    "10700"/"1165" (5-digit, or 4-digit starting 10-14 for mobile parts like 1165G7) -> 10/11."""
    if len(model_digits) == 3:
        return 1
    if len(model_digits) >= 4 and model_digits[:2] in ("10", "11", "12", "13", "14"):
        return int(model_digits[:2])
    return int(model_digits[0])


def _too_old(text: str) -> str | None:
    """Deterministic pre-filter for obviously pre-2018/DDR3-era hardware, based on the CPU model
    number or HP's chassis generation suffix when either is stated in the text - cheaper and more
    reliable than trusting the LLM to always catch this from title text alone (it doesn't always -
    a G1-chassis ProDesk from 2013 slipped through once). Returns a reason string if too old, else
    None; a listing with neither signal present passes through unaffected for the LLM to judge."""
    text = text or ""
    m = _INTEL_MODEL_RE.search(text)
    if m:
        gen = _intel_generation(m.group(1))
        if gen < 8:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(gen, "th")
            return f"Intel {gen}{suffix}-gen CPU"
    m = _RYZEN_MODEL_RE.search(text)
    if m:
        series = int(m.group(1)[0]) * 1000
        if series < 3000:
            return f"AMD Ryzen {series}-series"
    m = _HP_GEN_RE.search(text)
    if m and int(m.group(1)) < 4:
        return f"pre-G4 HP chassis ({m.group(0)})"
    return None


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

    price = v.get("price", {})
    return {
        "cpu": cpu, "cpu_tier": _cpu_tier((cpu or "") + " " + v.get("title", "")),
        "ram": ram, "ram_gb": ram_gb,
        "storage": storage, "storage_gb": storage_gb,
        "price": float(price["value"]) if price.get("value") else None,
        "currency": price.get("currency"),
        "shipping": _shipping_cost(v),
    }


def _pick_best_variation(variations: list[dict]) -> dict | None:
    """Among a listing's selectable configurations, deterministically pick the one worth
    reporting: best available CPU tier, then RAM as close to 32GB as possible (exact match
    preferred), then cheapest storage as the tiebreak since storage matters least. Returns that
    variation's own price - not the listing's cheapest-teaser price, which is often a lesser
    config than what anyone actually wants."""
    candidates = [v for v in variations if v["price"] is not None]
    if not candidates:
        return None

    best_cpu_tier = max(v["cpu_tier"] for v in candidates)
    pool = [v for v in candidates if v["cpu_tier"] == best_cpu_tier]

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


def _search(client: httpx.Client, token: str, marketplace: str, keywords: str) -> list[dict]:
    currency = _MARKETPLACE_CURRENCY.get(marketplace, "EUR")
    resp = client.get(
        _SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
        },
        params={
            "q": keywords,
            "filter": f"price:[..{settings.deal_extended_max_price_eur}],priceCurrency:{currency},buyingOptions:{{FIXED_PRICE}}",
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

        for search in _load_searches():
            for marketplace in marketplaces:
                for raw in _search(client, token, marketplace, search["keywords"]):
                    link = raw.get("itemWebUrl", "")
                    price = raw.get("price", {})
                    condition = raw.get("condition", "")
                    title = raw.get("title", "(no title)").strip()
                    if not link or not price.get("value"):
                        continue
                    if _too_old(title):
                        continue

                    # For a multi-variation listing, resolve which specific configuration is
                    # worth reporting (best CPU, ~32GB RAM, cheapest storage) and use *that* SKU's
                    # own price - not the listing's cheapest-teaser price, which is often a lesser
                    # config than what anyone actually wants and would otherwise get shown as if
                    # it were the real price.
                    chosen = None
                    group_href = raw.get("itemGroupHref")
                    if group_href:
                        resolved = _resolve_group(client, token, group_href, group_cache)
                        if resolved:
                            chosen = resolved["chosen"]

                    if chosen and chosen["price"] is not None:
                        # The chosen variation's own aspect text can name a CPU model the title
                        # didn't (or vice versa) - re-check now that we have it.
                        if _too_old(chosen["cpu"] or ""):
                            continue
                        item_price = chosen["price"]
                        native_currency = chosen["currency"] or price.get("currency", "EUR")
                        shipping = chosen["shipping"] if chosen["shipping"] is not None else _shipping_cost(raw)
                        cpu_hint, ram_hint, storage_hint = chosen["cpu"], chosen["ram"], chosen["storage"]
                    else:
                        item_price = float(price["value"])
                        native_currency = price.get("currency", "EUR")
                        shipping = _shipping_cost(raw)
                        cpu_hint = ram_hint = storage_hint = None

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
                        summary = f"Selected configuration: {picked} (price shown is for this exact configuration). {summary}".strip()
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
                        "kind": "deal",
                    })
    return items
