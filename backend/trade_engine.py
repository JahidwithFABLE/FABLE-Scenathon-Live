"""
Production core for the FABLE Scenathon trade-adjustment backend.

This module is intentionally workbook-agnostic:
- Excel/Office.js reads SCENATHON_report and sends JSON.
- This module validates submissions and performs the global trade adjustment.
- Excel/Office.js later injects returned adjusted trade into FullDataTradeAdj.

Methodology:
- Product matching key: strip().casefold()
- No semantic singular/plural mapping
- 2020 global imbalance (alpha) by pathway x product
- Clara's four export-adjustment branches
- Imports unchanged
- Historical years (2000-2020) unchanged
- Excluded products unchanged
- 2020 0/0 + no future trade => safe NO_TRADE, factor 1
- Undefined / unsafe cases with future trade => blockers
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from typing import Any, Iterable, Mapping, Sequence

ENGINE_VERSION = "1.0.0"

HISTORICAL_YEARS = frozenset({2000, 2005, 2010, 2015, 2020})

# Exact list from Clara's R source, canonicalized for production matching.
_EXCLUDED_PRODUCTS_RAW = (
    "cattle        ",
    "chickens      ",
    "pigs          ",
    "sheep_goats         ",
    "chips_and__particles",
    "mech_pulp",
    "fiber_hard_other",
    "fiber_soft_other",
    "honey",
    "meat_other",
    "cottcake",
    "groundnutcake",
    "other_olscake",
    "palmkernelcake",
    "rapecake",
    "sunflcake",
    "citrus_other",
    "cereal_other",
    "cattle",
    "pigs",
    "chickens",
    "sheep_goats",
    "oilpalmfruit",
    "clove",
    "sugarbeet",
    "sugarcane",
    "cottoil",
    "sisal",
    "other_oil",
    "abaca",
)
EXCLUDED_PRODUCTS = frozenset(x.strip().casefold() for x in _EXCLUDED_PRODUCTS_RAW)

# Pathway names are configured by the Streamlit iteration.
# The trade engine treats pathway as a grouping label and does
# not enforce a fixed global list.


class TradeEngineError(ValueError):
    """Base exception for invalid backend input."""


class SubmissionValidationError(TradeEngineError):
    """Raised when an add-in submission payload is invalid."""


class IterationValidationError(TradeEngineError):
    """Raised when the combined iteration rows are invalid."""


class TradeAdjustmentBlocked(TradeEngineError):
    """Raised when the iteration contains unsafe/undefined adjustment cases."""

    def __init__(self, blockers: Sequence[Mapping[str, Any]]):
        self.blockers = [dict(x) for x in blockers]
        message = (
            f"Trade adjustment blocked by {len(self.blockers)} "
            "global pathway/product/year case(s)."
        )
        super().__init__(message)


def canonical_product(value: Any) -> str:
    """Canonical product key used only for matching/calculation."""
    if value is None:
        return ""
    return str(value).strip().casefold()


def canonical_pathway(value: Any) -> str:
    """
    Normalize an admin-configured pathway name.

    Pathway names are intentionally not hardcoded in the engine.
    The Streamlit iteration configuration defines which pathways
    are valid for a given iteration.

    The engine only needs a stable grouping label.
    """
    if value is None:
        raise SubmissionValidationError("Pathway is blank.")

    raw = str(value).strip()

    if not raw:
        raise SubmissionValidationError("Pathway is blank.")

    # Collapse repeated internal whitespace while preserving
    # the admin-selected display name/casing.
    return " ".join(raw.split())


def _required_text(value: Any, field: str) -> str:
    if value is None:
        raise SubmissionValidationError(f"{field} is blank.")
    text = str(value).strip()
    if not text:
        raise SubmissionValidationError(f"{field} is blank.")
    return text


def _finite_nonnegative(value: Any, field: str) -> float:
    if value in (None, ""):
        raise SubmissionValidationError(f"{field} is blank.")
    try:
        number = float(str(value).replace(",", "").strip())
    except Exception as exc:
        raise SubmissionValidationError(f"{field} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise SubmissionValidationError(f"{field} must be finite: {value!r}")
    if number < 0:
        raise SubmissionValidationError(f"{field} must be non-negative: {value!r}")
    return number


def _year(value: Any) -> int:
    if value in (None, ""):
        raise SubmissionValidationError("Year is blank.")
    try:
        number = float(value)
    except Exception as exc:
        raise SubmissionValidationError(f"Year is not numeric: {value!r}") from exc
    if not math.isfinite(number) or abs(number - round(number)) > 1e-9:
        raise SubmissionValidationError(f"Year must be an integer: {value!r}")
    return int(round(number))


def _first(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """Case-insensitive alias lookup in a row/payload mapping."""
    lower = {str(k).strip().casefold(): k for k in row}
    for name in names:
        key = lower.get(name.casefold())
        if key is not None:
            return row[key]
    return default


def normalize_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    default_pathway: str | None = None,
    default_country_id: str | None = None,
    default_location: str | None = None,
    default_run_id: str | None = None,
    default_iteration: str | int | None = None,
) -> list[dict[str, Any]]:
    """
    Normalize either add-in style rows or already-flattened backend rows.

    Accepted row aliases include:
      Location / Country
      CountryID / country_id
      Pathway / pathway
      Product / ProductRaw
      Year
      Import_quantity / imports
      Export_quantity / exports
    """
    normalized: list[dict[str, Any]] = []

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise SubmissionValidationError(f"Row {index} is not an object.")

        location = _first(row, "Location", "Country", default=default_location)
        location = _required_text(location, f"Row {index} Location")

        country_id = _first(row, "CountryID", "country_id", default=default_country_id)
        if country_id in (None, ""):
            # Web submissions do not need an ISO3 filename parser.
            # Location is the stable fallback key, so RMECAS and similar regions are valid.
            country_id = location
        country_id = _required_text(country_id, f"Row {index} CountryID")

        pathway_value = _first(row, "Pathway", "pathway", default=default_pathway)
        pathway = canonical_pathway(pathway_value)

        product_raw = _first(row, "ProductRaw", "Product", "product")
        product_raw = _required_text(product_raw, f"Row {index} Product")
        product_key = canonical_product(product_raw)
        if not product_key:
            raise SubmissionValidationError(f"Row {index} Product is blank.")

        year = _year(_first(row, "Year", "year"))
        imp = _finite_nonnegative(
            _first(row, "Import_quantity", "imports", "Imports", "ImportsAdj"),
            f"Row {index} Import_quantity",
        )
        exp = _finite_nonnegative(
            _first(row, "Export_quantity", "exports", "Exports", "ExportsAdj"),
            f"Row {index} Export_quantity",
        )

        normalized.append(
            {
                "Country": location,
                "CountryID": country_id,
                "Pathway": pathway,
                "ProductRaw": product_raw,
                "ProductKey": product_key,
                "Year": year,
                "Import_quantity": imp,
                "Export_quantity": exp,
                "RunID": _first(row, "RunID", "run_id", default=default_run_id),
                "Iteration": _first(row, "Iteration", "iteration", default=default_iteration),
            }
        )

    validate_iteration_rows(normalized)
    return normalized


def validate_iteration_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Validate uniqueness and the minimum fields needed by the global engine."""
    if not rows:
        raise IterationValidationError("No trade rows were supplied.")

    seen: set[tuple[str, str, str, int]] = set()
    duplicates: list[tuple[str, str, str, int]] = []

    for row in rows:
        key = (
            str(row["CountryID"]).strip(),
            canonical_pathway(row["Pathway"]),
            canonical_product(row["ProductKey"]),
            int(row["Year"]),
        )
        if key in seen:
            duplicates.append(key)
        seen.add(key)

    if duplicates:
        sample = duplicates[:5]
        raise IterationValidationError(
            f"Duplicate CountryID/Pathway/Product/Year rows found; sample: {sample}"
        )


