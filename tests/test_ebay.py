"""Tests for the deterministic eBay filtering rules - the parts that don't touch the network or
the LLM. Titles are modeled on real listings that slipped through before (see commit history)."""
import pytest

import ebay


# --- CPU keep/drop policy ---------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Lenovo ThinkCentre M920q Tiny i7-8700T 16GB 256GB SSD",
    "Dell OptiPlex 3050 Micro i3-7100T",
    "HP EliteDesk 800 G4 Mini i9-9900T",
    "Lenovo M70q Gen 2 i5-11400T",
    "Dell Latitude 7420 i5-1135G7 16GB",
    "Intel NUC 13 Pro i7-1360P",
    "ASUS NUC 14 Core Ultra 5 125H",
    "HP ProDesk 405 G6 Mini Ryzen 5 PRO 4650GE",
    "Lenovo M75q Ryzen 5 4650GE",
])
def test_cpu_kept(title):
    assert ebay._too_old(title) is None


@pytest.mark.parametrize("title", [
    "Lenovo ThinkCentre M920q i5-8500T",                 # 8th-gen i5: no HT
    "Dell OptiPlex 3060 Micro i3-8100T",                 # 8th-gen i3: no HT
    "Dell OptiPlex 7050 Micro i5-7500T",                 # 7th-gen i5: no HT
    "HP EliteDesk 800 G6 Mini i3-10100T",                # 10th+ i3: dropped on policy
    "Intel NUC 11 i3-1115G4",                            # 11th-gen mobile i3
    "Lenovo M93p Tiny i7-4790",                          # pre-7th gen
    "Dell OptiPlex 780 i7-620",                          # 1st-gen, 3-digit model number
    "Mini PC Intel Core i5-7th Gen 8GB",                 # worded generation, 7th-gen i5
    "Mini PC Core Ultra 7 265K",                         # Core Ultra Series 2
    "HP ProDesk 405 G4 Mini Ryzen 5 PRO 2400GE",         # pre-Zen 2 Ryzen PRO
    "Lenovo M715q Ryzen 5 2400GE",                       # pre-Zen 2 Ryzen
])
def test_cpu_dropped(title):
    assert ebay._too_old(title) is not None


def test_9th_gen_i7_kept_with_warning():
    qualifies, reason, warning = ebay._cpu_verdict("Lenovo M720q i7-9700T")
    assert qualifies is True
    assert reason is None
    assert warning == ebay._INTEL_NO_HT_WARNING


def test_no_cpu_stated_passes_through_for_llm():
    assert ebay._cpu_verdict("Lenovo ThinkCentre Tiny Intel Core i7 vPro") == (None, None, None)
    assert ebay._too_old("Lenovo ThinkCentre Tiny Intel Core i7 vPro") is None


def test_cpu_disqualify_reason_rechecks_llm_extracted_cpu():
    # The LLM recognized "M93p" as an i7-4790 box but kept it anyway - this must catch that.
    assert ebay.cpu_disqualify_reason("Intel Core i7-4790 (4 cores / 8 threads)") is not None


@pytest.mark.parametrize("digits, gen", [
    ("620", 1), ("4770", 4), ("8700", 8), ("10700", 10), ("1165", 11), ("13700", 13),
])
def test_intel_generation(digits, gen):
    assert ebay._intel_generation(digits) == gen


# --- HP chassis generation --------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "HP ProDesk 600 G1 Mini",
    "HP 600 G1 DM",                                      # no EliteDesk/ProDesk sub-brand
    "HP EliteDesk 800 G3 Mini i7-7700T",                 # CPU passes, chassis still too old
])
def test_old_hp_chassis_dropped(title):
    assert "pre-G4 HP chassis" in ebay._too_old(title)


def test_g4_hp_chassis_kept():
    assert ebay._too_old("HP ProDesk 400 G4 Mini") is None


# --- Condition ------------------------------------------------------------------------------

