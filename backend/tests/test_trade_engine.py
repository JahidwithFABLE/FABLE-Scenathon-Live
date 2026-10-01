import math
import pytest

from trade_engine import (
    EXCLUDED_PRODUCTS,
    TradeAdjustmentBlocked,
    build_injection_payload,
    calculate_trade_adjustment,
    canonical_product,
    normalize_submission,
)


def row(country, pathway, product, year, imp, exp):
    return {
        "Country": country,
        "CountryID": country,
        "Pathway": pathway,
        "Product": product,
        "Year": year,
        "Import_quantity": imp,
        "Export_quantity": exp,
    }


def test_submission_minimal_contract_and_hash_stable():
    payload = {
        "username": "demo",
        "pathway": "CurrentTrends",
        "country_id": "ARG",
        "rows": [
            {
                "Location": "Argentina",
                "Product": "Barley",
                "Year": 2020,
                "Import_quantity": 10,
                "Export_quantity": 12,
            },
            {
                "Location": "Argentina",
                "Product": "Barley",
                "Year": 2025,
                "Import_quantity": 11,
                "Export_quantity": 13,
            },
        ],
    }
    a = normalize_submission(payload)
    b = normalize_submission(payload)
    assert a["row_count"] == 2
    assert a["country_id"] == "ARG"
    assert a["pathway"] == "CurrentTrends"
    assert a["report_hash"] == b["report_hash"]


def test_case_normalization_merges_barley_variants():
    rows = [
        row("SWE", "CurrentTrends", "Barley", 2020, 0, 100),
        row("ARG", "CurrentTrends", "barley", 2020, 100, 100),
        row("SWE", "CurrentTrends", "Barley", 2030, 0, 150),
        row("ARG", "CurrentTrends", "barley", 2030, 100, 150),
    ]
    result = calculate_trade_adjustment(rows)
    products = {r["Product"] for r in result["adjusted_rows"]}
    assert products == {"barley"}


def test_historical_rows_unchanged_and_future_adjusted():
    # 2020 alpha = (200 - 100) / 100 = 1.0.
    # In 2030: imports 100, exports 300 => allowed export limit = 200,
    # so factor = 200 / 300.
    rows = [
        row("A", "CurrentTrends", "maize", 2020, 60, 120),
        row("B", "CurrentTrends", "maize", 2020, 40, 80),
        row("A", "CurrentTrends", "maize", 2030, 60, 180),
        row("B", "CurrentTrends", "maize", 2030, 40, 120),
    ]
    result = calculate_trade_adjustment(rows)
    adjusted = result["adjusted_rows"]
    hist = [r for r in adjusted if r["Year"] == 2020]
    fut = [r for r in adjusted if r["Year"] == 2030]
    assert all(r["ExportsAdj"] == r["ExportOriginal"] for r in hist)
    assert all(math.isclose(r["AdjFactorExport"], 2/3) for r in fut)
    assert math.isclose(sum(r["ExportsAdj"] for r in fut), 200.0)


def test_excluded_product_untouched():
    assert "honey" in EXCLUDED_PRODUCTS
    rows = [
        row("A", "CurrentTrends", "honey", 2020, 100, 200),
        row("A", "CurrentTrends", "honey", 2030, 100, 500),
    ]
    result = calculate_trade_adjustment(rows)
    fut = next(r for r in result["adjusted_rows"] if r["Year"] == 2030)
    assert fut["ExportsAdj"] == 500
    assert fut["AdjFactorExport"] == 1


def test_zero_zero_true_no_trade_is_safe():
    rows = [
        row("A", "CurrentTrends", "test_zero", 2020, 0, 0),
        row("A", "CurrentTrends", "test_zero", 2030, 0, 0),
    ]
    result = calculate_trade_adjustment(rows)
    assert result["blockers"] == []
    qa = next(r for r in result["global_qa"] if r["Year"] == 2030)
    assert qa["Status"] == "NO_TRADE"
    assert qa["AdjFactorExport"] == 1


def test_zero_zero_baseline_with_future_trade_blocks():
    rows = [
        row("A", "CurrentTrends", "new_product", 2020, 0, 0),
        row("A", "CurrentTrends", "new_product", 2030, 10, 0),
    ]
    with pytest.raises(TradeAdjustmentBlocked) as exc:
        calculate_trade_adjustment(rows)
    reasons = {b["Reason"] for b in exc.value.blockers}
    assert "UNDEFINED_2020_ALPHA_WITH_FUTURE_TRADE" in reasons


def test_alpha_minus_one_zero_export_bug_is_safe():
    # 2020 alpha = -1. In 2030, threshold = min(.9, 0) * imports = 0.
    # exports=0 >= threshold=0, so factor must be 1 (no division by zero).
    rows = [
        row("A", "CurrentTrends", "lentil", 2020, 100, 0),
        row("A", "CurrentTrends", "lentil", 2030, 50, 0),
    ]
    result = calculate_trade_adjustment(rows)
    fut = next(r for r in result["adjusted_rows"] if r["Year"] == 2030)
    assert fut["AdjFactorExport"] == 1
    assert fut["ExportsAdj"] == 0


def test_injection_payload_returns_all_available_pathways():
    rows = []
    for pathway in ("CurrentTrends", "GlobalSustainability", "NationalCommitments"):
        rows.extend(
            [
                row("ARG", pathway, "maize", 2020, 100, 100),
                row("ARG", pathway, "maize", 2030, 100, 100),
            ]
        )
    result = calculate_trade_adjustment(rows)
    payload = build_injection_payload(result["adjusted_rows"], country_id="ARG")
    assert payload["row_count"] == 6
    assert {x["Pathway"] for x in payload["rows"]} == {
        "CurrentTrends",
        "GlobalSustainability",
        "NationalCommitments",
    }