def normalize_submission(payload: Mapping[str, Any]) -> dict[str, Any]:
    """
    Validate one 'Share your SCENATHON report' payload.

    Expected shape:
      {
        "username": "...",
        "pathway": "CurrentTrends",
        "country_id": "ARG",         # optional
        "rows": [
          {
            "Location": "Argentina",
            "Product": "barley",
            "Year": 2020,
            "Import_quantity": 1.0,
            "Export_quantity": 2.0
          },
          ...
        ]
      }

    The whole workbook is never required.
    """
    if not isinstance(payload, Mapping):
        raise SubmissionValidationError("Payload must be an object.")

    username = _required_text(_first(payload, "username", "user"), "username")
    pathway = canonical_pathway(_first(payload, "pathway", "Pathway"))
    rows_raw = _first(payload, "rows", "report", "trade")
    if not isinstance(rows_raw, Sequence) or isinstance(rows_raw, (str, bytes)):
        raise SubmissionValidationError("rows must be an array of report rows.")
    if len(rows_raw) == 0:
        raise SubmissionValidationError("rows is empty.")

    supplied_country_id = _first(payload, "country_id", "CountryID")

    # Establish unique Location from the minimal report itself.
    raw_locations = {
        str(_first(r, "Location", "Country", default="")).strip()
        for r in rows_raw
        if isinstance(r, Mapping)
        and str(_first(r, "Location", "Country", default="")).strip()
    }
    if len(raw_locations) != 1:
        raise SubmissionValidationError(
            f"Expected exactly one Location in a report, found {sorted(raw_locations)}."
        )
    location = next(iter(raw_locations))

    rows = normalize_rows(
        rows_raw,
        default_pathway=pathway,
        default_country_id=(
            str(supplied_country_id).strip()
            if supplied_country_id not in (None, "")
            else None
        ),
        default_location=location,
        default_run_id=_first(payload, "run_id", "RunID"),
        default_iteration=_first(payload, "iteration", "Iteration"),
    )

    # One submitted report must contain one country key and one pathway.
    country_ids = {str(r["CountryID"]) for r in rows}
    pathways = {str(r["Pathway"]) for r in rows}
    if len(country_ids) != 1:
        raise SubmissionValidationError(
            f"Expected one CountryID in a report, found {sorted(country_ids)}."
        )
    if pathways != {pathway}:
        raise SubmissionValidationError(
            f"Rows do not all match the submitted pathway {pathway}."
        )

    report_hash = compute_report_hash(
        country_id=next(iter(country_ids)),
        location=location,
        pathway=pathway,
        rows=rows,
    )

    return {
        "username": username,
        "country_id": next(iter(country_ids)),
        "location": location,
        "pathway": pathway,
        "row_count": len(rows),
        "report_hash": report_hash,
        "rows": rows,
    }


