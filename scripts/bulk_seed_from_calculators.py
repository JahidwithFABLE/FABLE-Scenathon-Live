from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import win32com.client  # type: ignore
except ImportError:  # pragma: no cover - Windows-only utility
    win32com = None  # type: ignore


PATHWAY_FOLDERS = {
    "CT": "CurrentTrends",
    "GS": "GlobalSustainability",
    "NC": "NationalCommitments",
}

REPORT_TABLE_CANDIDATES = {
    "reportingbyproduct",
    "scenathonreport",
}

REQUIRED_FIELDS = [
    "Location",
    "Product",
    "Year",
    "Import_quantity",
    "Export_quantity",
]

REGIONAL_FILENAME_MAP = {
    "R_ASP": "ASP",
    "R_CSA": "CSA",
    "R_NEU": "NEU",
    "R_OEU": "OEU",
    "R_SSA": "SSA",
}


def normalize_identifier(value: str) -> str:
    return re.sub(r"[\s_-]+", "", str(value).strip().lower())


def parse_country_id(path: Path) -> str:
    name = path.stem
    match = re.search(r"Iteration_\d+_(.+?)_\d{4}-\d{2}-\d{2}T", name)
    if not match:
        raise ValueError(f"Could not parse country ID from filename: {path.name}")
    token = match.group(1).strip()
    return REGIONAL_FILENAME_MAP.get(token, token)


def discover_workbooks(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for folder, pathway in PATHWAY_FOLDERS.items():
        folder_path = root / folder
        if not folder_path.is_dir():
            raise FileNotFoundError(f"Missing pathway folder: {folder_path}")

        for path in sorted(folder_path.glob("*.xlsx")):
            lower = path.name.lower()
            if path.name.startswith("~$"):
                continue
            if "tradeadjusted" in lower or "mvp_test" in lower:
                continue
            found.append(
                {
                    "path": path,
                    "pathway": pathway,
                    "country_id": parse_country_id(path),
                }
            )

    identities = [(x["country_id"], x["pathway"]) for x in found]
    if len(identities) != len(set(identities)):
        duplicates = sorted({x for x in identities if identities.count(x) > 1})
        raise ValueError(f"Duplicate country/pathway workbook identities: {duplicates}")

    return found


def _column_values(list_object: Any, column_index: int) -> list[Any]:
    body = list_object.ListColumns(column_index).DataBodyRange
    if body is None:
        return []
    values = body.Value2
    if isinstance(values, tuple):
        return [row[0] if isinstance(row, tuple) else row for row in values]
    return [values]


def _as_number(value: Any, field: str, row_number: int) -> float:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field} is blank at table row {row_number}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field} is not numeric at table row {row_number}: {value!r}"
        ) from exc
    if number < 0:
        raise ValueError(f"{field} is negative at table row {row_number}: {number}")
    return number


def extract_report_rows(excel: Any, path: Path) -> list[dict[str, Any]]:
    workbook = None
    try:
        workbook = excel.Workbooks.Open(
            str(path),
            UpdateLinks=0,
            ReadOnly=True,
            IgnoreReadOnlyRecommended=True,
        )

        report_table = None
        for sheet in workbook.Worksheets:
            for table in sheet.ListObjects:
                if normalize_identifier(table.Name) in REPORT_TABLE_CANDIDATES:
                    report_table = table
                    break
            if report_table is not None:
                break

        if report_table is None:
            raise ValueError(
                f"REPORTING_BYPRODUCT / SCENATHON_report table not found in {path.name}"
            )

        headers: dict[str, int] = {}
        for i in range(1, int(report_table.ListColumns.Count) + 1):
            header = str(report_table.ListColumns(i).Name)
            headers[normalize_identifier(header)] = i

        indices: dict[str, int] = {}
        for field in REQUIRED_FIELDS:
            key = normalize_identifier(field)
            if key not in headers:
                raise ValueError(
                    f"Required column {field!r} not found in {path.name}. "
                    f"Available: {sorted(headers)}"
                )
            indices[field] = headers[key]

        columns = {
            field: _column_values(report_table, index)
            for field, index in indices.items()
        }
        counts = {len(values) for values in columns.values()}
        if len(counts) != 1:
            raise ValueError(f"Required columns have inconsistent lengths in {path.name}")

        rows: list[dict[str, Any]] = []
        row_count = next(iter(counts), 0)
        for i in range(row_count):
            raw = {field: columns[field][i] for field in REQUIRED_FIELDS}
            if all(v is None or str(v).strip() == "" for v in raw.values()):
                continue

            location = str(raw["Location"] or "").strip()
            product = str(raw["Product"] or "")
            if not location:
                raise ValueError(f"Location is blank at table row {i + 1} in {path.name}")
            if not product.strip():
                raise ValueError(f"Product is blank at table row {i + 1} in {path.name}")

            year_number = _as_number(raw["Year"], "Year", i + 1)
            if not year_number.is_integer():
                raise ValueError(
                    f"Year is not an integer at table row {i + 1}: {year_number}"
                )

            rows.append(
                {
                    "Location": location,
                    "Product": product,
                    "Year": int(year_number),
                    "Import_quantity": _as_number(
                        raw["Import_quantity"], "Import_quantity", i + 1
                    ),
                    "Export_quantity": _as_number(
                        raw["Export_quantity"], "Export_quantity", i + 1
                    ),
                }
            )

        if not rows:
            raise ValueError(f"No usable report rows found in {path.name}")

        locations = {row["Location"] for row in rows}
        if len(locations) != 1:
            raise ValueError(
                f"Expected one Location in {path.name}, found: {sorted(locations)}"
            )

        return rows
    finally:
        if workbook is not None:
            workbook.Close(SaveChanges=False)