@pytest.mark.parametrize("condition, expected", [
    ("For parts or not working", True),
    ("Für Teile oder nicht funktionierend", True),
    ("Used", False),
    ("Gebraucht", False),
    ("", False),
    (None, False),
])
def test_not_working(condition, expected):
    assert ebay._not_working(condition) is expected


@pytest.mark.parametrize("condition, expected", [
    ("Gebraucht", "Used"),
    ("  Generalüberholt ", "Refurbished"),
    ("Für Teile oder nicht funktionierend", "For parts or not working"),
    ("Used", "Used"),                                    # already English: unchanged
    ("Etwas Unbekanntes", "Etwas Unbekanntes"),          # unknown: falls back to original
])
def test_translate_condition(condition, expected):
    assert ebay._translate_condition(condition) == expected


# --- Shipping ---------------------------------------------------------------------------------

def test_no_shipping_available():
    assert ebay._no_shipping_available({}) is True
    assert ebay._no_shipping_available({"shippingOptions": []}) is True
    # CALCULATED (checkout-time) shipping is common and doesn't mean it won't ship.
    assert ebay._no_shipping_available({"shippingOptions": [{"shippingCostType": "CALCULATED"}]}) is False


def test_shipping_cost():
    raw = {"shippingOptions": [
        {"shippingCostType": "CALCULATED"},
        {"shippingCostType": "FIXED", "shippingCost": {"value": "4.99"}},
    ]}
    assert ebay._shipping_cost(raw) == 4.99
    assert ebay._shipping_cost({"shippingOptions": [{"shippingCostType": "CALCULATED"}]}) is None


def test_to_eur(monkeypatch):
    monkeypatch.setattr(ebay, "_live_gbp_rate", None)
    monkeypatch.setattr(ebay.settings, "gbp_to_eur_rate", 1.2)
    assert ebay._to_eur(100, "GBP") == pytest.approx(120)
    assert ebay._to_eur(100, "EUR") == 100


def _fake_fx_response(payload):
    request = ebay.httpx.Request("GET", ebay._FX_URL)
    return ebay.httpx.Response(200, json=payload, request=request)


@pytest.mark.parametrize("payload, expected", [
    ({"base": "GBP", "rates": {"EUR": 1.15}}, 1.15),     # live rate used
    ({"base": "GBP", "rates": {"EUR": 42}}, 1.2),        # implausible: fallback
    ({"error": "nope"}, 1.2),                            # malformed: fallback
])
def test_refresh_gbp_rate(monkeypatch, payload, expected):
    monkeypatch.setattr(ebay, "_live_gbp_rate", None)
    monkeypatch.setattr(ebay.settings, "gbp_to_eur_rate", 1.2)
    monkeypatch.setattr(ebay.httpx, "get", lambda *a, **kw: _fake_fx_response(payload))
    ebay.refresh_gbp_rate()
    assert ebay._gbp_to_eur_rate() == pytest.approx(expected)


def test_refresh_gbp_rate_network_error_falls_back(monkeypatch):
    def boom(*a, **kw):
        raise ebay.httpx.ConnectError("offline")
    monkeypatch.setattr(ebay, "_live_gbp_rate", None)
    monkeypatch.setattr(ebay.settings, "gbp_to_eur_rate", 1.2)
    monkeypatch.setattr(ebay.httpx, "get", boom)
    ebay.refresh_gbp_rate()
    assert ebay._gbp_to_eur_rate() == pytest.approx(1.2)


@pytest.mark.parametrize("source, marketplace", [
    ("eBay (DE)", "EBAY_DE"), ("eBay (GB)", "EBAY_GB"), ("", "EBAY_FR"),
])
def test_marketplace_from_source(source, marketplace):
    assert ebay._marketplace_from_source(source) == marketplace


# --- RAM ------------------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Kingston 8GB DDR3 1600MHz",
    "Samsung 4GB DDR 3 SODIMM",
    "2x2GB DDR2 800",
])
def test_old_ram_dropped(title):
    assert ebay.ram_disqualify_reason(title) is not None


