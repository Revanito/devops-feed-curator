"""Structured vulnerability sources - CISA KEV and NVD - matched against stack.yaml with fixed rules.

No LLM is involved here on purpose: these are the sources that must never be missed, so they keep
working when the classifier models are down or mis-judge an item.

Severity:
- critical (red):  on CISA KEV (actively exploited) and in the stack, or CVSS >= 9.0 in the stack
- high (orange):   CVSS 7.0-8.9 in the stack, or on CISA KEV but not in the stack
Anything else from these sources is ignored.
"""

import fnmatch
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
import yaml

import db
from config import settings

log = logging.getLogger("advisories")

SEVERITY_RANK = {"high": 1, "critical": 2}
OTHER_TAG = "other"

KEV_SOURCE = "CISA KEV"
NVD_SOURCE = "NVD"
_NVD_CURSOR_KEY = "nvd_last_mod_end"


@dataclass
class Product:
    name: str
    cpe: list[str] = field(default_factory=list)
    keywords: list[re.Pattern] = field(default_factory=list)
    min_severity: str = "high"
    # Plain words for the page's filter chips only - see stack.yaml.
    aliases: list[str] = field(default_factory=list)


def load_stack(path: str | None = None) -> list[Product]:
    with open(path or settings.stack_file) as f:
        data = yaml.safe_load(f) or {}
    products = []
    for raw in data.get("products") or []:
        min_severity = str(raw.get("min_severity") or "high").lower()
        if min_severity not in SEVERITY_RANK:
            log.warning("stack.yaml: %s has unknown min_severity %r, using 'high'", raw.get("name"), min_severity)
            min_severity = "high"
        products.append(Product(
            name=str(raw["name"]),
            cpe=[str(c).lower() for c in raw.get("cpe") or []],
            keywords=[re.compile(k, re.IGNORECASE) for k in raw.get("keywords") or []],
            min_severity=min_severity,
            aliases=[str(a).lower() for a in raw.get("aliases") or []],
        ))
    return products


def match_product(products: list[Product], cpes: list[str], text: str) -> Product | None:
    """cpes are "vendor:product" pairs. An exact CPE match wins over a keyword match, so a CVE
    NVD has already analyzed is tagged by what it actually affects, not by words in its text."""
    cpes = [c.lower() for c in cpes]
    for product in products:
        if any(fnmatch.fnmatchcase(c, pattern) for c in cpes for pattern in product.cpe):
            return product
    for product in products:
        if any(k.search(text or "") for k in product.keywords):
            return product
    return None


