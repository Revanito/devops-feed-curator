import base64
import hashlib
import logging
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


def _group_configs(client: httpx.Client, token: str, group_href: str, cache: dict) -> str:
    """Multi-variation ('choose your configuration') listings show one price/title in search
    results - the cheapest variation - and hide the actual RAM/storage/CPU options behind a
    dropdown the search API never returns. Fetching the item group once per unique listing gets
    every variation's own price and aspect values, so the classifier can report real ranges
    ("16GB/32GB") instead of guessing from a title that says nothing about it."""
    if group_href in cache:
        return cache[group_href]

    text = ""
    try:
        resp = client.get(group_href, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            variations = resp.json().get("items", [])
            prices = sorted({
                float(v["price"]["value"]) for v in variations
                if v.get("price", {}).get("value")
            })
            currency = next(
                (v["price"]["currency"] for v in variations if v.get("price", {}).get("currency")), "EUR",
            )
            aspect_values = []
            for v in variations:
                for aspect in v.get("localizedAspects", []):
                    value = aspect.get("value", "")
                    if value and value not in aspect_values:
                        aspect_values.append(value)

            parts = []
            if aspect_values:
                parts.append("Configurations: " + " | ".join(aspect_values))
            elif len(variations) > 1:
                parts.append(f"{len(variations)} configurations available")
            if len(prices) > 1:
                parts.append(f"price range {prices[0]:.0f}-{prices[-1]:.0f} {currency}")
            text = "; ".join(parts)
        else:
            log.warning("eBay item group fetch failed (%s): %s", resp.status_code, resp.text[:300])
    except httpx.HTTPError as exc:
        log.warning("eBay item group fetch errored: %s", exc)

    cache[group_href] = text
    return text


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
            "filter": f"price:[..{settings.deal_max_price_eur}],priceCurrency:{currency},buyingOptions:{{FIXED_PRICE}}",
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
    group_cache: dict[str, str] = {}
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
                    if not link or not price.get("value"):
                        continue

                    native_currency = price.get("currency", "EUR")
                    item_price = float(price["value"])
                    shipping = _shipping_cost(raw)

                    price_eur = _to_eur(item_price, native_currency)
                    shipping_eur = _to_eur(shipping, native_currency) if shipping is not None else None
                    total_eur = price_eur + (shipping_eur or 0.0)
                    # The search API's own price filter only looks at the item price, not
                    # shipping, and can't filter across currencies - re-check the true EUR-
                    # equivalent total here now that shipping and FX are both known, so a listing
                    # that's only cheap-looking in isolation doesn't sneak in as a "deal".
                    if total_eur > settings.deal_max_price_eur:
                        continue

                    summary = condition
                    group_href = raw.get("itemGroupHref")
                    if group_href:
                        configs = _group_configs(client, token, group_href, group_cache)
                        if configs:
                            summary = f"{condition}. {configs}" if condition else configs
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
                        "title": raw.get("title", "(no title)").strip(),
                        "link": link,
                        "summary": summary,
                        "published": raw.get("itemCreationDate", ""),
                        "price": price_eur,
                        "shipping": shipping_eur,
                        "currency": "EUR",
                        "kind": "deal",
                    })
    return items