@pytest.mark.parametrize("title", [
    "Crucial 16GB DDR4 3200 SODIMM",
    "Kingston Fury 2x16GB DDR5 5600",
    "32GB Arbeitsspeicher SODIMM",                       # no generation stated: left to the LLM
])
def test_ram_kept(title):
    assert ebay.ram_disqualify_reason(title) is None


@pytest.mark.parametrize("title, expected", [
    ("Kingston Fury 2x16GB DDR4", ("32GB", "2x16GB")),
    ("Crucial 2 x 8 GB DDR4 SODIMM", ("16GB", "2x8GB")),
    ("Corsair 32GB (2x16GB) DDR5", ("32GB", "2x16GB")),    # kit wins over a bare total
    ("Samsung 16GB DDR4 3200", ("16GB", "")),
    ("DDR4 SODIMM", ("", "")),
])
def test_parse_ram_kit(title, expected):
    assert ebay._parse_ram_kit(title) == expected


# --- Multi-variation listings -----------------------------------------------------------------

def _variation(price, cpu=None, ram=None, storage=None):
    aspects = []
    if cpu:
        aspects.append({"name": "Processor", "value": cpu})
    if ram:
        aspects.append({"name": "RAM Size", "value": ram})
    if storage:
        aspects.append({"name": "SSD Capacity", "value": storage})
    return ebay._variation_specs({
        "localizedAspects": aspects,
        "price": {"value": str(price), "currency": "EUR"} if price is not None else {},
        "shippingOptions": [],
    })


@pytest.mark.parametrize("value, expected", [
    ("1TB", ("1TB", 1024)), ("512 Go", ("512 Go", 512)), ("32", ("32GB", 32)), ("", (None, None)),
])
def test_parse_size_gb(value, expected):
    assert ebay._parse_size_gb(value) == expected


def test_variation_specs_combined_french_aspect():
    specs = ebay._variation_specs({
        "localizedAspects": [{"name": "Configuration", "value": "RAM : 32 Go, Disque dur : 256 Go"}],
        "price": {"value": "299.00", "currency": "EUR"},
    })
    assert specs["ram_gb"] == 32
    assert specs["storage_gb"] == 256
    assert specs["cpu"] is None


def test_variations_picks_best_cpu_then_32gb():
    chosen = ebay._pick_best_variation([
        _variation(300, cpu="Intel Core i5-8500T", ram="32GB"),
        _variation(350, cpu="Intel Core i7-8700T", ram="16GB"),
        _variation(400, cpu="Intel Core i7-8700T", ram="32GB"),
    ])
    assert chosen["price"] == 400


def test_variations_shared_i7_title_but_only_i5_options_rejected():
    # The real failure: listing titled "i7", but the only selectable CPU was an i5-7th-gen.
    assert ebay._pick_best_variation([
        _variation(250, cpu="Intel Core i5-7500T", ram="16GB"),
        _variation(280, cpu="Intel Core i5-7500T", ram="32GB"),
    ]) is None


def test_variations_without_cpu_pick_32gb_then_smallest_storage():
    chosen = ebay._pick_best_variation([
        _variation(300, ram="16GB", storage="256GB"),
        _variation(380, ram="32GB", storage="512GB"),
        _variation(360, ram="32GB", storage="256GB"),
    ])
    assert chosen["price"] == 360


def test_variations_fall_back_to_most_ram_when_no_32gb():
    chosen = ebay._pick_best_variation([
        _variation(200, ram="8GB"),
        _variation(240, ram="16GB"),
    ])
    assert chosen["ram_gb"] == 16


def test_variations_carry_9th_gen_warning():
    chosen = ebay._pick_best_variation([_variation(320, cpu="Intel Core i7-9700T", ram="32GB")])
    assert chosen["cpu_warning"] == ebay._INTEL_NO_HT_WARNING


def test_variations_without_prices_ignored():
    assert ebay._pick_best_variation([_variation(None, cpu="Intel Core i7-8700T")]) is None