def api_json(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach backend at {url}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Development/validation utility: read the 84 local FABLE-C calculators "
            "with native Excel COM and submit only the minimal SCENATHON trade rows "
            "to the local API. Workbooks are opened read-only and never saved."
        )
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--iteration", default="FULLTEST-001")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="Bulk validation")
    args = parser.parse_args()

    if win32com is None:
        raise SystemExit(
            "pywin32 is required for this Windows/Excel validation utility. "
            "Install it in your environment with: conda install pywin32"
        )

    root = args.root.resolve()
    base_url = args.base_url.rstrip("/")
    workbooks = discover_workbooks(root)

    expectations = [
        {"country_id": item["country_id"], "pathway": item["pathway"]}
        for item in workbooks
    ]

    print(f"Discovered {len(workbooks)} workbook(s) under {root}")
    by_pathway: dict[str, int] = {}
    for item in workbooks:
        by_pathway[item["pathway"]] = by_pathway.get(item["pathway"], 0) + 1
    print("By pathway:", json.dumps(by_pathway, indent=2))

    completeness = api_json(
        "PUT",
        f"{base_url}/api/v1/iterations/{args.iteration}/expectations",
        {"reports": expectations},
    )
    print(
        f"Configured {completeness['expected_reports']} exact expected "
        f"country/pathway report(s) for {args.iteration}."
    )

    excel = win32com.client.DispatchEx("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    excel.AskToUpdateLinks = False

    try:
        for index, item in enumerate(workbooks, start=1):
            path: Path = item["path"]
            country_id = item["country_id"]
            pathway = item["pathway"]
            print(
                f"[{index:02d}/{len(workbooks):02d}] "
                f"{country_id} | {pathway} | {path.name}"
            )
            rows = extract_report_rows(excel, path)
            result = api_json(
                "POST",
                f"{base_url}/api/v1/submissions",
                {
                    "username": args.username,
                    "country_id": country_id,
                    "pathway": pathway,
                    "iteration_id": args.iteration,
                    "rows": rows,
                },
            )
            marker = "duplicate" if result.get("duplicate") else f"v{result['version']}"
            print(
                f"    -> {result['submission_id']} | {len(rows)} rows | {marker}"
            )
    finally:
        excel.Quit()

    completeness = api_json(
        "GET",
        f"{base_url}/api/v1/iterations/{args.iteration}/completeness",
    )
    dashboard = api_json(
        "GET",
        f"{base_url}/api/v1/dashboard/summary?iteration_id={args.iteration}",
    )

    print("\nCOMPLETENESS")
    print(json.dumps(completeness, indent=2))
    print("\nDASHBOARD")
    print(json.dumps(dashboard, indent=2))

    if not completeness.get("complete"):
        print("\nBulk seed finished, but iteration is NOT complete.", file=sys.stderr)
        return 2

    print("\nBULK SEED COMPLETE: exact expected set is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