def severity_for_score(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    return None


def advisory_id(cve_id: str) -> str:
    # One row per CVE, so KEV and NVD reports of the same CVE merge instead of showing twice.
    return hashlib.sha256(f"cve:{cve_id.upper()}".encode()).hexdigest()[:16]


def _nvd_link(cve_id: str) -> str:
    return f"https://nvd.nist.gov/vuln/detail/{cve_id}"


def _first_sentence(text: str, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    if len(sentence) > limit:
        sentence = sentence[:limit - 1].rstrip() + "…"
    return sentence


def _cpe_name(vendor: str, product: str) -> str:
    """KEV uses display names ("Grafana Labs", "HTTP Server"); turn them into CPE-style
    "vendor:product" so stack.yaml's cpe globs can match them too."""
    def norm(s: str) -> str:
        return re.sub(r"\s+", "_", (s or "").strip().lower())
    return f"{norm(vendor)}:{norm(product)}"


# ---------- CISA KEV ----------

def parse_kev(data: dict, products: list[Product], since: datetime) -> list[dict]:
    out = []
    for v in data.get("vulnerabilities") or []:
        cve_id = v.get("cveID") or ""
        if not cve_id:
            continue
        try:
            added = datetime.strptime(v.get("dateAdded", ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if added < since:
            continue
        vendor, prod, name = v.get("vendorProject", ""), v.get("product", ""), v.get("vulnerabilityName", "")
        text = f"{vendor} {prod} {name} {v.get('shortDescription', '')}"
        product = match_product(products, [_cpe_name(vendor, prod)], text)
        summary = f"{v.get('shortDescription', '').strip()} Required action: {v.get('requiredAction', '').strip()}"
        if v.get("dueDate"):
            summary += f" (federal deadline {v['dueDate']})"
        if (v.get("knownRansomwareCampaignUse") or "").lower() == "known":
            summary += " Used in ransomware campaigns."
        out.append({
            "id": advisory_id(cve_id), "cve": cve_id, "source": KEV_SOURCE,
            "title": f"{cve_id}: {name or f'{vendor} {prod}'}",
            "link": _nvd_link(cve_id), "summary": summary[:600],
            "published": added.strftime("%Y-%m-%d"),
            "severity": "critical" if product else "high",
            "cvss": None, "exploited": True,
            "tag": product.name if product else OTHER_TAG,
        })
    return out


def fetch_kev(client: httpx.Client, products: list[Product]) -> list[dict] | None:
    try:
        resp = client.get(settings.kev_url)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.error("CISA KEV fetch failed: %s", exc)
        db.record_source_health(KEV_SOURCE, False, str(exc)[:200])
        return None
    since = datetime.now(timezone.utc) - timedelta(days=settings.news_max_age_days)
    items = parse_kev(data, products, since)
    db.record_source_health(KEV_SOURCE, True)
    log.info("CISA KEV: %d entries added in the last %d days", len(items), settings.news_max_age_days)
    return items


# ---------- NVD ----------

def _nvd_max_score(metrics: dict) -> float | None:
    """Highest base score across CVSS v4.0/v3.1/v3.0, primary (NVD) or secondary (the vendor/CNA).
    Taking the max on purpose: the vendor's score usually lands days before NVD's own analysis,
    and if the two disagree, the worse one is the one to know about."""
    scores = []
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30"):
        for m in metrics.get(key) or []:
            score = (m.get("cvssData") or {}).get("baseScore")
            if isinstance(score, (int, float)):
                scores.append(float(score))
    return max(scores) if scores else None


def _nvd_cpes(cve: dict) -> list[str]:
    out = []
    for config in cve.get("configurations") or []:
        for node in config.get("nodes") or []:
            for match in node.get("cpeMatch") or []:
                # Only what's vulnerable, not the "running on" platform (e.g. an app CVE that lists
                # Windows as the OS it runs on is not a Windows vulnerability).
                if not match.get("vulnerable"):
                    continue
                parts = (match.get("criteria") or "").split(":")
                if len(parts) > 4:
                    out.append(f"{parts[3]}:{parts[4]}")
    return out


def parse_nvd(data: dict, products: list[Product], min_published: datetime) -> list[dict]:
    out = []
    for wrapper in data.get("vulnerabilities") or []:
        cve = wrapper.get("cve") or {}
        cve_id = cve.get("id") or ""
        if not cve_id or (cve.get("vulnStatus") or "").lower() == "rejected":
            continue
        try:
            published = datetime.fromisoformat(cve.get("published", "")).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if published < min_published:
            continue
        description = next((d.get("value", "") for d in cve.get("descriptions") or [] if d.get("lang") == "en"), "")
        product = match_product(products, _nvd_cpes(cve), description)
        if product is None:
            continue
        score = _nvd_max_score(cve.get("metrics") or {})
        severity = severity_for_score(score)
        if severity is None or SEVERITY_RANK[severity] < SEVERITY_RANK[product.min_severity]:
            continue
        out.append({
            "id": advisory_id(cve_id), "cve": cve_id, "source": NVD_SOURCE,
            "title": f"{cve_id}: {_first_sentence(description)}",
            "link": _nvd_link(cve_id), "summary": " ".join(description.split())[:600],
            "published": published.strftime("%Y-%m-%d"),
            "severity": severity, "cvss": score,
            # CISA's own KEV feed is the source of truth for this; NVD also flags it, so use that too.
            "exploited": bool(cve.get("cisaExploitAdd")),
            "tag": product.name,
        })
    return out


def _nvd_get(client: httpx.Client, params: dict) -> dict:
    headers = {"apiKey": settings.nvd_api_key} if settings.nvd_api_key else {}
    last_exc: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(10 * attempt)
        try:
            resp = client.get(settings.nvd_url, params=params, headers=headers, timeout=60)
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                # NVD's rate limit answers 403, and it has frequent short outages - retry those.
                last_exc = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
    raise last_exc or RuntimeError("NVD request failed")


def fetch_nvd(client: httpx.Client, products: list[Product]) -> list[dict] | None:
    """Incremental: asks NVD for every CVE *modified* since the last successful run (with an hour
    of overlap), not just newly published ones - that's how a CVE that was unscored yesterday and
    got its CVSS today is picked up. The cursor only advances after a fully successful run, so a
    failed run is retried from the same point next time instead of leaving a gap."""
    now = datetime.now(timezone.utc)
    last = db.get_meta(_NVD_CURSOR_KEY)
    # NVD's guidance is to poll for updates no more than once every two hours.
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=settings.nvd_poll_minutes):
        log.info("skipping NVD - last successful fetch %s, due every %d minutes", last, settings.nvd_poll_minutes)
        return []
    floor = now - timedelta(days=settings.advisory_max_age_days)
    start = datetime.fromisoformat(last) - timedelta(hours=1) if last else now - timedelta(days=settings.news_max_age_days)
    start = max(start, floor)  # also keeps us under NVD's 120-day maximum range

    params = {
        "lastModStartDate": start.isoformat(timespec="milliseconds"),
        "lastModEndDate": now.isoformat(timespec="milliseconds"),
        "resultsPerPage": 2000,
        "startIndex": 0,
    }
    page_delay = 1 if settings.nvd_api_key else 6  # NVD: 5 requests / 30s without a key
    items: list[dict] = []
    total = 0
    try:
        while True:
            data = _nvd_get(client, params)
            items.extend(parse_nvd(data, products, floor))
            total = int(data.get("totalResults") or 0)
            params["startIndex"] += int(data.get("resultsPerPage") or 0) or 2000
            if params["startIndex"] >= total:
                break
            time.sleep(page_delay)
    except Exception as exc:  # noqa: BLE001 - any failure means "retry this window next run"
        log.error("NVD fetch failed (window from %s): %s", params["lastModStartDate"], exc)
        db.record_source_health(NVD_SOURCE, False, str(exc)[:200])
        return None

    db.set_meta(_NVD_CURSOR_KEY, now.isoformat())
    db.record_source_health(NVD_SOURCE, True)
    log.info("NVD: %d CVEs modified since %s, %d match the stack at High or above",
             total, start.isoformat(), len(items))
    return items


def fetch_all() -> list[dict]:
    try:
        products = load_stack()
    except (OSError, yaml.YAMLError, re.error, KeyError) as exc:
        # A typo in stack.yaml must be loud, not a silent "no vulnerabilities".
        log.error("could not load %s: %s", settings.stack_file, exc)
        db.record_source_health("stack.yaml", False, str(exc)[:200])
        return []
    db.record_source_health("stack.yaml", True)

    items: list[dict] = []
    with httpx.Client(timeout=60, follow_redirects=True,
                      headers={"User-Agent": "devops-feed-curator (security watch)"}) as client:
        items.extend(fetch_kev(client, products) or [])
        items.extend(fetch_nvd(client, products) or [])
    return items