def compute_report_hash(
    *,
    country_id: str,
    location: str,
    pathway: str,
    rows: Sequence[Mapping[str, Any]],
) -> str:
    """Stable SHA-256 fingerprint for a submitted SCENATHON report."""
    compact_rows = sorted(
        [
            {
                "product": canonical_product(r.get("ProductKey", r.get("ProductRaw"))),
                "year": int(r["Year"]),
                "imports": float(r["Import_quantity"]),
                "exports": float(r["Export_quantity"]),
            }
            for r in rows
        ],
        key=lambda x: (x["product"], x["year"]),
    )
    payload = {
        "country_id": str(country_id).strip(),
        "location": str(location).strip(),
        "pathway": canonical_pathway(pathway),
        "rows": compact_rows,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _four_branch_factor(alpha: float, imports: float, exports: float) -> tuple[float | None, str | None]:
    """
    Clara's four branches with a zero-division guard.

    Returns (factor, blocker_reason). The important alpha=-1 / exports=0 case
    returns factor 1 because threshold is zero and the comparison succeeds
    before division.
    """
    if not math.isfinite(alpha):
        return None, "NON_FINITE_ALPHA"

    if alpha >= 0 and exports >= imports:
        upper = max(1.1, 1.0 + alpha)
        limit = imports * upper
        if exports <= limit:
            return 1.0, None
        # exports > limit implies exports > 0
        return (imports * upper) / exports, None

    if alpha >= 0 and imports > exports:
        threshold = 0.9 * imports
        if exports >= threshold:
            return 1.0, None
        if exports == 0:
            return None, "ZERO_EXPORT_DIVISION"
        return threshold / exports, None

    if alpha < 0 and imports > exports:
        lower = min(0.9, 1.0 + alpha)
        threshold = imports * lower
        if exports >= threshold:
            return 1.0, None
        if exports == 0:
            return None, "ZERO_EXPORT_DIVISION"
        return threshold / exports, None

    if alpha < 0 and exports >= imports:
        threshold = 1.1 * imports
        if exports <= threshold:
            return 1.0, None
        # exports > threshold, so exports > 0
        return threshold / exports, None

    return None, "UNCLASSIFIED_BRANCH"


def calculate_trade_adjustment(
    rows: Iterable[Mapping[str, Any]],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """
    Run the production global trade-adjustment engine.

    `rows` may come from multiple countries and pathways in the same iteration.
    Country-specific product universes are allowed.

    If strict=True (production default), any blocker raises TradeAdjustmentBlocked.
    """
    normalized = normalize_rows(rows)

    totals: dict[tuple[str, str, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
    raw_names: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in normalized:
        key = (row["Pathway"], row["ProductKey"], row["Year"])
        totals[key][0] += row["Import_quantity"]
        totals[key][1] += row["Export_quantity"]
        raw_names[(row["Pathway"], row["ProductKey"])].add(row["ProductRaw"])

    group_years: dict[tuple[str, str], set[int]] = defaultdict(set)
    for pathway, product, year in totals:
        group_years[(pathway, product)].add(year)

    group_info: dict[tuple[str, str], dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []

    # Determine the 2020 baseline once per pathway x product.
    for (pathway, product), years in sorted(group_years.items()):
        if product in EXCLUDED_PRODUCTS:
            group_info[(pathway, product)] = {
                "alpha": None,
                "mode": "EXCLUDED_PRODUCT",
            }
            continue

        key2020 = (pathway, product, 2020)
        if key2020 not in totals:
            blockers.append(
                {
                    "Pathway": pathway,
                    "Product": product,
                    "Year": 2020,
                    "Reason": "MISSING_2020_BASELINE",
                }
            )
            group_info[(pathway, product)] = {"alpha": None, "mode": "BLOCKED"}
            continue

        imp2020, exp2020 = totals[key2020]

        if imp2020 == 0 and exp2020 == 0:
            future_nonzero = any(
                y > 2020
                and (
                    totals[(pathway, product, y)][0] != 0
                    or totals[(pathway, product, y)][1] != 0
                )
                for y in years
            )
            if future_nonzero:
                blockers.append(
                    {
                        "Pathway": pathway,
                        "Product": product,
                        "Year": 2020,
                        "Reason": "UNDEFINED_2020_ALPHA_WITH_FUTURE_TRADE",
                    }
                )
                group_info[(pathway, product)] = {"alpha": None, "mode": "BLOCKED"}
            else:
                group_info[(pathway, product)] = {"alpha": None, "mode": "NO_TRADE"}
            continue

        if imp2020 == 0:
            # Avoid Inf / undefined R-style arithmetic in production.
            blockers.append(
                {
                    "Pathway": pathway,
                    "Product": product,
                    "Year": 2020,
                    "Reason": "ZERO_2020_IMPORT_WITH_NONZERO_EXPORT",
                }
            )
            group_info[(pathway, product)] = {"alpha": None, "mode": "BLOCKED"}
            continue

        alpha = (exp2020 - imp2020) / imp2020
        if not math.isfinite(alpha):
            blockers.append(
                {
                    "Pathway": pathway,
                    "Product": product,
                    "Year": 2020,
                    "Reason": "NON_FINITE_2020_ALPHA",
                }
            )
            group_info[(pathway, product)] = {"alpha": None, "mode": "BLOCKED"}
            continue

        group_info[(pathway, product)] = {"alpha": alpha, "mode": "NORMAL"}

    factors: dict[tuple[str, str, int], float | None] = {}
    global_qa: list[dict[str, Any]] = []

    for (pathway, product, year), (imp, exp) in sorted(totals.items()):
        info = group_info[(pathway, product)]
        alpha = info["alpha"]
        mode = info["mode"]
        factor: float | None
        status: str

        if year in HISTORICAL_YEARS:
            factor = 1.0
            status = "HISTORICAL"
        elif product in EXCLUDED_PRODUCTS:
            factor = 1.0
            status = "EXCLUDED_PRODUCT"
        elif mode == "NO_TRADE":
            factor = 1.0
            status = "NO_TRADE"
        elif mode == "BLOCKED":
            factor = None
            status = "BLOCKED_BASELINE"
        else:
            factor, reason = _four_branch_factor(float(alpha), imp, exp)
            if reason is not None:
                blockers.append(
                    {
                        "Pathway": pathway,
                        "Product": product,
                        "Year": year,
                        "Reason": reason,
                    }
                )
                status = f"BLOCKED_{reason}"
            elif abs(float(factor) - 1.0) <= 1e-12:
                status = "WITHIN_TOLERANCE"
            else:
                status = "ADJUSTED"

        factors[(pathway, product, year)] = factor
        global_qa.append(
            {
                "Pathway": pathway,
                "Product": product,
                "Year": year,
                "GlobalImports": imp,
                "GlobalExportsBefore": exp,
                "Alpha2020": alpha,
                "AdjFactorExport": factor,
                "GlobalExportsAfter": (None if factor is None else exp * factor),
                "Status": status,
                "RawProductVariants": sorted(raw_names[(pathway, product)]),
            }
        )

    # Deduplicate blockers produced once at baseline and/or factor level.
    dedup: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for b in blockers:
        k = (str(b["Pathway"]), str(b["Product"]), int(b["Year"]), str(b["Reason"]))
        dedup[k] = b
    blockers = list(dedup.values())

    adjusted_rows: list[dict[str, Any]] = []
    for row in normalized:
        factor = factors[(row["Pathway"], row["ProductKey"], row["Year"])]
        exp_adj = None if factor is None else row["Export_quantity"] * factor
        adjusted_rows.append(
            {
                "Country": row["Country"],
                "CountryID": row["CountryID"],
                "Pathway": row["Pathway"],
                "Product": row["ProductKey"],
                "ProductRaw": row["ProductRaw"],
                "Year": row["Year"],
                "ImportsAdj": row["Import_quantity"],
                "ExportsAdj": exp_adj,
                "ImportOriginal": row["Import_quantity"],
                "ExportOriginal": row["Export_quantity"],
                "AdjFactorExport": factor,
                "RunID": row.get("RunID"),
                "Iteration": row.get("Iteration"),
            }
        )

    result = {
        "engine_version": ENGINE_VERSION,
        "adjusted_rows": adjusted_rows,
        "global_qa": global_qa,
        "blockers": blockers,
    }

    if strict and blockers:
        raise TradeAdjustmentBlocked(blockers)

    return result


def build_injection_payload(
    adjusted_rows: Sequence[Mapping[str, Any]],
    *,
    country_id: str,
) -> dict[str, Any]:
    """
    Build the minimal response used by the future 'Inject adjusted trade' button.

    All available pathways for the country are returned so the add-in can match
    FullDataTradeAdj by Pathway + canonical Product + Year.
    """
    wanted = str(country_id).strip()
    rows = [r for r in adjusted_rows if str(r["CountryID"]).strip() == wanted]
    if not rows:
        raise TradeEngineError(f"No adjusted rows found for country_id={wanted!r}.")
    if any(r["ExportsAdj"] is None for r in rows):
        raise TradeEngineError("Cannot build injection payload while blocked rows exist.")

    payload_rows = sorted(
        [
            {
                "Pathway": r["Pathway"],
                "ProductKey": canonical_product(r["Product"]),
                "Year": int(r["Year"]),
                "ImportsAdj": float(r["ImportsAdj"]),
                "ExportsAdj": float(r["ExportsAdj"]),
            }
            for r in rows
        ],
        key=lambda x: (x["Pathway"], x["ProductKey"], x["Year"]),
    )
    return {
        "engine_version": ENGINE_VERSION,
        "country_id": wanted,
        "row_count": len(payload_rows),
        "rows": payload_rows,
    }
