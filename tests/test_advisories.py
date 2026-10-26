"""Tests for the security watch: stack matching, KEV/NVD parsing, severity rules, merging, the LLM
severity parsing and the page layout. No network calls."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import advisories
import classifier
import db
from config import settings

APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")
STACK = advisories.load_stack(os.path.join(APP_DIR, "stack.yaml"))
NOW = datetime.now(timezone.utc)
LONG_AGO = NOW - timedelta(days=365)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    db.init_db()
    return tmp_path


# ---------- matching ----------

@pytest.mark.parametrize("text,expected", [
    ("In PHP versions 8.1.* before 8.1.29, 8.2.* before 8.2.20, when using Apache and PHP-CGI on "
     "Windows, the CGI module may misinterpret characters", "PHP"),
    ("The XYZ plugin for WordPress is vulnerable to PHP Object Injection in all versions up to 1.2", None),
    ("An attacker can make the Node.js HTTP/2 server completely unavailable", "Node.js"),
    ("express is a minimal web framework for Node.js. Versions prior to 4.19 are vulnerable", None),
    ("xyz is a Node.js library for parsing YAML", None),
    ("Windows Kerberos Elevation of Privilege Vulnerability", "Windows Server"),
    ("Zoom Workplace App for Windows may allow privilege escalation", None),
    ("SQL Server Native Client OLE DB Provider Remote Code Execution Vulnerability", "SQL Server"),
    ("Git is a revision control system. Prior to version 2.45.1, recursive clones", "Git"),
    ("Vulnerability in the Oracle Java SE, Oracle GraalVM for JDK product of Oracle Java SE", "Java"),
    ("In the Linux kernel, the following vulnerability has been resolved: net: fix", "Linux kernel"),
    ("A signal handler race condition was found in OpenSSH server (sshd)", "OpenSSH"),
    ("mod_rewrite in Apache HTTP Server 2.4.59 and earlier allows", "Apache HTTP Server"),
    ("Apache Struts file upload logic is flawed", None),
    ("An issue in GitLab CE/EE allows", None),
    ("An authorized user may crash the mongod process", "MongoDB"),
    ("Docker Desktop for Windows allows a local user to escalate to SYSTEM", "Docker"),
    ("runc before 1.3.3 allows a container breakout through a /proc mount race", "Docker"),
    ("containerd is an open-source container runtime. A bug in the CRI plugin", "Docker"),
    ("Moby is an open-source project created by Docker. dockerd may ignore", "Docker"),
    ("BuildKit is a toolkit for converting source code to build artifacts", "Docker"),
])
def test_keyword_matching(text, expected):
    product = advisories.match_product(STACK, [], text)
    assert (product.name if product else None) == expected


def test_cpe_match_wins_over_keywords():
    # Mentions Windows in the text, but NVD says it's a Git for Windows vulnerability.
    product = advisories.match_product(STACK, ["git_for_windows_project:git_for_windows"],
                                       "Windows credential leak")
    assert product.name == "Git"


def test_cpe_glob():
    assert advisories.match_product(STACK, ["microsoft:windows_server_2022"], "").name == "Windows Server"


@pytest.mark.parametrize("score,expected", [
    (None, None), (6.9, None), (7.0, "high"), (8.9, "high"), (9.0, "critical"), (10.0, "critical"),
])
def test_severity_for_score(score, expected):
    assert advisories.severity_for_score(score) == expected


# ---------- KEV ----------

def _kev(cve, vendor, product, added, name="Some Vulnerability"):
    return {"cveID": cve, "vendorProject": vendor, "product": product, "vulnerabilityName": name,
            "dateAdded": added, "shortDescription": "Bad things.", "requiredAction": "Patch.",
            "dueDate": "2026-11-15", "knownRansomwareCampaignUse": "Known"}


def test_parse_kev():
    today = NOW.strftime("%Y-%m-%d")
    data = {"vulnerabilities": [
        _kev("CVE-2026-0001", "PHP", "PHP", today, "PHP-CGI OS Command Injection"),
        _kev("CVE-2026-0002", "Fortinet", "FortiOS", today),
        _kev("CVE-2020-0003", "PHP", "PHP", "2020-01-01"),  # too old
        _kev("CVE-2026-0004", "Microsoft", "Windows", today, "Microsoft Windows SMB Vulnerability"),
    ]}
    items = {i["cve"]: i for i in advisories.parse_kev(data, STACK, NOW - timedelta(days=7))}
    assert set(items) == {"CVE-2026-0001", "CVE-2026-0002", "CVE-2026-0004"}
    assert items["CVE-2026-0001"]["severity"] == "critical" and items["CVE-2026-0001"]["tag"] == "PHP"
    assert items["CVE-2026-0004"]["tag"] == "Windows Server"
    # Exploited, but not in the stack: orange, not red.
    assert items["CVE-2026-0002"]["severity"] == "high" and items["CVE-2026-0002"]["tag"] == "other"
    assert items["CVE-2026-0001"]["exploited"] is True
    assert "ransomware" in items["CVE-2026-0001"]["summary"]


# ---------- NVD ----------

def _nvd(cve, description, score=None, cpes=(), status="Analyzed", published=None, kev=False, metric="cvssMetricV31"):
    record = {
        "id": cve, "vulnStatus": status,
        "published": (published or NOW).strftime("%Y-%m-%dT%H:%M:%S.000"),
        "descriptions": [{"lang": "en", "value": description}],
        "metrics": {},
        "configurations": [{"nodes": [{"cpeMatch": [
            {"vulnerable": vulnerable, "criteria": f"cpe:2.3:a:{cpe}:*:*:*:*:*:*:*:*"}
            for cpe, vulnerable in cpes
        ]}]}] if cpes else [],
    }
    if score is not None:
        record["metrics"][metric] = [{"type": "Secondary", "cvssData": {"baseScore": score}}]
    if kev:
        record["cisaExploitAdd"] = "2026-10-20"
    return {"cve": record}


def test_parse_nvd_rules():
    data = {"vulnerabilities": [
        _nvd("CVE-1", "In PHP versions 8.3.* before 8.3.9, something", 9.8),         # critical
        _nvd("CVE-2", "Grafana is an open-source platform. XSS in panels", 7.5),     # high
        _nvd("CVE-3", "Grafana is an open-source platform. Minor issue", 5.3),       # medium: skip
        _nvd("CVE-4", "Grafana is an open-source platform. Unscored"),               # unscored: skip
        _nvd("CVE-5", "Some random router firmware RCE", 10.0),                      # not in stack
        _nvd("CVE-6", "In the Linux kernel, the following vulnerability", 7.8),      # kernel High: skip
        _nvd("CVE-7", "In the Linux kernel, the following vulnerability", 9.1),      # kernel Critical
        _nvd("CVE-8", "In PHP versions 8.3.*, rejected", 9.8, status="Rejected"),
        _nvd("CVE-9", "In PHP versions 8.1.*, old", 9.8, published=LONG_AGO),       # too old
        _nvd("CVE-10", "Unrelated app", 8.1, cpes=[("mongodb:mongodb", True)]),      # by CPE
        _nvd("CVE-11", "Some app running on Windows", 9.8,
             cpes=[("acme:app", True), ("microsoft:windows_server_2022", False)]),    # platform only
        _nvd("CVE-12", "Windows SMB Remote Code Execution Vulnerability", 9.0, metric="cvssMetricV40"),
        _nvd("CVE-13", "Grafana is an open-source platform.", 7.1, kev=True),
    ]}
    items = {i["cve"]: i for i in advisories.parse_nvd(data, STACK, NOW - timedelta(days=30))}
    assert set(items) == {"CVE-1", "CVE-2", "CVE-7", "CVE-10", "CVE-12", "CVE-13"}
    assert items["CVE-1"]["severity"] == "critical" and items["CVE-1"]["cvss"] == 9.8
    assert items["CVE-2"]["severity"] == "high"
    assert items["CVE-10"]["tag"] == "MongoDB"
    assert items["CVE-12"]["tag"] == "Windows Server"
    assert items["CVE-13"]["exploited"] is True


def test_nvd_takes_worst_score():
    metrics = {"cvssMetricV31": [{"cvssData": {"baseScore": 7.5}}, {"cvssData": {"baseScore": 9.1}}],
               "cvssMetricV2": [{"cvssData": {"baseScore": 10.0}}]}  # v2 ignored
    assert advisories._nvd_max_score(metrics) == 9.1


# ---------- merging ----------

def _adv(cve, severity, source="NVD", tag="PHP", exploited=False, cvss=7.5, title=None):
    return {"id": advisories.advisory_id(cve), "cve": cve, "source": source, "title": title or cve,
            "link": "https://nvd.nist.gov/vuln/detail/" + cve, "summary": "s", "published": NOW.isoformat(),
            "severity": severity, "cvss": cvss, "exploited": exploited, "tag": tag}


def _row(cve):
    with db.get_conn() as conn:
        return conn.execute("SELECT * FROM items WHERE id = ?", (advisories.advisory_id(cve),)).fetchone()


def test_upsert_escalates_and_merges(temp_db):
    assert db.upsert_advisories([_adv("CVE-A", "high")]) == (1, 0)
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET seen_at = '2000-01-01T00:00:00+00:00'")
    # Same CVE, same severity: no change, seen_at untouched.
    assert db.upsert_advisories([_adv("CVE-A", "high")]) == (0, 0)
    assert _row("CVE-A")["seen_at"].startswith("2000")
    # Added to KEV: becomes critical, KEV text wins, back to the top (seen_at reset).
    kev = _adv("CVE-A", "critical", source="CISA KEV", exploited=True, cvss=None, title="KEV title")
    assert db.upsert_advisories([kev]) == (0, 1)
    row = _row("CVE-A")
    assert row["severity"] == "critical" and row["exploited"] == 1 and row["critical"] == 1
    assert row["title"] == "KEV title" and row["cvss"] == 7.5
    assert not row["seen_at"].startswith("2000")
    # A later NVD update never downgrades or overwrites the KEV text.
    db.upsert_advisories([_adv("CVE-A", "high", title="NVD title")])
    row = _row("CVE-A")
    assert row["severity"] == "critical" and row["title"] == "KEV title"


def test_upsert_kev_other_becomes_critical_when_nvd_finds_product(temp_db):
    db.upsert_advisories([_adv("CVE-B", "high", source="CISA KEV", tag="other", exploited=True, cvss=None)])
    assert _row("CVE-B")["severity"] == "high"
    db.upsert_advisories([_adv("CVE-B", "high", tag="Grafana", cvss=7.2)])
    row = _row("CVE-B")
    assert row["severity"] == "critical" and row["tag"] == "Grafana"


def test_security_items_query(temp_db):
    db.upsert_advisories([_adv("CVE-C", "critical"), _adv("CVE-D", "high")])
    db.insert_new_items([{"id": "n1", "source": "x", "title": "t", "link": "l"}])
    db.apply_classifications({"n1": {"keep": False, "tag": "other", "critical": False, "severity": ""}})
    got = {r["title"] for r in db.get_security_items(days=7)}
    assert got == {"CVE-C", "CVE-D"}


def test_source_health(temp_db):
    db.record_source_health("NVD", True)
    db.record_source_health("NVD", False, "HTTP 503")
    health = db.get_source_health()["NVD"]
    assert health["ok"] is False and health["error"] == "HTTP 503" and health["last_ok_at"]


# ---------- LLM severity parsing ----------

def _fake_llm(monkeypatch, reply: str):
    def handler(request):
        return httpx.Response(200, json={"aiRecord": {"aiRecordDetail": {"resultObject": [reply]}}})
    real = httpx.AsyncClient
    monkeypatch.setattr(classifier.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_news_severity_parsing(monkeypatch):
    _fake_llm(monkeypatch, '[{"i":1,"severity":"critical","tag":"PHP"},{"i":2,"severity":"high","tag":"Git"},'
                           '{"i":3,"severity":"none","tag":"other"},{"i":4,"severity":"bogus","tag":"x"}]')
    batch = [{"id": f"id{n}", "source": "s", "title": "t", "summary": ""} for n in range(1, 5)]
    r = asyncio.run(classifier.classify_batch(batch, "model"))
    assert r["id1"]["keep"] and r["id1"]["critical"] and r["id1"]["severity"] == "critical"
    assert r["id2"]["keep"] and not r["id2"]["critical"] and r["id2"]["severity"] == "high"
    assert not r["id3"]["keep"] and r["id3"]["severity"] == ""
    assert not r["id4"]["keep"]


def test_deals_keep_still_works(monkeypatch):
    # The deals prompts answer with "keep" and no severity - must be unaffected.
    _fake_llm(monkeypatch, '[{"i":1,"keep":true,"tag":"mini-pc","cpu":"i7-8700T"}]')
    batch = [{"id": "d1", "title": "t", "summary": "", "price": 100, "shipping": 0, "currency": "EUR"}]
    r = asyncio.run(classifier._classify("p", batch, classifier._build_deals_user_prompt, "m"))
    assert r["d1"]["keep"] and r["d1"]["severity"] == ""


def test_news_prompt_lists_stack(monkeypatch):
    monkeypatch.chdir(APP_DIR)
    prompt = classifier._news_system_prompt()
    assert "{{STACK}}" not in prompt
    assert "- PHP" in prompt and "- Windows Server" in prompt


# ---------- page ----------

def test_page_red_above_orange(temp_db, monkeypatch):
    monkeypatch.chdir(APP_DIR)
    import main
    from fastapi.testclient import TestClient

    db.upsert_advisories([
        _adv("CVE-2026-1111", "high", tag="Grafana", title="orange one"),
        _adv("CVE-2026-2222", "critical", tag="PHP", title="red one", cvss=9.8),
    ])
    db.record_source_health("NVD", False, "HTTP 503")
    html = TestClient(main.app).get("/").text
    assert html.index("red one") < html.index("orange one")
    assert html.index("critical-section") < html.index("high-section")
    assert "CVSS 9.8" in html
    assert "source failing" in html and "NVD" in html
    # The filter bar and the product list its script reads.
    assert 'id="filter-search"' in html
    assert '"name": "Docker"' in html and '"node2shell"' in html


def test_docker_cpe():
    assert advisories.match_product(STACK, ["docker:desktop"], "").name == "Docker"
    assert advisories.match_product(STACK, ["linuxfoundation:runc"], "").name == "Docker"


def test_every_product_loads_aliases():
    names = {p.name: p.aliases for p in STACK}
    assert names["PHP"] == ["php"]
    assert "apache" not in names["Apache HTTP Server"]  # would pull Struts/Tomcat under that chip


def test_nvd_respects_two_hour_interval(temp_db):
    db.set_meta("nvd_last_mod_end", (NOW - timedelta(minutes=30)).isoformat())

    def boom(*a, **k):
        raise AssertionError("NVD should not be called")
    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert advisories.fetch_nvd(client, STACK) == []


def test_nvd_cursor_only_advances_on_success(temp_db, monkeypatch):
    monkeypatch.setattr(advisories.time, "sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert advisories.fetch_nvd(client, STACK) is None
    assert db.get_meta("nvd_last_mod_end") is None
    assert db.get_source_health()["NVD"]["ok"] is False

    page = {"totalResults": 1, "resultsPerPage": 1,
            "vulnerabilities": [_nvd("CVE-2026-5", "In PHP versions 8.4.* before 8.4.2, RCE", 9.8)]}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=page)))
    items = advisories.fetch_nvd(client, STACK)
    assert [i["cve"] for i in items] == ["CVE-2026-5"]
    assert db.get_meta("nvd_last_mod_end") is not None


# ---------- dismissing ----------

def _client(monkeypatch, token="s3cret"):
    monkeypatch.chdir(APP_DIR)
    monkeypatch.setattr(settings, "admin_token", token)
    import main
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_dismiss_requires_login(temp_db, monkeypatch):
    client = _client(monkeypatch)
    db.upsert_advisories([_adv("CVE-2026-3333", "critical", title="keep me")])
    item_id = advisories.advisory_id("CVE-2026-3333")
    r = client.post(f"/dismiss/{item_id}", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 403
    assert "keep me" in client.get("/").text
    assert "Not relevant - hide" not in client.get("/").text


def test_dismiss_disabled_without_token(temp_db, monkeypatch):
    client = _client(monkeypatch, token="")
    db.upsert_advisories([_adv("CVE-2026-3334", "critical")])
    r = client.post(f"/dismiss/{advisories.advisory_id('CVE-2026-3334')}",
                    headers={"X-Requested-With": "fetch", "X-Admin-Token": ""})
    assert r.status_code == 403
    assert client.post("/login", content="token=", headers={"Content-Type": "application/x-www-form-urlencoded"},
                       follow_redirects=False).headers["location"] == "/login?error=1"


def test_login_dismiss_restore(temp_db, monkeypatch):
    client = _client(monkeypatch)
    db.upsert_advisories([_adv("CVE-2026-4444", "high", tag="Grafana", title="not my grafana")])
    item_id = advisories.advisory_id("CVE-2026-4444")

    bad = client.post("/login", content="token=nope", follow_redirects=False,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert bad.headers["location"] == "/login?error=1"
    ok = client.post("/login", content="token=s3cret", follow_redirects=False,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert ok.headers["location"] == "/"
    assert "s3cret" not in ok.headers["set-cookie"]  # cookie holds a hash, not the token

    page = client.get("/").text
    assert "not my grafana" in page and "Not relevant - hide" in page

    assert client.post(f"/dismiss/{item_id}", headers={"X-Requested-With": "fetch"}).json() == {"ok": True}
    assert "not my grafana" not in client.get("/").text
    hidden = client.get("/?view=hidden").text
    assert "not my grafana" in hidden and "Restore" in hidden

    client.post(f"/restore/{item_id}", headers={"X-Requested-With": "fetch"})
    assert "not my grafana" in client.get("/").text


def test_hidden_view_needs_login(temp_db, monkeypatch):
    client = _client(monkeypatch)
    db.upsert_advisories([_adv("CVE-2026-5555", "high", title="secretly hidden")])
    db.set_dismissed(advisories.advisory_id("CVE-2026-5555"), True)
    assert "secretly hidden" not in client.get("/?view=hidden").text


def test_dismissed_stays_hidden_unless_escalated(temp_db):
    db.upsert_advisories([_adv("CVE-2026-6666", "high")])
    db.set_dismissed(advisories.advisory_id("CVE-2026-6666"), True)
    # Re-reported with nothing new: stays hidden.
    db.upsert_advisories([_adv("CVE-2026-6666", "high")])
    assert _row("CVE-2026-6666")["dismissed"] == 1
    # Now actively exploited: comes back.
    db.upsert_advisories([_adv("CVE-2026-6666", "critical", source="CISA KEV", exploited=True)])
    assert _row("CVE-2026-6666")["dismissed"] == 0


def test_dismiss_news_item(temp_db):
    db.insert_new_items([{"id": "news1", "source": "x", "title": "Exchange zero-day", "link": "l"}])
    db.apply_classifications({"news1": {"keep": True, "tag": "other", "critical": True, "severity": "critical"}})
    assert db.set_dismissed("news1", True)
    assert db.get_security_items(days=7) == []
    assert not db.set_dismissed("does-not-exist", True)
