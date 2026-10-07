

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st


# ============================================================
# 1. APPLICATION CONFIGURATION
# ============================================================

APP_TITLE = "FABLE Scenathon Trade Adjustment"

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from trade_engine import calculate_trade_adjustment, ENGINE_VERSION

from global_results import (
    aggregate_rows_from_submissions,
    calculate_global_targets_with_coverage,
    normalize_global_indicator_rows,
    filter_global_indicator_rows,
    build_global_scope_summary,
    build_before_after_comparison,
)


LOGO_PATH = (
    REPO_ROOT
    / "addin"
    / "assets"
    / "fable-logo-full.png"
)


# ============================================================
# Production fixture mode
#
# Local development fixtures are disabled in production.
# These helpers remain only so historical fixture-aware UI
# branches can safely evaluate without affecting runtime data.
# ============================================================

LOCAL_GLOBAL_FIXTURE_ITERATION = "__LOCAL_FIXTURE_DISABLED__"


def local_global_fixture_enabled():

    return False


def local_global_fixture_config():

    return {
        "iteration": LOCAL_GLOBAL_FIXTURE_ITERATION,
        "participants": [],
        "pathways": [],
    }


def load_local_global_fixture_payloads():

    return []


DEFAULT_PATHWAYS = [
    "CurrentTrends",
    "GlobalSustainability",
    "NationalCommitments",
]

REQUIRED_COLUMNS = [
    "Location",
    "Product",
    "Year",
    "Import_quantity",
    "Export_quantity",
]

AGGREGATE_COLUMNS = [
    "Location",
    "Year",
    "kcal_feas",
    "kcal_mder",
    "CalcAllLandCO2e",
    "CalcAllAgriCO2e",
]


# ============================================================
# 2. SECRETS
# ============================================================

def get_secret(section, key, default=""):
    try:
        return st.secrets[section][key]
    except Exception:
        return os.getenv(
            f"{section}_{key}".upper(),
            default,
        )


GITHUB_TOKEN = get_secret(
    "github",
    "token",
    "",
)

GITHUB_OWNER = get_secret(
    "github",
    "owner",
    "JahidwithFABLE",
)

GITHUB_REPO = get_secret(
    "github",
    "repo",
    "FABLE-Scenathon-Trade-Adds-in-for-Excel",
)

GITHUB_BRANCH = get_secret(
    "github",
    "branch",
    "main",
)

STORAGE_ROOT = get_secret(
    "github",
    "storage_root",
    "app_storage",
).strip("/")

ADMIN_PASSWORD = get_secret(
    "admin",
    "password",
    "",
)


# ============================================================
# 3. GENERAL HELPERS
# ============================================================

def safe_name(value: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in ("-", "_")
        else "_"
        for ch in value.strip()
    )

    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")

    return cleaned.strip("_")


def storage_path(*parts) -> str:
    clean_parts = [
        str(part).strip("/")
        for part in parts
        if str(part).strip("/")
    ]

    return "/".join(
        [STORAGE_ROOT] + clean_parts
    )


def iteration_config_path(
    iteration_name: str,
) -> str:

    return storage_path(
        "iterations",
        f"{safe_name(iteration_name)}.json",
    )


def submission_folder(
    iteration_name: str,
) -> str:

    return storage_path(
        "submissions",
        safe_name(iteration_name),
    )


def result_folder(
    iteration_name: str,
) -> str:

    return storage_path(
        "results",
        safe_name(iteration_name),
    )


def analytics_path(
    iteration_name: str,
) -> str:

    return (
        f"{result_folder(iteration_name)}"
        "/_analytics.csv"
    )


def run_summary_path(
    iteration_name: str,
) -> str:

    return (
        f"{result_folder(iteration_name)}"
        "/_run_summary.json"
    )


def stale_marker_path(
    iteration_name: str,
) -> str:

    return (
        f"{result_folder(iteration_name)}"
        "/_stale.json"
    )


# ============================================================
# 4. GITHUB API
# ============================================================

def github_headers():
    return {
        "Authorization": (
            f"Bearer {GITHUB_TOKEN}"
        ),
        "Accept": (
            "application/vnd.github+json"
        ),
        "X-GitHub-Api-Version": (
            "2022-11-28"
        ),
    }


def github_repo_url():
    return (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}"
    )


def github_contents_url(path=""):
    encoded_path = quote(
        path,
        safe="/",
    )

    base = (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/contents"
    )

    if encoded_path:
        return f"{base}/{encoded_path}"

    return base


def github_connection_status():

    if not GITHUB_TOKEN:
        return (
            False,
            "GitHub token is not configured.",
        )

    try:

        response = requests.get(
            github_repo_url(),
            headers=github_headers(),
            timeout=20,
        )

        if response.status_code == 200:
            return (
                True,
                "Connected successfully.",
            )

        return (
            False,
            (
                f"GitHub HTTP "
                f"{response.status_code}: "
                f"{response.text[:300]}"
            ),
        )

    except Exception as exc:

        return False, str(exc)


def github_get_file(path: str):

    response = requests.get(
        github_contents_url(path),
        headers=github_headers(),
        params={
            "ref": GITHUB_BRANCH,
        },
        timeout=30,
    )

    if response.status_code == 404:
        return None

    response.raise_for_status()

    payload = response.json()

    if isinstance(payload, list):
        raise ValueError(
            f"{path} is a directory."
        )

    return {
        "content": base64.b64decode(
            payload["content"]
        ),
        "sha": payload["sha"],
        "path": payload["path"],
    }


def github_list_dir(path: str):

    response = requests.get(
        github_contents_url(path),
        headers=github_headers(),
        params={
            "ref": GITHUB_BRANCH,
        },
        timeout=30,
    )

    if response.status_code == 404:
        return []

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, list):
        return []

    return payload


def github_put_bytes(
    path: str,
    content: bytes,
    message: str,
):

    existing = github_get_file(path)

    payload = {
        "message": message,
        "content": base64.b64encode(
            content
        ).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }

    if existing:
        payload["sha"] = existing["sha"]

    response = requests.put(
        github_contents_url(path),
        headers=github_headers(),
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


def github_put_text(
    path: str,
    text: str,
    message: str,
):

    return github_put_bytes(
        path,
        text.encode("utf-8"),
        message,
    )


def github_put_json(
    path: str,
    payload,
    message: str,
):

    text = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
    )

    return github_put_text(
        path,
        text,
        message,
    )


def github_get_json(path: str):

    file_data = github_get_file(path)

    if not file_data:
        return None

    return json.loads(
        file_data["content"].decode(
            "utf-8"
        )
    )


def github_delete_file(
    path: str,
    message: str,
):

    existing = github_get_file(path)

    if not existing:
        return False

    response = requests.delete(
        github_contents_url(path),
        headers=github_headers(),
        json={
            "message": message,
            "sha": existing["sha"],
            "branch": GITHUB_BRANCH,
        },
        timeout=30,
    )

    response.raise_for_status()

    return True


def github_delete_tree(
    folder_path: str,
):

    items = github_list_dir(
        folder_path
    )

    deleted = 0

    for item in items:

        if item["type"] == "file":

            if github_delete_file(
                item["path"],
                (
                    "Clear FABLE app data: "
                    f"{item['path']}"
                ),
            ):
                deleted += 1

        elif item["type"] == "dir":

            deleted += github_delete_tree(
                item["path"]
            )

    return deleted


# ============================================================
# 5. ITERATION CONFIGURATION
# ============================================================

def normalize_participants(
    text: str,
):

    participants = []
    seen = set()

    for line in text.splitlines():

        value = line.strip()

        if not value:
            continue

        key = value.casefold()

        if key not in seen:
            seen.add(key)
            participants.append(value)

    return participants


def save_iteration_config(
    iteration_name: str,
    participants: list[str],
    pathways: list[str],
    is_open: bool,
):

    config = {
        "iteration": (
            iteration_name.strip()
        ),
        "participants": participants,
        "pathways": pathways,
        "status": (
            "OPEN"
            if is_open
            else "CLOSED"
        ),
        "updated_at_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
    }

    github_put_json(
        iteration_config_path(
            iteration_name
        ),
        config,
        (
            "Update FABLE iteration: "
            f"{iteration_name}"
        ),
    )


def load_iteration_config(
    iteration_name: str,
):

    return github_get_json(
        iteration_config_path(
            iteration_name
        )
    )


def list_iteration_configs():

    items = github_list_dir(
        storage_path(
            "iterations"
        )
    )

    configs = []

    for item in items:

        if (
            item["type"] == "file"
            and item["name"]
            .lower()
            .endswith(".json")
        ):

            try:

                config = github_get_json(
                    item["path"]
                )

                if config:
                    configs.append(
                        config
                    )

            except Exception:
                pass

    configs.sort(
        key=lambda x: (
            x.get(
                "iteration",
                "",
            ).casefold()
        )
    )

    return configs


def list_open_iterations():

    return [
        cfg["iteration"]
        for cfg
        in list_iteration_configs()
        if cfg.get("status") == "OPEN"
    ]


# ============================================================
# 6. REPORT READING + VALIDATION
# ============================================================


def _extract_reporting_byproduct_from_rows(rows):

    required = REQUIRED_COLUMNS

    header_index = None
    column_indexes = None

    # --------------------------------------------------------
    # Find the real REPORTING_BYPRODUCT header row
    # --------------------------------------------------------

    for row_number, row in enumerate(rows):

        cleaned = [
            "" if value is None
            else str(value).strip()
            for value in row
        ]

        if all(
            column in cleaned
            for column in required
        ):

            header_index = row_number

            column_indexes = {
                column: cleaned.index(column)
                for column in required
            }

            break


    if header_index is None:

        raise ValueError(
            "Could not find the REPORTING_BYPRODUCT header. "
            "Expected columns: "
            + ", ".join(REQUIRED_COLUMNS)
        )


    # --------------------------------------------------------
    # Extract records below header
    # --------------------------------------------------------

    extracted = []

    max_index = max(
        column_indexes.values()
    )

    started = False
    blank_after_data = 0


    for row in rows[
        header_index + 1:
    ]:

        row = list(row)

        if len(row) <= max_index:
            row += [""] * (
                max_index + 1 - len(row)
            )


        record = {}

        for column, index in column_indexes.items():

            value = row[index]

            if value is None:
                value = ""

            record[column] = value


        location = str(
            record["Location"]
        ).strip()

        product = str(
            record["Product"]
        ).strip()

        year = str(
            record["Year"]
        ).strip()


        # Fully blank table row
        if (
            not location
            and not product
            and not year
        ):

            if started:
                blank_after_data += 1

                # Avoid reading unrelated sections
                # below the reporting table
                if blank_after_data >= 2:
                    break

            continue


        blank_after_data = 0


        # Actual trade record needs these fields
        if not (
            location
            and product
            and year
        ):
            continue


        started = True

        extracted.append(record)


    if not extracted:

        raise ValueError(
            "REPORTING_BYPRODUCT header was found, "
            "but no data rows were detected."
        )


    df = pd.DataFrame(
        extracted,
        columns=REQUIRED_COLUMNS,
    )


    # --------------------------------------------------------
    # Clean values
    # --------------------------------------------------------

    df["Location"] = (
        df["Location"]
        .astype(str)
        .str.strip()
    )

    df["Product"] = (
        df["Product"]
        .astype(str)
        .str.strip()
    )


    for column in [
        "Year",
        "Import_quantity",
        "Export_quantity",
    ]:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )


    # Remove rows that are clearly not data records
    df = df.dropna(
        subset=["Year"]
    ).copy()


    if df.empty:

        raise ValueError(
            "The reporting table was found, "
            "but no valid year records were detected."
        )


    return df.reset_index(drop=True)




def _find_scenathon_table_header(
    rows,
    required_columns,
):
    """
    Find a SCENATHON_report table by column names rather
    than relying on fixed Excel row numbers.
    """

    for row_index, row in enumerate(rows):

        cleaned = [
            ""
            if value is None
            else str(value).strip()
            for value in row
        ]

        if all(
            column in cleaned
            for column in required_columns
        ):
            return (
                row_index,
                {
                    column: cleaned.index(column)
                    for column in required_columns
                },
            )

    raise ValueError(
        "Could not find a SCENATHON table containing: "
        + ", ".join(required_columns)
    )


def _extract_scenathon_table(
    rows,
    required_columns,
    identity_columns,
):
    """
    Extract one table from SCENATHON_report.

    The table is detected from its headers, so the extractor
    is resilient to changes in the physical Excel row number.
    """

    header_index, column_indexes = (
        _find_scenathon_table_header(
            rows,
            required_columns,
        )
    )

    max_index = max(
        column_indexes.values()
    )

    records = []
    started = False
    blank_after_data = 0

    for row in rows[
        header_index + 1:
    ]:

        row = list(row)

        if len(row) <= max_index:
            row += [""] * (
                max_index + 1 - len(row)
            )

        record = {
            column: row[index]
            for column, index
            in column_indexes.items()
        }

        identity_values = [
            record.get(column)
            for column in identity_columns
        ]

        fully_blank = all(
            value is None
            or str(value).strip() == ""
            for value in identity_values
        )

        if fully_blank:

            if started:

                blank_after_data += 1

                if blank_after_data >= 2:
                    break

            continue

        blank_after_data = 0

        if not all(
            value is not None
            and str(value).strip() != ""
            for value in identity_values
        ):
            continue

        started = True
        records.append(
            record
        )

    if not records:
        raise ValueError(
            "SCENATHON table header was found, "
            "but no data rows were detected."
        )

    return pd.DataFrame(
        records,
        columns=required_columns,
    )


def _clean_trade_dataframe(
    df: pd.DataFrame,
):

    df = df[
        REQUIRED_COLUMNS
    ].copy()

    df["Location"] = (
        df["Location"]
        .astype(str)
        .str.strip()
    )

    df["Product"] = (
        df["Product"]
        .astype(str)
        .str.strip()
    )

    for column in [
        "Year",
        "Import_quantity",
        "Export_quantity",
    ]:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = (
        df
        .dropna(
            subset=["Year"]
        )
        .reset_index(drop=True)
    )

    if df.empty:
        raise ValueError(
            "REPORTING_BYPRODUCT contains "
            "no valid year records."
        )

    return df


def _clean_aggregate_dataframe(
    df: pd.DataFrame,
):

    df = df[
        AGGREGATE_COLUMNS
    ].copy()

    df["Location"] = (
        df["Location"]
        .astype(str)
        .str.strip()
    )

    for column in [
        "Year",
        "kcal_feas",
        "kcal_mder",
        "CalcAllLandCO2e",
        "CalcAllAgriCO2e",
    ]:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = (
        df
        .dropna(
            subset=["Year"]
        )
        .reset_index(drop=True)
    )

    if df.empty:
        raise ValueError(
            "REPORTING_AGGREGATE contains "
            "no valid year records."
        )

    return df


def read_uploaded_fable_workbook(
    uploaded_file,
):
    """
    Read a full FABLE-C XLSX/XLSM calculator and extract
    the two datasets required for the live Scenathon:

    REPORTING_BYPRODUCT
        Location
        Product
        Year
        Import_quantity
        Export_quantity

    REPORTING_AGGREGATE
        Location
        Year
        kcal_feas
        kcal_mder
        CalcAllLandCO2e
        CalcAllAgriCO2e

    The full workbook itself does not need to be stored.
    """

    filename = (
        uploaded_file.name.lower()
    )

    if not (
        filename.endswith(".xlsx")
        or filename.endswith(".xlsm")
    ):
        raise ValueError(
            "Full FABLE-C submission must be "
            "an XLSX or XLSM workbook."
        )

    from openpyxl import load_workbook

    uploaded_file.seek(0)

    wb = load_workbook(
        uploaded_file,
        data_only=True,
        read_only=True,
    )

    candidate_names = [
        "SCENATHON_report",
        "SCENATHON Report",
        "Scenathon_report",
    ]

    selected_sheet = next(
        (
            sheet_name
            for sheet_name in candidate_names
            if sheet_name in wb.sheetnames
        ),
        None,
    )

    if selected_sheet is None:

        wb.close()

        raise ValueError(
            "SCENATHON_report sheet "
            "was not found in the workbook."
        )

    ws = wb[
        selected_sheet
    ]

    rows = list(
        ws.iter_rows(
            values_only=True
        )
    )

    wb.close()


    # --------------------------------------------------------
    # REPORTING_BYPRODUCT
    # --------------------------------------------------------

    trade_df = (
        _extract_scenathon_table(
            rows,
            REQUIRED_COLUMNS,
            identity_columns=[
                "Location",
                "Product",
                "Year",
            ],
        )
    )

    trade_df = (
        _clean_trade_dataframe(
            trade_df
        )
    )


    # --------------------------------------------------------
    # REPORTING_AGGREGATE
    # --------------------------------------------------------

    aggregate_df = (
        _extract_scenathon_table(
            rows,
            AGGREGATE_COLUMNS,
            identity_columns=[
                "Location",
                "Year",
            ],
        )
    )

    aggregate_df = (
        _clean_aggregate_dataframe(
            aggregate_df
        )
    )


    # --------------------------------------------------------
    # LOCATION CONSISTENCY
    # --------------------------------------------------------

    trade_locations = {
        str(value).strip()
        for value
        in trade_df[
            "Location"
        ].dropna()
        if str(value).strip()
    }

    aggregate_locations = {
        str(value).strip()
        for value
        in aggregate_df[
            "Location"
        ].dropna()
        if str(value).strip()
    }

    if len(trade_locations) != 1:

        raise ValueError(
            "Expected exactly one region in "
            "REPORTING_BYPRODUCT, found: "
            f"{sorted(trade_locations)}"
        )

    if len(aggregate_locations) != 1:

        raise ValueError(
            "Expected exactly one region in "
            "REPORTING_AGGREGATE, found: "
            f"{sorted(aggregate_locations)}"
        )

    if (
        trade_locations
        != aggregate_locations
    ):
        raise ValueError(
            "Location mismatch between "
            "REPORTING_BYPRODUCT and "
            "REPORTING_AGGREGATE."
        )

    location = next(
        iter(
            trade_locations
        )
    )

    return {
        "location": location,
        "trade_df": trade_df,
        "aggregate_df": aggregate_df,
        "trade_row_count": len(
            trade_df
        ),
        "aggregate_row_count": len(
            aggregate_df
        ),
    }




def read_uploaded_report(uploaded_file):
    """
    Backward-compatible trade-only reader.

    CSV submissions continue to use the legacy
    REPORTING_BYPRODUCT parser.

    XLSX/XLSM submissions are now processed by the
    full FABLE-C workbook extractor, while this wrapper
    returns only the trade table so existing app code
    continues to work until the submission UI is upgraded.
    """

    filename = uploaded_file.name.lower()


    # ========================================================
    # CSV - LEGACY TRADE-ONLY SUPPORT
    # ========================================================

    if filename.endswith(".csv"):

        raw_bytes = (
            uploaded_file.getvalue()
        )

        try:

            text = raw_bytes.decode(
                "utf-8-sig"
            )

        except UnicodeDecodeError:

            text = raw_bytes.decode(
                "cp1252",
                errors="replace",
            )

        try:

            dialect = (
                csv.Sniffer().sniff(
                    text[:20000],
                    delimiters=",;\t",
                )
            )

            delimiter = (
                dialect.delimiter
            )

        except Exception:

            delimiter = ";"

        rows = list(
            csv.reader(
                io.StringIO(text),
                delimiter=delimiter,
            )
        )

        return (
            _extract_reporting_byproduct_from_rows(
                rows
            )
        )


    # ========================================================
    # FULL FABLE-C XLSX / XLSM
    # ========================================================

    if (
        filename.endswith(".xlsx")
        or filename.endswith(".xlsm")
    ):

        bundle = (
            read_uploaded_fable_workbook(
                uploaded_file
            )
        )

        return bundle[
            "trade_df"
        ]


    raise ValueError(
        "Please upload a CSV, XLSX or XLSM file."
    )



def validate_report(
    df: pd.DataFrame,
):

    return [
        column
        for column in REQUIRED_COLUMNS
        if column not in df.columns
    ]


# ============================================================
# 7. SUBMISSION STORAGE
# ============================================================

SUBMISSION_STAGE_LABELS = {
    "before": "Before trade adjustment",
    "after": "After trade adjustment",
}


def normalize_submission_stage(
    stage: str,
) -> str:

    value = str(
        stage
    ).strip().casefold()

    if value not in (
        "before",
        "after",
    ):

        raise ValueError(
            "Submission stage must be "
            "'before' or 'after'."
        )

    return value


def submission_stage_folder(
    iteration: str,
    stage: str,
):

    stage = (
        normalize_submission_stage(
            stage
        )
    )

    return (
        f"{submission_folder(iteration)}"
        f"/{stage}"
    )


def submission_path(
    iteration: str,
    participant: str,
    pathway: str,
    stage: str = "before",
):

    stage = (
        normalize_submission_stage(
            stage
        )
    )

    filename = (
        f"{safe_name(participant)}"
        f"__{safe_name(pathway)}.json"
    )

    return (
        f"{submission_stage_folder(iteration, stage)}"
        f"/{filename}"
    )


def legacy_submission_path(
    iteration: str,
    participant: str,
    pathway: str,
):

    """
    Path used before schema version 2.0.

    Legacy flat submissions are interpreted
    as BEFORE trade adjustment.
    """

    filename = (
        f"{safe_name(participant)}"
        f"__{safe_name(pathway)}.json"
    )

    return (
        f"{submission_folder(iteration)}"
        f"/{filename}"
    )


def _rows_hash(
    rows,
) -> str:

    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        canonical.encode(
            "utf-8"
        )
    ).hexdigest()


def _submission_content_hash(
    rows,
    aggregate_rows,
) -> str:

    return _rows_hash(
        {
            "rows": rows,
            "aggregate_rows": aggregate_rows,
        }
    )


def dataframe_report_hash(
    df: pd.DataFrame,
) -> str:

    clean_df = (
        df[
            REQUIRED_COLUMNS
        ].copy()
    )

    rows = json.loads(
        clean_df.to_json(
            orient="records"
        )
    )

    return _rows_hash(
        rows
    )


def get_submission_payload(
    iteration: str,
    participant: str,
    pathway: str,
    stage: str = "before",
):

    stage = (
        normalize_submission_stage(
            stage
        )
    )


    # --------------------------------------------------------
    # New schema 2.0 staged storage
    # --------------------------------------------------------

    payload = github_get_json(
        submission_path(
            iteration,
            participant,
            pathway,
            stage,
        )
    )

    if payload:

        return payload


    # --------------------------------------------------------
    # Legacy flat storage = BEFORE
    # --------------------------------------------------------

    if stage == "before":

        legacy = github_get_json(
            legacy_submission_path(
                iteration,
                participant,
                pathway,
            )
        )

        if legacy:

            legacy = dict(
                legacy
            )

            legacy.setdefault(
                "schema_version",
                "1.0",
            )

            legacy.setdefault(
                "submission_stage",
                "before",
            )

            legacy.setdefault(
                "aggregate_rows",
                [],
            )

            legacy.setdefault(
                "aggregate_row_count",
                len(
                    legacy.get(
                        "aggregate_rows",
                        [],
                    )
                ),
            )

            return legacy


    return None


def iteration_has_published_results(
    iteration: str,
) -> bool:

    return bool(
        github_get_file(
            run_summary_path(
                iteration
            )
        )
        or github_get_file(
            analytics_path(
                iteration
            )
        )
    )


def mark_iteration_results_stale(
    iteration: str,
    *,
    reason: str,
):

    if not iteration_has_published_results(
        iteration
    ):

        return


    github_put_json(
        stale_marker_path(
            iteration
        ),
        {
            "iteration": iteration,
            "stale": True,
            "reason": reason,
            "marked_at_utc": (
                datetime.now(
                    timezone.utc
                ).isoformat()
            ),
        },
        (
            "Mark trade adjustment results stale: "
            f"{iteration}"
        ),
    )


def clear_iteration_stale_marker(
    iteration: str,
):

    github_delete_file(
        stale_marker_path(
            iteration
        ),
        (
            "Clear stale marker after trade "
            f"adjustment rerun: {iteration}"
        ),
    )


def load_stale_marker(
    iteration: str,
):

    return github_get_json(
        stale_marker_path(
            iteration
        )
    )


def save_submission(
    iteration: str,
    username: str,
    participant: str,
    pathway: str,
    df: pd.DataFrame,
    *,
    aggregate_df: pd.DataFrame | None = None,
    stage: str = "before",
    source_workbook: str = "",
    replace_existing: bool = False,
):

    stage = (
        normalize_submission_stage(
            stage
        )
    )


    # --------------------------------------------------------
    # TRADE ROWS
    # --------------------------------------------------------

    clean_df = (
        df[
            REQUIRED_COLUMNS
        ].copy()
    )

    rows = json.loads(
        clean_df.to_json(
            orient="records"
        )
    )


    # --------------------------------------------------------
    # AGGREGATE ROWS
    # --------------------------------------------------------

    if aggregate_df is None:

        aggregate_rows = []

    else:

        clean_aggregate_df = (
            aggregate_df[
                AGGREGATE_COLUMNS
            ].copy()
        )

        aggregate_rows = json.loads(
            clean_aggregate_df.to_json(
                orient="records"
            )
        )


    # --------------------------------------------------------
    # COMBINED HASH
    # --------------------------------------------------------

    report_hash = (
        _submission_content_hash(
            rows,
            aggregate_rows,
        )
    )


    path = submission_path(
        iteration,
        participant,
        pathway,
        stage,
    )


    existing = (
        get_submission_payload(
            iteration,
            participant,
            pathway,
            stage,
        )
    )


    if existing:

        old_hash = (
            existing.get(
                "report_hash"
            )
        )


        if not old_hash:

            old_hash = (
                _submission_content_hash(
                    existing.get(
                        "rows",
                        [],
                    ),
                    existing.get(
                        "aggregate_rows",
                        [],
                    ),
                )
            )


        # Legacy reports used only trade rows
        legacy_trade_hash = (
            _rows_hash(
                rows
            )
        )


        if (
            old_hash == report_hash
            or old_hash == legacy_trade_hash
        ):

            return {
                "path": path,
                "status": "DUPLICATE",
                "replaced": False,
                "report_hash": report_hash,
            }


        if not replace_existing:

            raise FileExistsError(
                "A different calculator is already "
                "submitted for this iteration, "
                "participant, pathway and stage."
            )


    # --------------------------------------------------------
    # SCHEMA 2.0 PAYLOAD
    # --------------------------------------------------------

    payload = {
        "schema_version": "2.0",

        "iteration": (
            iteration
        ),

        "username": (
            username
        ),

        "participant": (
            participant
        ),

        "pathway": (
            pathway
        ),

        "submission_stage": (
            stage
        ),

        "submission_stage_label": (
            SUBMISSION_STAGE_LABELS[
                stage
            ]
        ),

        "source_workbook": (
            source_workbook
        ),

        "submitted_at_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),

        "row_count": (
            len(
                rows
            )
        ),

        "trade_row_count": (
            len(
                rows
            )
        ),

        "aggregate_row_count": (
            len(
                aggregate_rows
            )
        ),

        "report_hash": (
            report_hash
        ),

        # Existing trade-engine data
        "rows": (
            rows
        ),

        # New global indicator data
        "aggregate_rows": (
            aggregate_rows
        ),
    }


    github_put_json(
        path,
        payload,
        (
            (
                "Replace FABLE-C calculator: "
                if existing
                else
                "Submit FABLE-C calculator: "
            )
            + f"{iteration} / "
            + f"{participant} / "
            + f"{pathway} / "
            + f"{stage}"
        ),
    )


    # --------------------------------------------------------
    # STALE TRADE RESULT RULE
    #
    # Only BEFORE submissions feed the trade engine.
    # AFTER submissions therefore do not make adjusted
    # trade outputs stale.
    # --------------------------------------------------------

    if stage == "before":

        mark_iteration_results_stale(
            iteration,
            reason=(
                (
                    "A BEFORE submission was replaced."
                )
                if existing
                else (
                    "A new BEFORE submission was "
                    "added after a run."
                )
            ),
        )


    return {
        "path": path,

        "status": (
            "REPLACED"
            if existing
            else
            "SUBMITTED"
        ),

        "replaced": (
            bool(
                existing
            )
        ),

        "report_hash": (
            report_hash
        ),
    }


def _payload_identity_key(
    payload,
):

    return (
        str(
            payload.get(
                "submission_stage",
                "before",
            )
        )
        .strip()
        .casefold(),

        str(
            payload.get(
                "participant",
                "",
            )
        )
        .strip()
        .casefold(),

        str(
            payload.get(
                "pathway",
                "",
            )
        )
        .strip()
        .casefold(),
    )


def _read_submission_json_files(
    folder,
):

    payloads = []

    for item in github_list_dir(
        folder
    ):

        if (
            item.get(
                "type"
            )
            == "file"
            and item.get(
                "name",
                "",
            )
            .lower()
            .endswith(
                ".json"
            )
        ):

            try:

                payload = github_get_json(
                    item[
                        "path"
                    ]
                )

                if payload:

                    payloads.append(
                        payload
                    )

            except Exception:

                pass


    return payloads


def list_submission_payloads(
    iteration: str,
    stage: str | None = "before",
):

    """
    stage="before"
        Used by trade adjustment.

    stage="after"
        Used by final global indicators.

    stage=None
        Return both.

    Legacy flat submissions are interpreted as BEFORE.

    New staged submissions override equivalent legacy
    submissions when both exist.
    """

    if stage is not None:

        stage = (
            normalize_submission_stage(
                stage
            )
        )


    merged = {}


    # --------------------------------------------------------
    # LEGACY FLAT SUBMISSIONS = BEFORE
    # --------------------------------------------------------

    if (
        stage is None
        or stage == "before"
    ):

        legacy_payloads = (
            _read_submission_json_files(
                submission_folder(
                    iteration
                )
            )
        )


        for payload in legacy_payloads:

            payload = dict(
                payload
            )

            payload.setdefault(
                "schema_version",
                "1.0",
            )

            payload.setdefault(
                "submission_stage",
                "before",
            )

            payload.setdefault(
                "aggregate_rows",
                [],
            )

            payload.setdefault(
                "aggregate_row_count",
                len(
                    payload.get(
                        "aggregate_rows",
                        [],
                    )
                ),
            )

            merged[
                _payload_identity_key(
                    payload
                )
            ] = payload


    # --------------------------------------------------------
    # NEW STAGED SUBMISSIONS
    # --------------------------------------------------------

    stages_to_read = (
        [
            stage
        ]
        if stage is not None
        else [
            "before",
            "after",
        ]
    )


    for stage_name in stages_to_read:

        staged_payloads = (
            _read_submission_json_files(
                submission_stage_folder(
                    iteration,
                    stage_name,
                )
            )
        )


        for payload in staged_payloads:

            payload = dict(
                payload
            )

            payload.setdefault(
                "schema_version",
                "2.0",
            )

            payload.setdefault(
                "submission_stage",
                stage_name,
            )

            payload.setdefault(
                "aggregate_rows",
                [],
            )

            payload.setdefault(
                "aggregate_row_count",
                len(
                    payload.get(
                        "aggregate_rows",
                        [],
                    )
                ),
            )


            # New schema takes priority over legacy
            merged[
                _payload_identity_key(
                    payload
                )
            ] = payload


    submissions = list(
        merged.values()
    )


    submissions.sort(
        key=lambda payload: (
            str(
                payload.get(
                    "participant",
                    "",
                )
            ).casefold(),

            str(
                payload.get(
                    "pathway",
                    "",
                )
            ).casefold(),

            str(
                payload.get(
                    "submission_stage",
                    "before",
                )
            ).casefold(),
        )
    )


    return submissions


def submission_metadata_df(
    iteration: str,
    stage: str | None = "before",
):

    rows = []


    for payload in (
        list_submission_payloads(
            iteration,
            stage=stage,
        )
    ):

        rows.append(
            {
                "participant": (
                    payload.get(
                        "participant",
                        ""
                    )
                ),

                "pathway": (
                    payload.get(
                        "pathway",
                        ""
                    )
                ),

                "submission_stage": (
                    payload.get(
                        "submission_stage",
                        "before",
                    )
                ),

                "username": (
                    payload.get(
                        "username",
                        ""
                    )
                ),

                "submitted_at_utc": (
                    payload.get(
                        "submitted_at_utc",
                        ""
                    )
                ),

                "row_count": (
                    payload.get(
                        "row_count",
                        payload.get(
                            "trade_row_count",
                            0,
                        ),
                    )
                ),

                "aggregate_row_count": (
                    payload.get(
                        "aggregate_row_count",
                        0,
                    )
                ),
            }
        )


    return pd.DataFrame(
        rows
    )



# ============================================================
# 8. EXPECTED SUBMISSIONS
# ============================================================

def build_expected_matrix(
    participants,
    pathways,
):

    rows = []

    for participant in participants:

        for pathway in pathways:

            rows.append(
                {
                    "participant": participant,
                    "pathway": pathway,
                }
            )

    return pd.DataFrame(rows)


# ============================================================
# 9. RESULT STORAGE
# ============================================================

def result_path(
    iteration: str,
    participant: str,
):

    return (
        f"{result_folder(iteration)}/"
        f"{safe_name(participant)}"
        "_adjusted_trade.csv"
    )




# ============================================================
# TRADE ENGINE - BUILD INPUT
# ============================================================

def build_engine_rows(
    iteration: str,
):

    # ========================================================
    # CRITICAL WORKFLOW RULE
    #
    # BEFORE submissions:
    #     feed global trade adjustment.
    #
    # AFTER submissions:
    #     never feed the trade engine.
    # ========================================================

    submissions = (
        list_submission_payloads(
            iteration,
            stage="before",
        )
    )


    engine_rows = []


    for submission in submissions:

        participant = str(
            submission.get(
                "participant",
                "",
            )
        ).strip()


        pathway = str(
            submission.get(
                "pathway",
                "",
            )
        ).strip()


        rows = submission.get(
            "rows",
            [],
        )


        for row in rows:

            location = str(
                row.get(
                    "Location",
                    "",
                )
            ).strip()


            product = str(
                row.get(
                    "Product",
                    "",
                )
            ).strip()


            if not location or not product:

                continue


            engine_rows.append(
                {
                    "Country": (
                        location
                    ),

                    "CountryID": (
                        participant
                    ),

                    "Pathway": (
                        pathway
                    ),

                    "Product": (
                        product
                    ),

                    "Year": (
                        row.get(
                            "Year"
                        )
                    ),

                    "Import_quantity": (
                        row.get(
                            "Import_quantity"
                        )
                    ),

                    "Export_quantity": (
                        row.get(
                            "Export_quantity"
                        )
                    ),

                    "Iteration": (
                        iteration
                    ),

                    "RunID": (
                        None
                    ),
                }
            )


    return (
        engine_rows,
        submissions,
    )



# ============================================================
# ANALYTICS STORAGE + HELPERS
# ============================================================

def build_iteration_analytics(
    adjusted_df: pd.DataFrame,
    iteration: str,
) -> pd.DataFrame:

    required = [
        "Pathway",
        "Product",
        "Year",
        "ImportOriginal",
        "ExportOriginal",
        "ImportsAdj",
        "ExportsAdj",
    ]

    missing = [
        column
        for column in required
        if column not in adjusted_df.columns
    ]

    if missing:
        raise ValueError(
            "Adjusted rows are missing analytics "
            "columns: " + ", ".join(missing)
        )

    work = adjusted_df.copy()

    if "ProductRaw" in work.columns:
        work["ProductDisplay"] = (
            work["ProductRaw"]
            .fillna(work["Product"])
            .astype(str)
        )
    else:
        work["ProductDisplay"] = (
            work["Product"].astype(str)
        )

    for column in [
        "Year",
        "ImportOriginal",
        "ExportOriginal",
        "ImportsAdj",
        "ExportsAdj",
    ]:
        work[column] = pd.to_numeric(
            work[column],
            errors="coerce",
        )

    work = work.dropna(
        subset=[
            "Pathway",
            "Product",
            "Year",
        ]
    )

    analytics = (
        work
        .groupby(
            [
                "Pathway",
                "Product",
                "Year",
            ],
            as_index=False,
            dropna=False,
        )
        .agg(
            ProductDisplay=(
                "ProductDisplay",
                "first",
            ),
            ImportsOriginal=(
                "ImportOriginal",
                "sum",
            ),
            ExportsOriginal=(
                "ExportOriginal",
                "sum",
            ),
            ImportsAdj=(
                "ImportsAdj",
                lambda series: series.sum(min_count=1),
            ),
            ExportsAdj=(
                "ExportsAdj",
                lambda series: series.sum(min_count=1),
            ),
        )
    )

    analytics.insert(
        0,
        "Iteration",
        iteration,
    )

    analytics["Year"] = (
        analytics["Year"].astype(int)
    )

    analytics["TradeGapBefore"] = (
        analytics["ImportsOriginal"]
        - analytics["ExportsOriginal"]
    ).abs()

    analytics["TradeGapAfter"] = (
        analytics["ImportsAdj"]
        - analytics["ExportsAdj"]
    ).abs()

    analytics["ExportAdjustment"] = (
        analytics["ExportsAdj"]
        - analytics["ExportsOriginal"]
    )

    analytics["AbsoluteExportAdjustment"] = (
        analytics["ExportAdjustment"].abs()
    )

    return (
        analytics
        .sort_values(
            [
                "Pathway",
                "ProductDisplay",
                "Year",
            ],
            kind="stable",
        )
        .reset_index(drop=True)
    )


def load_iteration_analytics(
    iteration: str,
):

    file_data = github_get_file(
        analytics_path(iteration)
    )

    if not file_data:
        return pd.DataFrame()

    return pd.read_csv(
        io.BytesIO(
            file_data["content"]
        )
    )


def load_run_summary(
    iteration: str,
):

    return (
        github_get_json(
            run_summary_path(iteration)
        )
        or {}
    )


def load_all_published_analytics():

    frames = []

    for config in list_iteration_configs():

        iteration = config.get(
            "iteration",
            "",
        )

        if not iteration:
            continue

        if load_stale_marker(iteration):
            continue

        try:
            frame = load_iteration_analytics(
                iteration
            )
        except Exception:
            continue

        if not frame.empty:
            frames.append(frame)

    if not frames:
        return pd.DataFrame()

    return pd.concat(
        frames,
        ignore_index=True,
    )


def aggregate_trade_view(
    analytics_df: pd.DataFrame,
    *,
    pathway: str,
    product_display: str,
):

    filtered = analytics_df[
        analytics_df["Pathway"].astype(str)
        == str(pathway)
    ].copy()

    if product_display != "All products":
        filtered = filtered[
            filtered["ProductDisplay"].astype(str)
            == str(product_display)
        ].copy()

    if filtered.empty:
        return filtered

    return (
        filtered
        .groupby(
            "Year",
            as_index=False,
        )
        .agg(
            ImportsOriginal=(
                "ImportsOriginal",
                "sum",
            ),
            ExportsOriginal=(
                "ExportsOriginal",
                "sum",
            ),
            ImportsAdj=(
                "ImportsAdj",
                lambda series: series.sum(min_count=1),
            ),
            ExportsAdj=(
                "ExportsAdj",
                lambda series: series.sum(min_count=1),
            ),
        )
        .sort_values("Year")
        .assign(
            TradeGapBefore=lambda d: (
                d["ImportsOriginal"]
                - d["ExportsOriginal"]
            ).abs(),
            TradeGapAfter=lambda d: (
                d["ImportsAdj"]
                - d["ExportsAdj"]
            ).abs(),
            ExportAdjustment=lambda d: (
                d["ExportsAdj"]
                - d["ExportsOriginal"]
            ),
        )
    )


# ============================================================
# TRADE ENGINE - RUN + PUBLISH RESULTS
# ============================================================

def run_iteration_trade_adjustment(
    iteration: str,
):

    engine_rows, submissions = (
        build_engine_rows(
            iteration
        )
    )

    if not engine_rows:
        raise ValueError(
            "No submitted trade rows "
            "are available for this iteration."
        )

    result = calculate_trade_adjustment(
        engine_rows,
        strict=False,
    )

    adjusted_rows = result.get(
        "adjusted_rows",
        [],
    )
    blockers = result.get(
        "blockers",
        [],
    )
    global_qa = result.get(
        "global_qa",
        [],
    )

    if not adjusted_rows:
        raise ValueError(
            "Trade engine returned no adjusted rows."
        )

    adjusted_df = pd.DataFrame(
        adjusted_rows
    )

    participants = sorted(
        {
            str(
                submission.get(
                    "participant",
                    "",
                )
            ).strip()
            for submission in submissions
            if str(
                submission.get(
                    "participant",
                    "",
                )
            ).strip()
        }
    )

    published_files = []

    for participant in participants:

        participant_df = adjusted_df[
            adjusted_df["CountryID"].astype(str)
            == participant
        ].copy()

        if participant_df.empty:
            continue

        output_df = (
            participant_df[
                [
                    "Pathway",
                    "ProductRaw",
                    "Year",
                    "ImportsAdj",
                    "ExportsAdj",
                ]
            ]
            .rename(
                columns={
                    "ProductRaw": "Product"
                }
            )
            .sort_values(
                [
                    "Pathway",
                    "Product",
                    "Year",
                ],
                kind="stable",
            )
            .reset_index(drop=True)
        )

        output_df = output_df[
            [
                "Pathway",
                "Product",
                "Year",
                "ImportsAdj",
                "ExportsAdj",
            ]
        ]

        csv_bytes = output_df.to_csv(
            index=False
        ).encode("utf-8-sig")

        path = result_path(
            iteration,
            participant,
        )

        github_put_bytes(
            path,
            csv_bytes,
            (
                "Publish trade adjustment result: "
                f"{iteration} / {participant}"
            ),
        )

        published_files.append(
            {
                "participant": participant,
                "rows": len(output_df),
                "path": path,
            }
        )

    analytics_df = build_iteration_analytics(
        adjusted_df,
        iteration,
    )

    github_put_bytes(
        analytics_path(iteration),
        analytics_df.to_csv(
            index=False
        ).encode("utf-8-sig"),
        (
            "Publish trade analytics: "
            f"{iteration}"
        ),
    )

    config = (
        load_iteration_config(iteration)
        or {}
    )

    expected_reports = (
        len(config.get("participants", []))
        * len(config.get("pathways", []))
    )

    submitted_pairs = {
        (
            str(item.get("participant", "")).strip(),
            str(item.get("pathway", "")).strip(),
        )
        for item in submissions
        if str(
            item.get("participant", "")
        ).strip()
        and str(
            item.get("pathway", "")
        ).strip()
    }

    submitted_reports = len(
        submitted_pairs
    )

    complete = bool(
        expected_reports
        and submitted_reports >= expected_reports
    )

    publication_status = (
        "FINAL"
        if complete and not blockers
        else "PROVISIONAL"
    )

    run_summary = {
        "iteration": iteration,
        "engine_version": result.get(
            "engine_version",
            ENGINE_VERSION,
        ),
        "run_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "status": publication_status,
        "complete": complete,
        "expected_reports": expected_reports,
        "reports_used": len(submissions),
        "submitted_reports": submitted_reports,
        "input_rows": len(engine_rows),
        "adjusted_rows": len(adjusted_rows),
        "analytics_rows": len(analytics_df),
        "blocker_count": len(blockers),
        "published_participants": len(
            published_files
        ),
        "published_files": published_files,
        "analytics_path": analytics_path(
            iteration
        ),
    }

    github_put_json(
        run_summary_path(iteration),
        run_summary,
        (
            "Publish trade adjustment "
            f"run summary: {iteration}"
        ),
    )

    clear_iteration_stale_marker(
        iteration
    )

    return {
        "engine_version": result.get(
            "engine_version",
            ENGINE_VERSION,
        ),
        "input_rows": len(engine_rows),
        "adjusted_rows": len(adjusted_rows),
        "analytics_rows": len(analytics_df),
        "reports_used": len(submissions),
        "participants": len(
            published_files
        ),
        "status": publication_status,
        "complete": complete,
        "expected_reports": expected_reports,
        "submitted_reports": submitted_reports,
        "published_files": published_files,
        "blockers": blockers,
        "global_qa": global_qa,
    }


# ============================================================
# 10. PAGE SETUP
# ============================================================

st.set_page_config(
    page_title=APP_TITLE,
    layout="wide",
)


if LOGO_PATH.exists():

    st.image(
        str(LOGO_PATH),
        width=300,
    )


st.title(APP_TITLE)

st.caption(
    "Submit SCENATHON reports, monitor participation, "
    "run the trade adjustment and retrieve "
    "adjusted trade results."
)


# ============================================================
# 11. STORAGE CONNECTION CHECK
# ============================================================

with st.spinner(
    "Connecting to private app storage..."
):

    connection_ok, connection_message = (
        github_connection_status()
    )

if not connection_ok:

    st.error(
        "GitHub storage is not connected."
    )

    st.code(
        connection_message
    )

    st.info(
        "Check .streamlit/secrets.toml"
    )

    st.stop()


# ============================================================
# 12. MAIN TABS
# ============================================================

submit_tab, result_tab, analytics_tab, admin_tab = (
    st.tabs(
        [
            "Submit Report",
            "Download Result",
            "Analytics",
            "Admin",
        ]
    )
)


# ============================================================
# 13. USER - SUBMIT REPORT
# ============================================================

with submit_tab:

    st.subheader(
        "Submit FABLE-C calculator"
    )

    st.caption(
        'Upload the full FABLE-C workbook. Required trade and global indicator data are extracted automatically from SCENATHON report.'
    )


    try:

        open_iterations = (
            list_open_iterations()
        )

    except Exception as exc:

        st.error(
            f"Could not load iterations: {exc}"
        )

        open_iterations = []


    if not open_iterations:

        st.info(
            "There is currently no iteration "
            "open for submissions."
        )


    else:

        selected_iteration = (
            st.selectbox(
                "Iteration",
                open_iterations,
            )
        )


        config = (
            load_iteration_config(
                selected_iteration
            )
        )


        participants = (
            config.get(
                "participants",
                [],
            )
        )


        pathways = (
            config.get(
                "pathways",
                [],
            )
        )


        with st.form(
            "submission_form"
        ):

            col1, col2 = (
                st.columns(
                    2
                )
            )


            with col1:

                username = (
                    st.text_input(
                        "Your name",
                        placeholder=(
                            "e.g. Aline"
                        ),
                    )
                )


                participant = (
                    st.selectbox(
                        "Country / participant",
                        participants,
                    )
                )


            with col2:

                pathway = (
                    st.selectbox(
                        "Pathway",
                        pathways,
                    )
                )


                stage_label = (
                    st.selectbox(
                        "Submission stage",
                        [
                            "Select submission stage...",
                            "Before trade adjustment",
                            "After trade adjustment",
                        ],
                        index=0,
                        help=(
                            "Before trade adjustment: "
                            "Calculator has NOT yet applied "
                            "adjusted trade for this pathway."
                            "\n\n"
                            "After trade adjustment: "
                            "Calculator HAS applied adjusted "
                            "trade for this pathway."
                        ),
                    )
                )


            uploaded_file = (
                st.file_uploader(
                    "Upload full FABLE-C calculator",
                    type=[
                        "xlsx",
                        "xlsm",
                    ],
                    help=(
                        "Upload the complete FABLE-C "
                        "workbook containing "
                        "SCENATHON_report."
                    ),
                )
            )


            replace_existing = (
                st.checkbox(
                    "Replace the existing submission "
                    "for this exact participant / "
                    "pathway / stage.",
                    help=(
                        "Use this only when you intentionally "
                        "want to replace a previously submitted "
                        "calculator for the same iteration, "
                        "participant, pathway and stage."
                    ),
                )
            )


            submitted = (
                st.form_submit_button(
                    "Submit calculator",
                    type="primary",
                )
            )


        # ====================================================
        # PROCESS SUBMISSION
        # ====================================================

        if submitted:

            stage_map = {
                "Before trade adjustment": (
                    "before"
                ),

                "After trade adjustment": (
                    "after"
                ),
            }


            submission_stage = (
                stage_map.get(
                    stage_label
                )
            )


            if not username.strip():

                st.error(
                    "Please enter your name."
                )


            elif submission_stage is None:

                st.error(
                    "Please select whether this "
                    "calculator is Before or "
                    "After trade adjustment."
                )


            elif uploaded_file is None:

                st.error(
                    "Please upload the full "
                    "FABLE-C calculator."
                )


            else:

                submission_status = (
                    None
                )


                try:

                    with st.status(
                        "Processing FABLE-C calculator...",
                        expanded=True,
                    ) as submission_status:


                        submission_progress = (
                            st.progress(
                                0
                            )
                        )


                        # =====================================
                        # STEP 1 - PREPARE
                        # =====================================

                        submission_status.write(
                            "Preparing uploaded workbook..."
                        )

                        submission_progress.progress(
                            10
                        )


                        # =====================================
                        # STEP 2 - READ WORKBOOK
                        # =====================================

                        submission_status.write(
                            'Reading SCENATHON report from the calculator...'
                        )

                        submission_progress.progress(
                            20
                        )


                        # =====================================
                        # STEP 3 - EXTRACT
                        # =====================================

                        submission_status.write(
                            "Extracting trade data and "
                            "aggregate indicators..."
                        )


                        workbook_bundle = (
                            read_uploaded_fable_workbook(
                                uploaded_file
                            )
                        )


                        trade_df = (
                            workbook_bundle[
                                "trade_df"
                            ]
                        )


                        aggregate_df = (
                            workbook_bundle[
                                "aggregate_df"
                            ]
                        )


                        workbook_location = (
                            workbook_bundle[
                                "location"
                            ]
                        )


                        submission_progress.progress(
                            50
                        )


                        # =====================================
                        # STEP 4 - VALIDATE
                        # =====================================

                        submission_status.write(
                            "Validating extracted data..."
                        )


                        missing_trade = [
                            column
                            for column
                            in REQUIRED_COLUMNS
                            if column
                            not in trade_df.columns
                        ]


                        missing_aggregate = [
                            column
                            for column
                            in AGGREGATE_COLUMNS
                            if column
                            not in aggregate_df.columns
                        ]


                        if missing_trade:

                            raise ValueError(
                                "Missing trade columns: "
                                + ", ".join(
                                    missing_trade
                                )
                            )


                        if missing_aggregate:

                            raise ValueError(
                                "Missing aggregate columns: "
                                + ", ".join(
                                    missing_aggregate
                                )
                            )


                        if trade_df.empty:

                            raise ValueError(
                                "No valid trade rows "
                                "were extracted."
                            )


                        if aggregate_df.empty:

                            raise ValueError(
                                "No valid aggregate rows "
                                "were extracted."
                            )


                        submission_progress.progress(
                            65
                        )


                        # =====================================
                        # STEP 5 - CHECK WORKFLOW
                        # =====================================

                        before_missing = (
                            False
                        )


                        if (
                            submission_stage
                            == "after"
                        ):

                            submission_status.write(
                                "Checking corresponding "
                                "Before submission..."
                            )


                            before_payload = (
                                get_submission_payload(
                                    selected_iteration,
                                    participant,
                                    pathway,
                                    stage="before",
                                )
                            )


                            before_missing = (
                                before_payload
                                is None
                            )


                        submission_progress.progress(
                            75
                        )


                        # =====================================
                        # STEP 6 - SAVE
                        # =====================================

                        submission_status.write(
                            "Saving extracted submission "
                            "data to private runtime storage..."
                        )


                        save_result = (
                            save_submission(
                                iteration=(
                                    selected_iteration
                                ),

                                username=(
                                    username.strip()
                                ),

                                participant=(
                                    participant
                                ),

                                pathway=(
                                    pathway
                                ),

                                df=(
                                    trade_df
                                ),

                                aggregate_df=(
                                    aggregate_df
                                ),

                                stage=(
                                    submission_stage
                                ),

                                source_workbook=(
                                    uploaded_file.name
                                ),

                                replace_existing=(
                                    replace_existing
                                ),
                            )
                        )


                        submission_progress.progress(
                            95
                        )


                        stored_path = (
                            save_result[
                                "path"
                            ]
                        )


                        # =====================================
                        # COMPLETE
                        # =====================================

                        submission_progress.progress(
                            100
                        )


                        submission_status.update(
                            label=(
                                "Calculator processed "
                                "successfully."
                            ),
                            state="complete",
                            expanded=False,
                        )


                    # =========================================
                    # RESULT MESSAGE
                    # =========================================

                    if (
                        save_result[
                            "status"
                        ]
                        == "DUPLICATE"
                    ):

                        st.info(
                            "This exact calculator data "
                            "is already submitted. "
                            "Nothing was changed."
                        )


                    elif (
                        save_result[
                            "status"
                        ]
                        == "REPLACED"
                    ):

                        st.success(
                            "Previous submission replaced "
                            "successfully."
                        )


                        if (
                            submission_stage
                            == "before"
                        ):

                            st.warning(
                                "Published adjusted-trade "
                                "results are now stale. "
                                "The admin must rerun the "
                                "trade adjustment."
                            )


                    else:

                        st.success(
                            "Calculator submitted "
                            "successfully."
                        )


                    # =========================================
                    # AFTER WITHOUT BEFORE WARNING
                    # =========================================

                    if (
                        submission_stage
                        == "after"
                        and before_missing
                    ):

                        st.warning(
                            "No corresponding Before "
                            "submission was found for this "
                            "participant and pathway. "
                            "The After submission was saved, "
                            "but please confirm that this "
                            "is intentional."
                        )


                    # =========================================
                    # PARTICIPANT / WORKBOOK LOCATION CHECK
                    # =========================================

                    if (
                        str(
                            workbook_location
                        )
                        .strip()
                        .casefold()
                        !=
                        str(
                            participant
                        )
                        .strip()
                        .casefold()
                    ):

                        st.warning(
                            "Workbook Location is "
                            f"'{workbook_location}', while "
                            "the selected participant is "
                            f"'{participant}'. "
                            "Please verify the submission."
                        )


                    # =========================================
                    # SUBMISSION SUMMARY
                    # =========================================

                    c1, c2, c3, c4 = (
                        st.columns(
                            4
                        )
                    )


                    c1.metric(
                        "Trade rows",
                        len(
                            trade_df
                        ),
                    )


                    c2.metric(
                        "Aggregate rows",
                        len(
                            aggregate_df
                        ),
                    )


                    c3.metric(
                        "Pathway",
                        pathway,
                    )


                    c4.metric(
                        "Stage",
                        (
                            "Before"
                            if submission_stage
                            == "before"
                            else
                            "After"
                        ),
                    )


                    st.caption(
                        "Workbook location: "
                        f"{workbook_location}"
                    )


                    st.caption(
                        "Stored in private runtime storage:"
                    )


                    st.code(
                        stored_path
                    )


                    # =========================================
                    # OPTIONAL DATA PREVIEW
                    # =========================================

                    with st.expander(
                        "Preview extracted data",
                        expanded=False,
                    ):

                        st.markdown(
                            "#### Trade data"
                        )


                        st.dataframe(
                            trade_df.head(
                                20
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )


                        st.markdown(
                            "#### Aggregate indicators"
                        )


                        st.dataframe(
                            aggregate_df,
                            use_container_width=True,
                            hide_index=True,
                        )


                # =============================================
                # EXISTING SUBMISSION
                # =============================================

                except FileExistsError:

                    if (
                        submission_status
                        is not None
                    ):

                        try:

                            submission_status.update(
                                label=(
                                    "Submission not saved."
                                ),
                                state="error",
                                expanded=True,
                            )

                        except Exception:

                            pass


                    st.warning(
                        "A different calculator already "
                        "exists for this exact iteration, "
                        "participant, pathway and stage. "
                        "Tick 'Replace the existing "
                        "submission' only if you intend "
                        "to replace it."
                    )


                # =============================================
                # GENERAL FAILURE
                # =============================================

                except Exception as exc:

                    if (
                        submission_status
                        is not None
                    ):

                        try:

                            submission_status.update(
                                label=(
                                    "Calculator processing "
                                    "failed."
                                ),
                                state="error",
                                expanded=True,
                            )

                        except Exception:

                            pass


                    st.error(
                        "Could not process the "
                        f"calculator: {exc}"
                    )



# ============================================================
# 14. USER - DOWNLOAD RESULT
# ============================================================

with result_tab:

    st.subheader(
        "Download adjusted trade"
    )

    try:
        configs = list_iteration_configs()
    except Exception:
        configs = []

    iteration_names = [
        cfg["iteration"]
        for cfg in configs
    ]

    if not iteration_names:
        st.info(
            "No iterations are available."
        )
    else:
        selected_result_iteration = (
            st.selectbox(
                "Iteration",
                iteration_names,
                key="result_iteration",
            )
        )

        stale_info = load_stale_marker(
            selected_result_iteration
        )

        if stale_info:
            st.warning(
                "This iteration changed after the last "
                "trade adjustment run. The published "
                "result is stale and download is disabled "
                "until the admin reruns the adjustment."
            )
        else:
            result_config = (
                load_iteration_config(
                    selected_result_iteration
                )
                or {}
            )

            participant_options = (
                result_config.get(
                    "participants",
                    [],
                )
            )

            if not participant_options:
                st.info(
                    "No participants are configured for "
                    "this iteration."
                )
            else:
                result_participant = (
                    st.selectbox(
                        "Country / participant",
                        participant_options,
                        key="result_participant",
                    )
                )

                path = result_path(
                    selected_result_iteration,
                    result_participant,
                )

                result_file = github_get_file(
                    path
                )

                if result_file:
                    st.success(
                        "Adjusted trade result is ready."
                    )
                    st.caption(
                        "The downloaded CSV is formatted for "
                        "the FABLE-C FullDataTradeAdj table: "
                        "Pathway, Product, Year, ImportsAdj, "
                        "ExportsAdj."
                    )
                    st.download_button(
                        "Download adjusted trade",
                        data=result_file["content"],
                        file_name=(
                            f"{safe_name(result_participant)}"
                            "_adjusted_trade.csv"
                        ),
                        mime="text/csv",
                    )
                else:
                    st.warning(
                        "No published adjusted trade "
                        "result is available yet."
                    )


# ============================================================
# 15. PUBLIC ANALYTICS
# ============================================================

with analytics_tab:

    # ========================================================
    # GLOBAL TARGETS DASHBOARD - BEGIN
    # ========================================================

    def plain_text_label(text):

        text = str(text)

        text = text.replace("_", " ")

        text = text.replace("—", "-")
        text = text.replace("–", "-")

        while "--" in text:
            text = text.replace("--", "-")

        while "  " in text:
            text = text.replace("  ", " ")

        return text.strip()


    def pretty_columns(columns):

        special_map = {
            "Value_Gt_CO2e": "Gt CO2e",
            "CalcAllLandCO2e": "Land use change GHG (Mt CO2e)",
            "CalcAllAgriCO2e": "Agriculture GHG (Mt CO2e)",
            "Margin_2030_pct": "Margin 2030 (%)",
            "Margin_2050_pct": "Margin 2050 (%)",
            "Pass_2030": "Pass 2030",
            "Pass_2050": "Pass 2050",
            "Pass_Both_Years": "Pass both years",
            "Target threshold": "Target threshold",
            "Target": "Target",
            "Status": "Status",
            "Actual": "Actual",
            "Location": "Region",
            "Participant": "Participant",
            "Pathway": "Pathway",
            "Year": "Year",
            "kcal_feas": "Kcal feas",
            "kcal_mder": "Kcal mder",
            "Complete": "Complete",
        }

        output = []

        for column in columns:

            column = str(column)

            if column in special_map:
                output.append(special_map[column])
            else:
                output.append(
                    plain_text_label(column)
                )

        return output


    def pretty_dataframe(df):

        pretty_df = df.copy()

        pretty_df.columns = pretty_columns(
            pretty_df.columns
        )

        return pretty_df


    st.subheader(
        "Global Targets"
    )

    st.caption(
        "Assess food security and global GHG targets "
        "using aggregate outputs automatically extracted "
        "from submitted FABLE-C calculators."
    )


    # --------------------------------------------------------
    # ITERATIONS
    # --------------------------------------------------------

    try:

        gt_configs = (
            list_iteration_configs()
        )

    except Exception as exc:

        gt_configs = []

        st.error(
            "Could not load iteration configuration: "
            f"{exc}"
        )


    # --------------------------------------------------------
    # Add development-only fixture iteration when explicitly
    # enabled through FABLE_ENABLE_LOCAL_FIXTURES=1.
    # --------------------------------------------------------

    if local_global_fixture_enabled():

        local_fixture_cfg = (
            local_global_fixture_config()
        )


        if local_fixture_cfg[
            "participants"
        ]:

            gt_configs = (
                [
                    local_fixture_cfg
                ]
                + gt_configs
            )


    gt_iterations = [
        config.get(
            "iteration",
            "",
        )
        for config in gt_configs
        if config.get(
            "iteration",
            "",
        )
    ]


    if not gt_iterations:

        st.info(
            "No configured iteration is available "
            "for global target assessment."
        )


    else:

        # ====================================================
        # FILTERS
        # ====================================================

        gt_filter_1, gt_filter_2, gt_filter_3 = (
            st.columns(
                3
            )
        )


        with gt_filter_1:

            gt_iteration = (
                st.selectbox(
                    "Iteration",
                    gt_iterations,
                    key="gt_iteration",
                    format_func=plain_text_label,
                )
            )


        if (
            gt_iteration
            == LOCAL_GLOBAL_FIXTURE_ITERATION
            and local_global_fixture_enabled()
        ):

            gt_config = (
                local_global_fixture_config()
            )

        else:

            gt_config = (
                load_iteration_config(
                    gt_iteration
                )
                or {}
            )


        gt_pathways = (
            gt_config.get(
                "pathways",
                [],
            )
        )


        with gt_filter_2:

            gt_pathway = (
                st.selectbox(
                    "Pathway",
                    gt_pathways,
                    key="gt_pathway",
                    format_func=plain_text_label,
                )
            )


        with gt_filter_3:

            gt_stage_label = (
                st.selectbox(
                    "Submission stage",
                    [
                        "After trade adjustment",
                        "Before trade adjustment",
                    ],
                    index=0,
                    key="gt_stage",
                    help=(
                        "After trade adjustment: "
                        "calculator HAS applied adjusted "
                        "trade for this pathway."
                        "\n\n"
                        "Before trade adjustment: "
                        "calculator has NOT yet applied "
                        "adjusted trade for this pathway."
                    ),
                )
            )


        gt_stage = (
            "after"
            if (
                gt_stage_label
                == "After trade adjustment"
            )
            else "before"
        )


        if (
            gt_iteration
            == LOCAL_GLOBAL_FIXTURE_ITERATION
        ):

            st.info(
                "Local development fixture mode - "
                "using the validated 28-region "
                "CurrentTrends dataset. "
                "GitHub and app-data storage are not being used."
            )


        # ====================================================
        # RUN TOKEN
        # ====================================================

        gt_token = (
            f"{gt_iteration}|"
            f"{gt_pathway}|"
            f"{gt_stage}"
        )


        gt_run_clicked = (
            st.button(
                "Run Global Assessment",
                type="primary",
                key="gt_run_button",
            )
        )


        if gt_run_clicked:

            st.session_state[
                "gt_run_token"
            ] = gt_token


        gt_ready = (
            st.session_state.get(
                "gt_run_token"
            )
            == gt_token
        )


        if not gt_ready:

            st.info(
                "Choose the iteration, pathway and stage, "
                "then click Run Global Assessment."
            )


        else:

            try:

                # =============================================
                # SMART PROCESSING STATUS
                # =============================================

                with st.status(
                    "Running global target assessment...",
                    expanded=True,
                ) as gt_status:

                    gt_progress = (
                        st.progress(
                            0
                        )
                    )


                    # -----------------------------------------
                    # STEP 1
                    # -----------------------------------------

                    gt_status.write(
                        "Reading expected participant coverage..."
                    )


                    gt_expected_participants = (
                        gt_config.get(
                            "participants",
                            [],
                        )
                    )


                    gt_progress.progress(
                        15
                    )


                    # -----------------------------------------
                    # STEP 2
                    # -----------------------------------------

                    gt_status.write(
                        "Loading submitted FABLE-C calculators..."
                    )


                    if (
                        gt_iteration
                        == LOCAL_GLOBAL_FIXTURE_ITERATION
                        and local_global_fixture_enabled()
                    ):

                        if gt_stage == "after":

                            gt_submissions_all = (
                                load_local_global_fixture_payloads()
                            )

                        else:

                            gt_submissions_all = []


                    else:

                        gt_submissions_all = (
                            list_submission_payloads(
                                gt_iteration,
                                stage=gt_stage,
                            )
                        )


                    gt_submissions = [
                        payload
                        for payload
                        in gt_submissions_all
                        if (
                            str(
                                payload.get(
                                    "pathway",
                                    "",
                                )
                            )
                            .strip()
                            .casefold()
                            ==
                            str(
                                gt_pathway
                            )
                            .strip()
                            .casefold()
                        )
                    ]


                    gt_submitted_participants = [
                        payload.get(
                            "participant",
                            "",
                        )
                        for payload
                        in gt_submissions
                    ]


                    gt_progress.progress(
                        35
                    )


                    # -----------------------------------------
                    # STEP 3
                    # -----------------------------------------

                    gt_status.write(
                        "Building global aggregate dataset..."
                    )


                    gt_aggregate_df = (
                        aggregate_rows_from_submissions(
                            gt_submissions,
                            pathway=gt_pathway,
                        )
                    )


                    gt_progress.progress(
                        55
                    )


                    # -----------------------------------------
                    # STEP 4
                    # -----------------------------------------

                    gt_status.write(
                        "Calculating food security and GHG targets..."
                    )


                    gt_result = (
                        calculate_global_targets_with_coverage(
                            gt_aggregate_df,
                            expected_regions=(
                                gt_expected_participants
                            ),
                            submitted_regions=(
                                gt_submitted_participants
                            ),
                        )
                    )


                    gt_food = (
                        gt_result[
                            "food_security"
                        ]
                    )

                    gt_agri = (
                        gt_result[
                            "agriculture_ghg"
                        ]
                    )

                    gt_land = (
                        gt_result[
                            "land_use_ghg"
                        ]
                    )

                    gt_coverage = (
                        gt_result[
                            "coverage"
                        ]
                    )


                    gt_progress.progress(
                        80
                    )


                    # -----------------------------------------
                    # STEP 5
                    # -----------------------------------------

                    gt_status.write(
                        "Preparing charts and summary..."
                    )


                    gt_progress.progress(
                        100
                    )


                    gt_status.update(
                        label=(
                            "Global assessment ready"
                        ),
                        state="complete",
                        expanded=False,
                    )


                # =================================================
                # COVERAGE
                # =================================================

                coverage_col_1, coverage_col_2 = (
                    st.columns(
                        2
                    )
                )


                coverage_col_1.metric(
                    "Submitted participants",
                    (
                        f"{gt_coverage['submitted_expected_regions']}/"
                        f"{gt_coverage['expected_regions']}"
                    ),
                )


                gt_region_count = (
                    int(
                        gt_aggregate_df[
                            "Location"
                        ].nunique()
                    )
                    if not gt_aggregate_df.empty
                    else 0
                )


                coverage_col_2.metric(
                    "Regions in aggregate data",
                    gt_region_count,
                )


                if not gt_coverage[
                    "coverage_complete"
                ]:

                    st.warning(
                        "Assessment is INCOMPLETE because "
                        "not all configured participants "
                        "have submitted the selected "
                        "pathway and stage."
                    )


                    if gt_coverage[
                        "missing_regions"
                    ]:

                        with st.expander(
                            "Missing participants"
                        ):

                            st.write(
                                ", ".join(
                                    gt_coverage[
                                        "missing_regions"
                                    ]
                                )
                            )


                # =================================================
                # SUMMARY CARDS
                # =================================================

                st.markdown(
                    "### Target summary"
                )


                card_1, card_2, card_3 = (
                    st.columns(
                        3
                    )
                )


                def show_gt_card(
                    container,
                    title,
                    status,
                    actual,
                    target,
                ):

                    with container:

                        st.markdown(
                            f"#### {plain_text_label(title)}"
                        )


                        if status == "YES":

                            st.success(
                                f"YES - {actual}"
                            )

                        elif status == "NO":

                            st.error(
                                f"NO - {actual}"
                            )

                        else:

                            st.warning(
                                f"INCOMPLETE - {actual}"
                            )


                        st.caption(
                            f"Target: {plain_text_label(target)}"
                        )


                show_gt_card(
                    card_1,
                    "Food security",
                    gt_food[
                        "status"
                    ],
                    (
                        f"{gt_food['regions_passing']}/"
                        f"{gt_food['regions_total']} "
                        "regions pass"
                    ),
                    (
                        "All regions 10-50% above "
                        "MDER in 2030 and 2050"
                    ),
                )


                show_gt_card(
                    card_2,
                    "Agriculture GHG",
                    gt_agri[
                        "status"
                    ],
                    (
                        (
                            f"{gt_agri['global_total_gt']:.3f} "
                            "Gt CO2e"
                        )
                        if pd.notna(
                            gt_agri[
                                "global_total_gt"
                            ]
                        )
                        else "Missing data"
                    ),
                    "<= 4 Gt CO2e in 2050",
                )


                show_gt_card(
                    card_3,
                    "Land use change GHG",
                    gt_land[
                        "status"
                    ],
                    (
                        (
                            f"{gt_land['global_total_gt']:.3f} "
                            "Gt CO2e"
                        )
                        if pd.notna(
                            gt_land[
                                "global_total_gt"
                            ]
                        )
                        else "Missing data"
                    ),
                    "<= -1 Gt CO2e in 2050",
                )


                # =================================================
                # SUMMARY TABLE
                # =================================================

                st.dataframe(
                    pretty_dataframe(
                        gt_result[
                            "summary"
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )


                # =================================================
                # SHARED FILTER FOR VISUAL DASHBOARD
                # =================================================

                st.markdown(
                    "### Visual dashboard"
                )

                gt_year = (
                    st.radio(
                        "Food security year",
                        [
                            2030,
                            2050,
                        ],
                        horizontal=True,
                        key="gt_year",
                    )
                )


                # =================================================
                # FOOD SECURITY - VERTICAL LOCATION BARS
                # =================================================

                gt_food_detail = (
                    gt_food[
                        "detail"
                    ].copy()
                )


                if gt_food_detail.empty:

                    food_chart_ready = False

                    gt_food_fig = (
                        go.Figure()
                    )


                    gt_food_fig.update_layout(
                        title=(
                            "Food security"
                        ),
                        height=560,
                        template="plotly_white",
                        font=dict(
                            color="#1F2937",
                            size=12,
                        ),
                    )


                else:

                    gt_margin_column = (
                        f"Margin_{gt_year}_pct"
                    )


                    gt_pass_column = (
                        f"Pass_{gt_year}"
                    )


                    gt_food_plot = (
                        gt_food_detail[
                            [
                                "Location",
                                gt_margin_column,
                                gt_pass_column,
                            ]
                        ]
                        .copy()
                    )


                    gt_food_plot.columns = [
                        "Location",
                        "Margin",
                        "Pass",
                    ]


                    gt_food_plot[
                        "Margin"
                    ] = pd.to_numeric(
                        gt_food_plot[
                            "Margin"
                        ],
                        errors="coerce",
                    )


                    gt_food_plot = (
                        gt_food_plot.sort_values(
                            [
                                "Margin",
                                "Location",
                            ],
                            ascending=[
                                False,
                                True,
                            ],
                            na_position="last",
                        )
                        .reset_index(
                            drop=True
                        )
                    )


                    def gt_food_bar_color(
                        value,
                    ):

                        if pd.isna(
                            value
                        ):

                            return "#9CA3AF"


                        return (
                            "#2E8B57"
                            if bool(
                                value
                            )
                            else "#E6533C"
                        )


                    gt_food_colors = [
                        gt_food_bar_color(
                            value
                        )
                        for value
                        in gt_food_plot[
                            "Pass"
                        ]
                    ]


                    gt_food_status = [
                        (
                            "Incomplete"
                            if pd.isna(
                                value
                            )
                            else (
                                "Within target"
                                if bool(
                                    value
                                )
                                else "Outside target"
                            )
                        )
                        for value
                        in gt_food_plot[
                            "Pass"
                        ]
                    ]


                    gt_food_fig = (
                        go.Figure()
                    )


                    gt_food_fig.add_trace(
                        go.Bar(
                            x=(
                                gt_food_plot[
                                    "Location"
                                ]
                            ),
                            y=(
                                gt_food_plot[
                                    "Margin"
                                ]
                            ),
                            marker=dict(
                                color=(
                                    gt_food_colors
                                ),
                                line=dict(
                                    width=0
                                ),
                            ),
                            customdata=(
                                gt_food_status
                            ),
                            hovertemplate=(
                                "<b>%{x}</b>"
                                "<br>Margin: %{y:.1f}%"
                                "<br>Status: %{customdata}"
                                "<extra></extra>"
                            ),
                            showlegend=False,
                        )
                    )


                    # -----------------------------------------
                    # Target range
                    # -----------------------------------------

                    gt_food_fig.add_hrect(
                        y0=10,
                        y1=50,
                        opacity=0.06,
                        line_width=0,
                    )


                    gt_food_fig.add_hline(
                        y=10,
                        line_dash="dash",
                        line_width=2,
                        annotation_text="10%",
                        annotation_position=(
                            "bottom right"
                        ),
                    )


                    gt_food_fig.add_hline(
                        y=50,
                        line_dash="dash",
                        line_width=2,
                        annotation_text="50%",
                        annotation_position=(
                            "top right"
                        ),
                    )


                    gt_food_fig.update_layout(
                        title=(
                            "Food security margin - "
                            f"{gt_year}"
                        ),
                        height=560,
                        template="plotly_white",
                        font=dict(
                            color="#1F2937",
                            size=12,
                        ),
                        title_font=dict(
                            color="#111827",
                            size=16,
                        ),
                        margin=dict(
                            l=10,
                            r=10,
                            t=60,
                            b=150,
                        ),
                        bargap=0.18,
                        xaxis=dict(
                            title="Location",
                            tickangle=-55,
                            automargin=True,
                            tickfont=dict(
                                size=9,
                                color="#374151",
                            ),
                            title_font=dict(
                                color="#374151",
                            ),
                        ),
                        yaxis=dict(
                            title=(
                                "Food security margin (%)"
                            ),
                            automargin=True,
                            zeroline=True,
                            zerolinewidth=1,
                            tickfont=dict(
                                color="#374151",
                            ),
                            title_font=dict(
                                color="#374151",
                            ),
                        ),
                    )


                    gt_food_fig.update_annotations(
                        font=dict(
                            color="#374151",
                            size=11,
                        )
                    )


                    food_chart_ready = True


                # =================================================
                # SHARED LOCATION COLORS FOR GHG STACKS
                # =================================================

                gt_all_ghg_locations = sorted(
                    set(
                        gt_land[
                            "contributions"
                        ][
                            "Location"
                        ]
                        .dropna()
                        .astype(str)
                        .tolist()
                    )
                    |
                    set(
                        gt_agri[
                            "contributions"
                        ][
                            "Location"
                        ]
                        .dropna()
                        .astype(str)
                        .tolist()
                    ),
                    key=str.casefold,
                )


                gt_location_palette = (
                    px.colors.qualitative.Safe
                    + px.colors.qualitative.Set3
                    + px.colors.qualitative.Plotly
                    + px.colors.qualitative.Light24
                    + px.colors.qualitative.Dark24
                )


                gt_location_colors = {
                    location: (
                        gt_location_palette[
                            index
                            % len(
                                gt_location_palette
                            )
                        ]
                    )
                    for index, location
                    in enumerate(
                        gt_all_ghg_locations
                    )
                }


                # =================================================
                # GHG STACKED VERTICAL CHART HELPER
                # =================================================

                def build_gt_vertical_ghg_chart(
                    contributions,
                    *,
                    title,
                    global_total,
                    target,
                    target_label,
                ):

                    plot_df = (
                        contributions[
                            [
                                "Location",
                                "Value_Gt_CO2e",
                            ]
                        ]
                        .copy()
                    )


                    plot_df[
                        "Location"
                    ] = (
                        plot_df[
                            "Location"
                        ]
                        .astype(str)
                    )


                    plot_df[
                        "Value_Gt_CO2e"
                    ] = pd.to_numeric(
                        plot_df[
                            "Value_Gt_CO2e"
                        ],
                        errors="coerce",
                    )


                    plot_df = (
                        plot_df.dropna(
                            subset=[
                                "Value_Gt_CO2e"
                            ]
                        )
                    )


                    plot_df = (
                        plot_df.sort_values(
                            "Location",
                            key=lambda values: (
                                values.astype(
                                    str
                                ).str.casefold()
                            ),
                        )
                        .reset_index(
                            drop=True
                        )
                    )


                    fig = (
                        go.Figure()
                    )


                    if plot_df.empty:

                        fig.update_layout(
                            title=(
                                plain_text_label(
                                    title
                                )
                            ),
                            height=560,
                            template="plotly_white",
                            font=dict(
                                color="#1F2937",
                                size=12,
                            ),
                        )

                        return fig


                    show_location_legend = (
                        len(
                            plot_df
                        )
                        <= 10
                    )


                    # -----------------------------------------
                    # One trace per Location
                    #
                    # Positive values stack above zero.
                    # Negative values stack below zero.
                    # -----------------------------------------

                    for _, row in (
                        plot_df.iterrows()
                    ):

                        location = str(
                            row[
                                "Location"
                            ]
                        )


                        value = float(
                            row[
                                "Value_Gt_CO2e"
                            ]
                        )


                        fig.add_trace(
                            go.Bar(
                                x=[
                                    "Global 2050"
                                ],
                                y=[
                                    value
                                ],
                                name=(
                                    plain_text_label(
                                        location
                                    )
                                ),
                                marker=dict(
                                    color=(
                                        gt_location_colors.get(
                                            location
                                        )
                                    ),
                                    line=dict(
                                        width=0.5,
                                        color=(
                                            "rgba(255,255,255,0.7)"
                                        ),
                                    ),
                                ),
                                hovertemplate=(
                                    "<b>"
                                    + plain_text_label(
                                        location
                                    )
                                    + "</b>"
                                    "<br>Contribution: "
                                    "%{y:.3f} Gt CO2e"
                                    "<extra></extra>"
                                ),
                                showlegend=(
                                    show_location_legend
                                ),
                            )
                        )


                    # -----------------------------------------
                    # Global total marker
                    # -----------------------------------------

                    if pd.notna(
                        global_total
                    ):

                        fig.add_trace(
                            go.Scatter(
                                x=[
                                    "Global 2050"
                                ],
                                y=[
                                    global_total
                                ],
                                mode=(
                                    "markers+text"
                                ),
                                name=(
                                    "Global total"
                                ),
                                marker=dict(
                                    symbol="diamond",
                                    size=12,
                                    color="#111111",
                                    line=dict(
                                        width=1,
                                        color="#FFFFFF",
                                    ),
                                ),
                                text=[
                                    (
                                        "Global total<br>"
                                        f"{global_total:.3f} "
                                        "Gt CO2e"
                                    )
                                ],
                                textposition=(
                                    "top center"
                                ),
                                textfont=dict(
                                    color="#111827",
                                    size=11,
                                ),
                                hovertemplate=(
                                    "<b>Global total</b>"
                                    "<br>%{y:.3f} Gt CO2e"
                                    "<extra></extra>"
                                ),
                                showlegend=False,
                            )
                        )


                    # -----------------------------------------
                    # Target
                    # -----------------------------------------

                    fig.add_hline(
                        y=target,
                        line_dash="dash",
                        line_width=2,
                        annotation_text=(
                            target_label
                        ),
                        annotation_position=(
                            "bottom right"
                            if target < 0
                            else "top right"
                        ),
                    )


                    # -----------------------------------------
                    # Sensible signed y range
                    # -----------------------------------------

                    positive_sum = float(
                        plot_df.loc[
                            (
                                plot_df[
                                    "Value_Gt_CO2e"
                                ]
                                > 0
                            ),
                            "Value_Gt_CO2e",
                        ].sum()
                    )


                    negative_sum = float(
                        plot_df.loc[
                            (
                                plot_df[
                                    "Value_Gt_CO2e"
                                ]
                                < 0
                            ),
                            "Value_Gt_CO2e",
                        ].sum()
                    )


                    range_values = [
                        0.0,
                        positive_sum,
                        negative_sum,
                        float(
                            target
                        ),
                    ]


                    if pd.notna(
                        global_total
                    ):

                        range_values.append(
                            float(
                                global_total
                            )
                        )


                    lower_value = min(
                        range_values
                    )

                    upper_value = max(
                        range_values
                    )


                    span = (
                        upper_value
                        - lower_value
                    )


                    padding = max(
                        span * 0.18,
                        0.35,
                    )


                    y_range = [
                        lower_value
                        - padding,
                        upper_value
                        + padding,
                    ]


                    # -----------------------------------------
                    # Layout
                    # -----------------------------------------

                    fig.update_layout(
                        title=(
                            plain_text_label(
                                title
                            )
                        ),
                        height=620,
                        template="plotly_white",
                        barmode="relative",
                        font=dict(
                            color="#1F2937",
                            size=12,
                        ),
                        title_font=dict(
                            color="#111827",
                            size=16,
                        ),
                        margin=dict(
                            l=10,
                            r=10,
                            t=70,
                            b=145,
                        ),
                        bargap=0.42,
                        legend_title_text="",
                        showlegend=(
                            show_location_legend
                        ),
                        legend=dict(
                            orientation="h",
                            yanchor="top",
                            y=-0.18,
                            xanchor="center",
                            x=0.5,
                            font=dict(
                                size=9,
                                color="#374151",
                            ),
                        ),
                        xaxis=dict(
                            title="",
                            tickfont=dict(
                                color="#374151",
                                size=11,
                            ),
                        ),
                        yaxis=dict(
                            title=(
                                "Gt CO2e"
                            ),
                            range=(
                                y_range
                            ),
                            zeroline=True,
                            zerolinewidth=1.5,
                            zerolinecolor=(
                                "#6B7280"
                            ),
                            tickfont=dict(
                                color="#374151",
                            ),
                            title_font=dict(
                                color="#374151",
                            ),
                        ),
                    )


                    fig.update_annotations(
                        font=dict(
                            color="#374151",
                            size=11,
                        )
                    )


                    return fig


                # =================================================
                # BUILD LAND CHART
                # =================================================

                gt_land_fig = (
                    build_gt_vertical_ghg_chart(
                        gt_land[
                            "contributions"
                        ],
                        title=(
                            "Land use change GHG - 2050"
                        ),
                        global_total=(
                            gt_land[
                                "global_total_gt"
                            ]
                        ),
                        target=-1.0,
                        target_label=(
                            "Target: -1 Gt CO2e"
                        ),
                    )
                )


                # =================================================
                # BUILD AGRICULTURE CHART
                # =================================================

                gt_agri_fig = (
                    build_gt_vertical_ghg_chart(
                        gt_agri[
                            "contributions"
                        ],
                        title=(
                            "Agriculture GHG - 2050"
                        ),
                        global_total=(
                            gt_agri[
                                "global_total_gt"
                            ]
                        ),
                        target=4.0,
                        target_label=(
                            "Target: 4 Gt CO2e"
                        ),
                    )
                )


                # =================================================
                # OPTIONAL BEFORE / AFTER COMPARISON
                # =================================================

                def gt_compare_stage_payloads(
                    stage_name,
                ):

                    payloads = (
                        list_submission_payloads(
                            gt_iteration,
                            stage=stage_name,
                        )
                    )


                    return [
                        payload
                        for payload
                        in payloads
                        if (
                            str(
                                payload.get(
                                    "pathway",
                                    "",
                                )
                            )
                            .strip()
                            .casefold()
                            ==
                            str(
                                gt_pathway
                            )
                            .strip()
                            .casefold()
                        )
                    ]


                gt_compare_before_payloads = (
                    gt_compare_stage_payloads(
                        "before"
                    )
                )


                gt_compare_after_payloads = (
                    gt_compare_stage_payloads(
                        "after"
                    )
                )


                gt_compare_available = (
                    bool(
                        gt_compare_before_payloads
                    )
                    and bool(
                        gt_compare_after_payloads
                    )
                )


                gt_compare_enabled = False


                if gt_compare_available:

                    gt_compare_enabled = (
                        st.checkbox(
                            "Compare Before and After trade adjustment",
                            value=False,
                            key=(
                                "gt_compare_before_after"
                            ),
                            help=(
                                "Show paired Before and After bars "
                                "for this iteration and pathway."
                            ),
                        )
                    )


                if gt_compare_enabled:


                    def gt_compare_result(
                        payloads,
                    ):

                        aggregate_df = (
                            aggregate_rows_from_submissions(
                                payloads,
                                pathway=gt_pathway,
                            )
                        )


                        submitted_regions = [
                            payload.get(
                                "participant",
                                "",
                            )
                            for payload
                            in payloads
                        ]


                        return (
                            calculate_global_targets_with_coverage(
                                aggregate_df,
                                expected_regions=(
                                    gt_expected_participants
                                ),
                                submitted_regions=(
                                    submitted_regions
                                ),
                            )
                        )


                    gt_before_compare_result = (
                        gt_compare_result(
                            gt_compare_before_payloads
                        )
                    )


                    gt_after_compare_result = (
                        gt_compare_result(
                            gt_compare_after_payloads
                        )
                    )


                    gt_compare_results = {
                        "Before trade adjustment": (
                            gt_before_compare_result
                        ),
                        "After trade adjustment": (
                            gt_after_compare_result
                        ),
                    }


                    # -----------------------------------------
                    # FOOD SECURITY PAIRED VERTICAL BARS
                    # -----------------------------------------

                    gt_compare_food_rows = []

                    margin_column = (
                        f"Margin_{gt_year}_pct"
                    )


                    for stage_label, result in (
                        gt_compare_results.items()
                    ):

                        detail_df = (
                            result[
                                "food_security"
                            ][
                                "detail"
                            ]
                            .copy()
                        )


                        if (
                            detail_df.empty
                            or margin_column
                            not in detail_df.columns
                        ):

                            continue


                        for _, row in (
                            detail_df.iterrows()
                        ):

                            gt_compare_food_rows.append(
                                {
                                    "Location": (
                                        plain_text_label(
                                            row.get(
                                                "Location",
                                                "",
                                            )
                                        )
                                    ),
                                    "Margin": (
                                        pd.to_numeric(
                                            pd.Series(
                                                [
                                                    row.get(
                                                        margin_column
                                                    )
                                                ]
                                            ),
                                            errors="coerce",
                                        ).iloc[
                                            0
                                        ]
                                    ),
                                    "Stage": (
                                        stage_label
                                    ),
                                }
                            )


                    gt_compare_food_df = (
                        pd.DataFrame(
                            gt_compare_food_rows
                        )
                    )


                    if not gt_compare_food_df.empty:

                        gt_compare_food_df = (
                            gt_compare_food_df.dropna(
                                subset=[
                                    "Margin"
                                ]
                            )
                        )


                        gt_compare_location_order = (
                            gt_compare_food_df.groupby(
                                "Location"
                            )[
                                "Margin"
                            ]
                            .mean()
                            .sort_values(
                                ascending=False
                            )
                            .index
                            .tolist()
                        )


                        gt_food_fig = (
                            px.bar(
                                gt_compare_food_df,
                                x="Location",
                                y="Margin",
                                color="Stage",
                                barmode="group",
                                category_orders={
                                    "Location": (
                                        gt_compare_location_order
                                    ),
                                    "Stage": [
                                        "Before trade adjustment",
                                        "After trade adjustment",
                                    ],
                                },
                                title=(
                                    "Food security margin - "
                                    f"{gt_year}"
                                ),
                                labels={
                                    "Margin": (
                                        "Food security margin (%)"
                                    ),
                                    "Stage": (
                                        ""
                                    ),
                                },
                            )
                        )


                        gt_food_fig.add_hrect(
                            y0=10,
                            y1=50,
                            opacity=0.06,
                            line_width=0,
                        )


                        gt_food_fig.add_hline(
                            y=10,
                            line_dash="dash",
                            line_width=2,
                            annotation_text="10%",
                            annotation_position=(
                                "bottom right"
                            ),
                        )


                        gt_food_fig.add_hline(
                            y=50,
                            line_dash="dash",
                            line_width=2,
                            annotation_text="50%",
                            annotation_position=(
                                "top right"
                            ),
                        )


                        gt_food_fig.update_layout(
                            height=610,
                            template="plotly_white",
                            font=dict(
                                color="#1F2937",
                                size=12,
                            ),
                            title_font=dict(
                                color="#111827",
                                size=16,
                            ),
                            margin=dict(
                                l=10,
                                r=10,
                                t=65,
                                b=165,
                            ),
                            legend_title_text="",
                            legend=dict(
                                orientation="h",
                                yanchor="top",
                                y=-0.28,
                                xanchor="center",
                                x=0.5,
                            ),
                            xaxis=dict(
                                title="Location",
                                tickangle=-55,
                                automargin=True,
                            ),
                            yaxis=dict(
                                title=(
                                    "Food security margin (%)"
                                ),
                                zeroline=True,
                            ),
                        )


                        food_chart_ready = True


                    # -----------------------------------------
                    # GHG BEFORE / AFTER STACKED BARS
                    # -----------------------------------------

                    def build_gt_compare_ghg_chart(
                        indicator_key,
                        *,
                        title,
                        target,
                        target_label,
                    ):

                        stage_labels = [
                            "Before trade adjustment",
                            "After trade adjustment",
                        ]

                        stage_maps = {}

                        all_locations = set()

                        range_values = [
                            0.0,
                            float(
                                target
                            ),
                        ]

                        global_totals = []


                        for stage_label in (
                            stage_labels
                        ):

                            result = (
                                gt_compare_results[
                                    stage_label
                                ][
                                    indicator_key
                                ]
                            )


                            contributions = (
                                result[
                                    "contributions"
                                ]
                                .copy()
                            )


                            if contributions.empty:

                                stage_maps[
                                    stage_label
                                ] = {}


                            else:

                                contributions[
                                    "Location"
                                ] = (
                                    contributions[
                                        "Location"
                                    ]
                                    .astype(str)
                                )


                                contributions[
                                    "Value_Gt_CO2e"
                                ] = pd.to_numeric(
                                    contributions[
                                        "Value_Gt_CO2e"
                                    ],
                                    errors="coerce",
                                )


                                contributions = (
                                    contributions.dropna(
                                        subset=[
                                            "Value_Gt_CO2e"
                                        ]
                                    )
                                )


                                stage_map = {
                                    str(
                                        row[
                                            "Location"
                                        ]
                                    ): float(
                                        row[
                                            "Value_Gt_CO2e"
                                        ]
                                    )
                                    for _, row
                                    in contributions.iterrows()
                                }


                                stage_maps[
                                    stage_label
                                ] = stage_map


                                all_locations.update(
                                    stage_map.keys()
                                )


                                range_values.extend(
                                    [
                                        float(
                                            contributions.loc[
                                                contributions[
                                                    "Value_Gt_CO2e"
                                                ]
                                                > 0,
                                                "Value_Gt_CO2e",
                                            ].sum()
                                        ),
                                        float(
                                            contributions.loc[
                                                contributions[
                                                    "Value_Gt_CO2e"
                                                ]
                                                < 0,
                                                "Value_Gt_CO2e",
                                            ].sum()
                                        ),
                                    ]
                                )


                            global_total = (
                                result.get(
                                    "global_total_gt"
                                )
                            )


                            if pd.notna(
                                global_total
                            ):

                                global_totals.append(
                                    float(
                                        global_total
                                    )
                                )

                                range_values.append(
                                    float(
                                        global_total
                                    )
                                )


                            else:

                                global_totals.append(
                                    None
                                )


                        all_locations = sorted(
                            all_locations,
                            key=str.casefold,
                        )


                        palette = (
                            px.colors.qualitative.Safe
                            + px.colors.qualitative.Set3
                            + px.colors.qualitative.Plotly
                            + px.colors.qualitative.Light24
                        )


                        location_colors = {
                            location: (
                                palette[
                                    index
                                    % len(
                                        palette
                                    )
                                ]
                            )
                            for index, location
                            in enumerate(
                                all_locations
                            )
                        }


                        fig = (
                            go.Figure()
                        )


                        show_location_legend = (
                            len(
                                all_locations
                            )
                            <= 10
                        )


                        for location in (
                            all_locations
                        ):

                            fig.add_trace(
                                go.Bar(
                                    x=(
                                        stage_labels
                                    ),
                                    y=[
                                        stage_maps.get(
                                            stage_label,
                                            {},
                                        ).get(
                                            location,
                                            0.0,
                                        )
                                        for stage_label
                                        in stage_labels
                                    ],
                                    name=(
                                        plain_text_label(
                                            location
                                        )
                                    ),
                                    marker=dict(
                                        color=(
                                            location_colors[
                                                location
                                            ]
                                        )
                                    ),
                                    hovertemplate=(
                                        "<b>"
                                        + plain_text_label(
                                            location
                                        )
                                        + "</b>"
                                        "<br>%{x}"
                                        "<br>Contribution: "
                                        "%{y:.3f} Gt CO2e"
                                        "<extra></extra>"
                                    ),
                                    showlegend=(
                                        show_location_legend
                                    ),
                                )
                            )


                        valid_total_x = []
                        valid_total_y = []
                        valid_total_text = []


                        for stage_label, global_total in zip(
                            stage_labels,
                            global_totals,
                        ):

                            if global_total is None:

                                continue


                            valid_total_x.append(
                                stage_label
                            )

                            valid_total_y.append(
                                global_total
                            )

                            valid_total_text.append(
                                (
                                    "Net global total<br>"
                                    f"{global_total:.3f} "
                                    "Gt CO2e"
                                )
                            )


                        if valid_total_x:

                            fig.add_trace(
                                go.Scatter(
                                    x=valid_total_x,
                                    y=valid_total_y,
                                    mode=(
                                        "markers+text"
                                    ),
                                    marker=dict(
                                        symbol="diamond",
                                        size=12,
                                        color="#111111",
                                        line=dict(
                                            width=1,
                                            color="#FFFFFF",
                                        ),
                                    ),
                                    text=(
                                        valid_total_text
                                    ),
                                    textposition=(
                                        "top center"
                                    ),
                                    textfont=dict(
                                        color="#111827",
                                        size=10,
                                    ),
                                    showlegend=False,
                                    hovertemplate=(
                                        "<b>%{x}</b>"
                                        "<br>Net global total: "
                                        "%{y:.3f} Gt CO2e"
                                        "<extra></extra>"
                                    ),
                                )
                            )


                        fig.add_hline(
                            y=target,
                            line_dash="dash",
                            line_width=2,
                            annotation_text=(
                                target_label
                            ),
                            annotation_position=(
                                "bottom right"
                                if target < 0
                                else "top right"
                            ),
                        )


                        lower_value = min(
                            range_values
                        )

                        upper_value = max(
                            range_values
                        )

                        span = (
                            upper_value
                            - lower_value
                        )

                        padding = max(
                            span * 0.18,
                            0.35,
                        )


                        fig.update_layout(
                            title=(
                                plain_text_label(
                                    title
                                )
                            ),
                            height=620,
                            template="plotly_white",
                            barmode="relative",
                            font=dict(
                                color="#1F2937",
                                size=12,
                            ),
                            title_font=dict(
                                color="#111827",
                                size=16,
                            ),
                            margin=dict(
                                l=10,
                                r=10,
                                t=70,
                                b=145,
                            ),
                            bargap=0.35,
                            legend_title_text="",
                            showlegend=(
                                show_location_legend
                            ),
                            legend=dict(
                                orientation="h",
                                yanchor="top",
                                y=-0.18,
                                xanchor="center",
                                x=0.5,
                                font=dict(
                                    size=9,
                                    color="#374151",
                                ),
                            ),
                            xaxis=dict(
                                title="",
                                automargin=True,
                            ),
                            yaxis=dict(
                                title=(
                                    "Gt CO2e"
                                ),
                                range=[
                                    lower_value
                                    - padding,
                                    upper_value
                                    + padding,
                                ],
                                zeroline=True,
                                zerolinewidth=1.5,
                                zerolinecolor=(
                                    "#6B7280"
                                ),
                            ),
                        )


                        return fig


                    gt_land_fig = (
                        build_gt_compare_ghg_chart(
                            "land_use_ghg",
                            title=(
                                "Land use change GHG - 2050"
                            ),
                            target=-1.0,
                            target_label=(
                                "Target: -1 Gt CO2e"
                            ),
                        )
                    )


                    gt_agri_fig = (
                        build_gt_compare_ghg_chart(
                            "agriculture_ghg",
                            title=(
                                "Agriculture GHG - 2050"
                            ),
                            target=4.0,
                            target_label=(
                                "Target: 4 Gt CO2e"
                            ),
                        )
                    )


                # =================================================
                # THREE SIDE BY SIDE
                # =================================================

                (
                    chart_col_1,
                    chart_col_2,
                    chart_col_3,
                ) = st.columns(
                    [
                        1.55,
                        1,
                        1,
                    ]
                )


                with chart_col_1:

                    if food_chart_ready:

                        st.plotly_chart(
                            gt_food_fig,
                            use_container_width=True,
                            key="gt_food_chart",
                        )


                        st.caption(
                            "Each bar is one Location. "
                            "The overall target is met only when "
                            "every Location is 10% to 50% above "
                            "MDER in both 2030 and 2050."
                        )


                    else:

                        st.info(
                            "No usable food security data is available."
                        )


                with chart_col_2:

                    st.plotly_chart(
                        gt_land_fig,
                        use_container_width=True,
                        key="gt_land_chart",
                    )


                    if (
                        len(
                            gt_land[
                                "contributions"
                            ]
                        )
                        > 10
                    ):

                        st.caption(
                            "Each stacked segment represents one Location. "
                            "Hover over a segment to see its contribution. "
                            "Negative values represent removals or sequestration."
                        )


                    else:

                        st.caption(
                            "Each stacked segment represents one Location. "
                            "Negative values represent removals or sequestration."
                        )


                with chart_col_3:

                    st.plotly_chart(
                        gt_agri_fig,
                        use_container_width=True,
                        key="gt_agri_chart",
                    )


                    if (
                        len(
                            gt_agri[
                                "contributions"
                            ]
                        )
                        > 10
                    ):

                        st.caption(
                            "Each stacked segment represents one Location. "
                            "Hover over a segment to see its contribution. "
                            "The black diamond marks the global total."
                        )


                    else:

                        st.caption(
                            "Each stacked segment represents one Location. "
                            "The black diamond marks the global total."
                        )


                # =================================================
                # DETAIL TABLES
                # =================================================

                with st.expander(
                    "Food security detail"
                ):

                    st.dataframe(
                        pretty_dataframe(
                            gt_food[
                                "detail"
                            ]
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )


                with st.expander(
                    "Land use change contribution detail"
                ):

                    st.dataframe(
                        pretty_dataframe(
                            gt_land[
                                "contributions"
                            ]
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )


                with st.expander(
                    "Agriculture contribution detail"
                ):

                    st.dataframe(
                        pretty_dataframe(
                            gt_agri[
                                "contributions"
                            ]
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )


                # =================================================
                # QA
                # =================================================

                if not gt_result[
                    "duplicate_rows"
                ].empty:

                    st.error(
                        "Duplicate Region-Year aggregate records were detected."
                    )


                    with st.expander(
                        "View duplicate aggregate rows"
                    ):

                        st.dataframe(
                            pretty_dataframe(
                                gt_result[
                                    "duplicate_rows"
                                ]
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )


            except Exception as exc:

                st.error(
                    "Global target assessment failed: "
                    f"{exc}"
                )


    # ========================================================
    # GLOBAL TARGETS DASHBOARD - END
    # ========================================================

    # ========================================================
    # FLEXIBLE GLOBAL ANALYSIS STUDIO - BEGIN
    # ========================================================

    st.divider()

    st.subheader(
        "Global Analysis Studio"
    )

    st.caption(
        "Explore how global conditions change across "
        "iterations, pathways and trade adjustment stages. "
        "Use the predefined progress view or build a custom comparison."
    )


    # ========================================================
    # HELPERS
    # ========================================================

    def flex_iteration_sort_key(
        value,
    ):

        text = str(
            value
        ).strip()


        numeric_parts = (
            "".join(
                character
                if character.isdigit()
                else " "
                for character in text
            )
            .split()
        )


        number = (
            int(
                numeric_parts[
                    -1
                ]
            )
            if numeric_parts
            else 10**9
        )


        return (
            number,
            text.casefold(),
        )


    def flex_scope_label(
        row,
    ):

        return plain_text_label(
            (
                f"{row['Iteration']} | "
                f"{row['Pathway']} | "
                f"{row['Stage']}"
            )
        )


    def flex_prepare_figure(
        fig,
        *,
        height=520,
        tickangle=None,
    ):

        fig.update_layout(
            height=height,
            template="plotly_white",
            font=dict(
                color="#1F2937",
                size=12,
            ),
            title_font=dict(
                color="#111827",
                size=16,
            ),
            margin=dict(
                l=10,
                r=10,
                t=60,
                b=70,
            ),
            legend_title_text="",
        )

        fig.update_xaxes(
            tickfont=dict(
                color="#374151",
                size=11,
            ),
            title_font=dict(
                color="#374151",
                size=12,
            ),
        )

        fig.update_yaxes(
            tickfont=dict(
                color="#374151",
                size=11,
            ),
            title_font=dict(
                color="#374151",
                size=12,
            ),
        )

        fig.update_annotations(
            font=dict(
                color="#374151",
                size=11,
            )
        )


        if tickangle is not None:

            fig.update_xaxes(
                tickangle=tickangle,
                automargin=True,
            )


        return fig


    # ========================================================
    # LOAD / REFRESH ANALYTICS DATA
    # ========================================================

    flex_load_clicked = st.button(
        "Load or refresh analysis data",
        type="primary",
        key="flex_load_analysis",
    )


    if flex_load_clicked:

        try:

            with st.status(
                "Loading global analysis data...",
                expanded=True,
            ) as flex_status:

                flex_progress = st.progress(
                    0
                )


                # ------------------------------------------------
                # STEP 1 - ITERATION CONFIGURATION
                # ------------------------------------------------

                flex_status.write(
                    "Reading iteration configuration..."
                )


                flex_configs = (
                    list_iteration_configs()
                )


                if local_global_fixture_enabled():

                    flex_fixture_config = (
                        local_global_fixture_config()
                    )


                    fixture_iteration = (
                        flex_fixture_config.get(
                            "iteration",
                            "",
                        )
                    )


                    existing_iterations = {
                        str(
                            config.get(
                                "iteration",
                                "",
                            )
                        ).strip()
                        for config in flex_configs
                    }


                    if (
                        fixture_iteration
                        and fixture_iteration
                        not in existing_iterations
                    ):

                        flex_configs = (
                            [
                                flex_fixture_config
                            ]
                            + flex_configs
                        )


                flex_progress.progress(
                    15
                )


                # ------------------------------------------------
                # STEP 2 - EXPECTED PARTICIPANTS
                # ------------------------------------------------

                flex_status.write(
                    "Preparing expected participant coverage..."
                )


                flex_expected_by_iteration = {}


                for config in flex_configs:

                    iteration_name = str(
                        config.get(
                            "iteration",
                            "",
                        )
                    ).strip()


                    if not iteration_name:

                        continue


                    flex_expected_by_iteration[
                        iteration_name
                    ] = [
                        str(
                            participant
                        ).strip()
                        for participant
                        in (
                            config.get(
                                "participants",
                                []
                            )
                            or []
                        )
                        if str(
                            participant
                        ).strip()
                    ]


                flex_progress.progress(
                    30
                )


                # ------------------------------------------------
                # STEP 3 - LOAD BOTH STAGES
                # ------------------------------------------------

                flex_status.write(
                    "Loading Before and After submissions..."
                )


                flex_payloads = []


                for config in flex_configs:

                    iteration_name = str(
                        config.get(
                            "iteration",
                            "",
                        )
                    ).strip()


                    if not iteration_name:

                        continue


                    if (
                        iteration_name
                        == LOCAL_GLOBAL_FIXTURE_ITERATION
                        and local_global_fixture_enabled()
                    ):

                        fixture_payloads = (
                            load_local_global_fixture_payloads()
                        )


                        flex_payloads.extend(
                            fixture_payloads
                        )


                        continue


                    before_payloads = (
                        list_submission_payloads(
                            iteration_name,
                            stage="before",
                        )
                    )


                    after_payloads = (
                        list_submission_payloads(
                            iteration_name,
                            stage="after",
                        )
                    )


                    flex_payloads.extend(
                        before_payloads
                    )


                    flex_payloads.extend(
                        after_payloads
                    )


                flex_progress.progress(
                    55
                )


                # ------------------------------------------------
                # STEP 4 - NORMALIZED DATASET
                # ------------------------------------------------

                flex_status.write(
                    "Building normalized analytics dataset..."
                )


                flex_normalized_df = (
                    normalize_global_indicator_rows(
                        flex_payloads
                    )
                )


                flex_progress.progress(
                    70
                )


                # ------------------------------------------------
                # STEP 5 - SCOPE SUMMARY
                # ------------------------------------------------

                flex_status.write(
                    "Calculating iteration and pathway summaries..."
                )


                flex_summary_df = (
                    build_global_scope_summary(
                        flex_payloads,
                        expected_participants_by_iteration=(
                            flex_expected_by_iteration
                        ),
                    )
                )


                flex_progress.progress(
                    90
                )


                # ------------------------------------------------
                # STORE
                # ------------------------------------------------

                st.session_state[
                    "flex_global_normalized_df"
                ] = flex_normalized_df


                st.session_state[
                    "flex_global_summary_df"
                ] = flex_summary_df


                st.session_state[
                    "flex_global_expected"
                ] = flex_expected_by_iteration


                st.session_state[
                    "flex_global_payload_count"
                ] = len(
                    flex_payloads
                )


                flex_progress.progress(
                    100
                )


                flex_status.update(
                    label=(
                        "Global analysis data ready"
                    ),
                    state="complete",
                    expanded=False,
                )


        except Exception as exc:

            st.error(
                "Could not prepare global analysis data: "
                f"{exc}"
            )


    # ========================================================
    # READ CACHED SESSION DATA
    # ========================================================

    flex_normalized_df = (
        st.session_state.get(
            "flex_global_normalized_df"
        )
    )


    flex_summary_df = (
        st.session_state.get(
            "flex_global_summary_df"
        )
    )


    if (
        flex_normalized_df is None
        or flex_summary_df is None
    ):

        st.info(
            "Click Load or refresh analysis data "
            "to prepare iteration and pathway comparisons."
        )


    elif (
        flex_normalized_df.empty
        and flex_summary_df.empty
    ):

        st.warning(
            "No usable global indicator data is available yet."
        )


    else:

        flex_payload_count = (
            st.session_state.get(
                "flex_global_payload_count",
                0,
            )
        )


        flex_info_1, flex_info_2, flex_info_3 = (
            st.columns(
                3
            )
        )


        flex_info_1.metric(
            "Submission payloads",
            flex_payload_count,
        )


        flex_info_2.metric(
            "Iterations",
            (
                flex_summary_df[
                    "Iteration"
                ].nunique()
                if not flex_summary_df.empty
                else 0
            ),
        )


        flex_info_3.metric(
            "Pathways",
            (
                flex_summary_df[
                    "Pathway"
                ].nunique()
                if not flex_summary_df.empty
                else 0
            ),
        )


        # ====================================================
        # STUDIO TABS
        # ====================================================

        (
            flex_progress_tab,
            flex_custom_tab,
        ) = st.tabs(
            [
                "Iteration progress",
                "Custom analysis",
            ]
        )


        # ====================================================
        # TAB 1 - ITERATION PROGRESS
        # ====================================================

        with flex_progress_tab:

            st.markdown(
                "#### Iteration progress"
            )

            st.caption(
                "Track whether global conditions are moving "
                "toward the targets from one iteration to the next."
            )


            if flex_summary_df.empty:

                st.info(
                    "No iteration summary is available."
                )


            else:

                progress_pathways = sorted(
                    flex_summary_df[
                        "Pathway"
                    ]
                    .dropna()
                    .astype(str)
                    .unique()
                    .tolist(),
                    key=str.casefold,
                )


                progress_stages = [
                    stage
                    for stage in [
                        "Before trade adjustment",
                        "After trade adjustment",
                    ]
                    if stage
                    in set(
                        flex_summary_df[
                            "Stage"
                        ].astype(str)
                    )
                ]


                progress_filter_1, progress_filter_2 = (
                    st.columns(
                        2
                    )
                )


                with progress_filter_1:

                    selected_progress_pathways = (
                        st.multiselect(
                            "Pathways",
                            progress_pathways,
                            default=progress_pathways,
                            key=(
                                "flex_progress_pathways"
                            ),
                            format_func=plain_text_label,
                        )
                    )


                with progress_filter_2:

                    default_progress_stage = (
                        [
                            "After trade adjustment"
                        ]
                        if (
                            "After trade adjustment"
                            in progress_stages
                        )
                        else progress_stages[:1]
                    )


                    selected_progress_stages = (
                        st.multiselect(
                            "Trade stages",
                            progress_stages,
                            default=progress_stages,
                            key=(
                                "flex_progress_stages"
                            ),
                            format_func=plain_text_label,
                        )
                    )


                show_incomplete_progress = (
                    st.checkbox(
                        "Include incomplete iteration scopes",
                        value=False,
                        key=(
                            "flex_progress_incomplete"
                        ),
                        help=(
                            "Incomplete scopes do not yet contain "
                            "all configured participant submissions."
                        ),
                    )
                )


                progress_df = (
                    flex_summary_df[
                        flex_summary_df[
                            "Pathway"
                        ].isin(
                            selected_progress_pathways
                        )
                        &
                        flex_summary_df[
                            "Stage"
                        ].isin(
                            selected_progress_stages
                        )
                    ]
                    .copy()
                )


                if not show_incomplete_progress:

                    progress_df = (
                        progress_df[
                            progress_df[
                                "Coverage complete"
                            ]
                            == True
                        ]
                        .copy()
                    )


                if progress_df.empty:

                    st.info(
                        "No matching complete iteration scope "
                        "is available for this selection."
                    )


                else:

                    progress_iteration_order = sorted(
                        progress_df[
                            "Iteration"
                        ]
                        .astype(str)
                        .unique()
                        .tolist(),
                        key=flex_iteration_sort_key,
                    )


                    progress_df[
                        "Iteration"
                    ] = pd.Categorical(
                        progress_df[
                            "Iteration"
                        ],
                        categories=(
                            progress_iteration_order
                        ),
                        ordered=True,
                    )

                    progress_df[
                        "Iteration display"
                    ] = (
                        progress_df[
                            "Iteration"
                        ]
                        .astype(str)
                        .map(
                            plain_text_label
                        )
                    )


                    progress_df = (
                        progress_df.sort_values(
                            [
                                "Iteration",
                                "Pathway",
                                "Stage",
                            ]
                        )
                    )


                    progress_df[
                        "Series"
                    ] = (
                        (
                            progress_df[
                                "Pathway"
                            ].astype(str)
                            + " | "
                            + progress_df[
                                "Stage"
                            ].astype(str)
                        )
                        .map(
                            plain_text_label
                        )
                    )


                    # ----------------------------------------
                    # FOOD SECURITY PROGRESS
                    # ----------------------------------------

                    progress_food_fig = (
                        px.bar(
                            progress_df,
                            x="Iteration display",
                            y=(
                                "Food security passing percent"
                            ),
                            color="Series",
                            barmode="group",
                            title=(
                                "Food security progress"
                            ),
                            labels={
                                "Food security passing percent": (
                                    "Locations passing (%)"
                                ),
                                "Iteration display": (
                                    "Iteration"
                                ),
                                "Series": "",
                            },
                        )
                    )


                    progress_food_fig.add_hline(
                        y=100,
                        line_dash="dash",
                        annotation_text=(
                            "Target: 100% of locations"
                        ),
                        annotation_position="top left",
                    )


                    flex_prepare_figure(
                        progress_food_fig,
                        height=500,
                    )


                    # ----------------------------------------
                    # AGRICULTURE GHG PROGRESS
                    # ----------------------------------------

                    progress_agri_fig = (
                        px.bar(
                            progress_df,
                            x="Iteration display",
                            y="Agriculture GHG",
                            color="Series",
                            barmode="group",
                            title=(
                                "Agriculture GHG progress"
                            ),
                            labels={
                                "Agriculture GHG": (
                                    "Global total (Gt CO2e)"
                                ),
                                "Iteration display": (
                                    "Iteration"
                                ),
                                "Series": "",
                            },
                        )
                    )


                    progress_agri_fig.add_hline(
                        y=4.0,
                        line_dash="dash",
                        annotation_text=(
                            "Target: 4 Gt CO2e"
                        ),
                        annotation_position="top left",
                    )


                    flex_prepare_figure(
                        progress_agri_fig,
                        height=500,
                    )


                    # ----------------------------------------
                    # LAND GHG PROGRESS
                    # ----------------------------------------

                    progress_land_fig = (
                        px.bar(
                            progress_df,
                            x="Iteration display",
                            y="Land use change GHG",
                            color="Series",
                            barmode="group",
                            title=(
                                "Land use change GHG progress"
                            ),
                            labels={
                                "Land use change GHG": (
                                    "Global total (Gt CO2e)"
                                ),
                                "Iteration display": (
                                    "Iteration"
                                ),
                                "Series": "",
                            },
                        )
                    )


                    progress_land_fig.add_hline(
                        y=-1.0,
                        line_dash="dash",
                        annotation_text=(
                            "Target: -1 Gt CO2e"
                        ),
                        annotation_position="bottom left",
                    )


                    flex_prepare_figure(
                        progress_land_fig,
                        height=500,
                    )


                    # ----------------------------------------
                    # THREE SIDE BY SIDE
                    # ----------------------------------------

                    (
                        progress_col_1,
                        progress_col_2,
                        progress_col_3,
                    ) = st.columns(
                        [
                            1.25,
                            1,
                            1,
                        ]
                    )


                    with progress_col_1:

                        st.plotly_chart(
                            progress_food_fig,
                            use_container_width=True,
                            key=(
                                "flex_progress_food_chart"
                            ),
                        )


                    with progress_col_2:

                        st.plotly_chart(
                            progress_agri_fig,
                            use_container_width=True,
                            key=(
                                "flex_progress_agri_chart"
                            ),
                        )


                    with progress_col_3:

                        st.plotly_chart(
                            progress_land_fig,
                            use_container_width=True,
                            key=(
                                "flex_progress_land_chart"
                            ),
                        )


                    with st.expander(
                        "View iteration progress data"
                    ):

                        st.dataframe(
                            pretty_dataframe(
                                progress_df.drop(
                                    columns=[
                                        "Series"
                                    ],
                                    errors="ignore",
                                )
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )


        # ====================================================
        # TAB 2 - CUSTOM ANALYSIS
        # ====================================================

        with flex_custom_tab:

            st.markdown(
                "#### Custom analysis"
            )

            st.caption(
                "Choose any combination of iterations, pathways "
                "and trade stages. The three core indicators "
                "update together."
            )


            custom_iterations = sorted(
                flex_summary_df[
                    "Iteration"
                ]
                .dropna()
                .astype(str)
                .unique()
                .tolist(),
                key=flex_iteration_sort_key,
            )


            custom_pathways = sorted(
                flex_summary_df[
                    "Pathway"
                ]
                .dropna()
                .astype(str)
                .unique()
                .tolist(),
                key=str.casefold,
            )


            custom_stages = [
                stage
                for stage in [
                    "Before trade adjustment",
                    "After trade adjustment",
                ]
                if stage
                in set(
                    flex_summary_df[
                        "Stage"
                    ].astype(str)
                )
            ]


            (
                custom_filter_1,
                custom_filter_2,
                custom_filter_3,
            ) = st.columns(
                3
            )


            with custom_filter_1:

                default_iterations = (
                    custom_iterations[
                        -1:
                    ]
                )


                selected_custom_iterations = (
                    st.multiselect(
                        "Iterations",
                        custom_iterations,
                        default=default_iterations,
                        key=(
                            "flex_custom_iterations"
                        ),
                        format_func=plain_text_label,
                    )
                )


            with custom_filter_2:

                selected_custom_pathways = (
                    st.multiselect(
                        "Pathways",
                        custom_pathways,
                        default=custom_pathways,
                        key=(
                            "flex_custom_pathways"
                        ),
                        format_func=plain_text_label,
                    )
                )


            with custom_filter_3:

                selected_custom_stages = (
                    st.multiselect(
                        "Trade stages",
                        custom_stages,
                        default=custom_stages,
                        key=(
                            "flex_custom_stages"
                        ),
                        format_func=plain_text_label,
                    )
                )


            custom_level = (
                st.radio(
                    "Analysis level",
                    [
                        "Global comparison",
                        "Location detail",
                    ],
                    horizontal=True,
                    key="flex_custom_level",
                )
            )


            # =================================================
            # GLOBAL COMPARISON
            # =================================================

            if (
                custom_level
                == "Global comparison"
            ):

                custom_scope_df = (
                    flex_summary_df[
                        flex_summary_df[
                            "Iteration"
                        ].astype(str).isin(
                            selected_custom_iterations
                        )
                        &
                        flex_summary_df[
                            "Pathway"
                        ].astype(str).isin(
                            selected_custom_pathways
                        )
                        &
                        flex_summary_df[
                            "Stage"
                        ].astype(str).isin(
                            selected_custom_stages
                        )
                    ]
                    .copy()
                )


                if custom_scope_df.empty:

                    st.info(
                        "No global scope matches the current selection."
                    )


                else:

                    custom_scope_df[
                        "Scenario"
                    ] = (
                        custom_scope_df.apply(
                            flex_scope_label,
                            axis=1,
                        )
                    )


                    custom_scope_df[
                        "Food security"
                    ] = (
                        custom_scope_df[
                            "Food security passing percent"
                        ]
                    )


                    # -----------------------------------------
                    # GLOBAL FOOD
                    # -----------------------------------------

                    custom_food_fig = (
                        px.bar(
                            custom_scope_df,
                            x="Scenario",
                            y="Food security",
                            color="Stage",
                            barmode="group",
                            title=(
                                "Food security comparison"
                            ),
                            labels={
                                "Food security": (
                                    "Locations passing (%)"
                                ),
                                "Scenario": (
                                    "Scenario"
                                ),
                                "Stage": (
                                    "Trade stage"
                                ),
                            },
                        )
                    )


                    custom_food_fig.add_hline(
                        y=100,
                        line_dash="dash",
                        annotation_text=(
                            "Target: 100%"
                        ),
                    )


                    flex_prepare_figure(
                        custom_food_fig,
                        height=540,
                        tickangle=-40,
                    )


                    # -----------------------------------------
                    # GLOBAL AGRICULTURE
                    # -----------------------------------------

                    custom_agri_fig = (
                        px.bar(
                            custom_scope_df,
                            x="Scenario",
                            y="Agriculture GHG",
                            color="Stage",
                            barmode="group",
                            title=(
                                "Agriculture GHG comparison"
                            ),
                            labels={
                                "Agriculture GHG": (
                                    "Global total (Gt CO2e)"
                                ),
                                "Scenario": (
                                    "Scenario"
                                ),
                                "Stage": (
                                    "Trade stage"
                                ),
                            },
                        )
                    )


                    custom_agri_fig.add_hline(
                        y=4.0,
                        line_dash="dash",
                        annotation_text=(
                            "Target: 4 Gt CO2e"
                        ),
                    )


                    flex_prepare_figure(
                        custom_agri_fig,
                        height=540,
                        tickangle=-40,
                    )


                    # -----------------------------------------
                    # GLOBAL LAND
                    # -----------------------------------------

                    custom_land_fig = (
                        px.bar(
                            custom_scope_df,
                            x="Scenario",
                            y="Land use change GHG",
                            color="Stage",
                            barmode="group",
                            title=(
                                "Land use change GHG comparison"
                            ),
                            labels={
                                "Land use change GHG": (
                                    "Global total (Gt CO2e)"
                                ),
                                "Scenario": (
                                    "Scenario"
                                ),
                                "Stage": (
                                    "Trade stage"
                                ),
                            },
                        )
                    )


                    custom_land_fig.add_hline(
                        y=-1.0,
                        line_dash="dash",
                        annotation_text=(
                            "Target: -1 Gt CO2e"
                        ),
                    )


                    flex_prepare_figure(
                        custom_land_fig,
                        height=540,
                        tickangle=-40,
                    )


                    # -----------------------------------------
                    # THREE SIDE BY SIDE
                    # -----------------------------------------

                    (
                        custom_col_1,
                        custom_col_2,
                        custom_col_3,
                    ) = st.columns(
                        [
                            1.25,
                            1,
                            1,
                        ]
                    )


                    with custom_col_1:

                        st.plotly_chart(
                            custom_food_fig,
                            use_container_width=True,
                            key=(
                                "flex_custom_global_food"
                            ),
                        )


                    with custom_col_2:

                        st.plotly_chart(
                            custom_agri_fig,
                            use_container_width=True,
                            key=(
                                "flex_custom_global_agri"
                            ),
                        )


                    with custom_col_3:

                        st.plotly_chart(
                            custom_land_fig,
                            use_container_width=True,
                            key=(
                                "flex_custom_global_land"
                            ),
                        )


                    incomplete_scopes = (
                        custom_scope_df[
                            custom_scope_df[
                                "Coverage complete"
                            ]
                            != True
                        ]
                    )


                    if not incomplete_scopes.empty:

                        st.warning(
                            "One or more selected scenarios are incomplete. "
                            "Their values use only the submissions currently available."
                        )


                    with st.expander(
                        "View global comparison data"
                    ):

                        st.dataframe(
                            pretty_dataframe(
                                custom_scope_df.drop(
                                    columns=[
                                        "Scenario"
                                    ],
                                    errors="ignore",
                                )
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )


            # =================================================
            # LOCATION DETAIL
            # =================================================

            else:

                available_years = sorted(
                    pd.to_numeric(
                        flex_normalized_df[
                            "Year"
                        ],
                        errors="coerce",
                    )
                    .dropna()
                    .astype(int)
                    .unique()
                    .tolist()
                )


                year_filter_1, year_filter_2 = (
                    st.columns(
                        2
                    )
                )


                with year_filter_1:

                    food_default_index = (
                        available_years.index(
                            2050
                        )
                        if 2050 in available_years
                        else (
                            len(
                                available_years
                            )
                            - 1
                        )
                    )


                    custom_food_year = (
                        st.selectbox(
                            "Food security year",
                            available_years,
                            index=(
                                food_default_index
                            ),
                            key=(
                                "flex_custom_food_year"
                            ),
                        )
                    )


                with year_filter_2:

                    ghg_default_index = (
                        available_years.index(
                            2050
                        )
                        if 2050 in available_years
                        else (
                            len(
                                available_years
                            )
                            - 1
                        )
                    )


                    custom_ghg_year = (
                        st.selectbox(
                            "GHG year",
                            available_years,
                            index=(
                                ghg_default_index
                            ),
                            key=(
                                "flex_custom_ghg_year"
                            ),
                        )
                    )


                custom_location_df = (
                    filter_global_indicator_rows(
                        flex_normalized_df,
                        iterations=(
                            selected_custom_iterations
                        ),
                        pathways=(
                            selected_custom_pathways
                        ),
                        stages=(
                            selected_custom_stages
                        ),
                    )
                )


                if custom_location_df.empty:

                    st.info(
                        "No Location-level data matches the current selection."
                    )


                else:

                    custom_location_df[
                        "Scenario"
                    ] = (
                        (
                            custom_location_df[
                                "Iteration"
                            ].astype(str)
                            + " | "
                            + custom_location_df[
                                "Pathway"
                            ].astype(str)
                            + " | "
                            + custom_location_df[
                                "Stage"
                            ].astype(str)
                        )
                        .map(
                            plain_text_label
                        )
                    )


                    # -----------------------------------------
                    # FOOD LOCATION DETAIL
                    # -----------------------------------------

                    location_food_df = (
                        custom_location_df[
                            (
                                custom_location_df[
                                    "Indicator"
                                ]
                                == (
                                    "Food security margin"
                                )
                            )
                            &
                            (
                                custom_location_df[
                                    "Year"
                                ]
                                == custom_food_year
                            )
                        ]
                        .copy()
                    )


                    location_food_fig = (
                        px.bar(
                            location_food_df,
                            x="Location",
                            y="Value",
                            color="Scenario",
                            barmode="group",
                            title=(
                                "Food security by Location"
                            ),
                            labels={
                                "Value": (
                                    "Food security margin (%)"
                                ),
                                "Location": (
                                    "Location"
                                ),
                                "Scenario": (
                                    "Scenario"
                                ),
                            },
                        )
                    )


                    location_food_fig.add_hline(
                        y=10,
                        line_dash="dash",
                        annotation_text="10%",
                    )


                    location_food_fig.add_hline(
                        y=50,
                        line_dash="dash",
                        annotation_text="50%",
                    )


                    flex_prepare_figure(
                        location_food_fig,
                        height=560,
                        tickangle=-55,
                    )


                    # -----------------------------------------
                    # AGRICULTURE LOCATION DETAIL
                    # -----------------------------------------

                    location_agri_df = (
                        custom_location_df[
                            (
                                custom_location_df[
                                    "Indicator"
                                ]
                                == (
                                    "Agriculture GHG"
                                )
                            )
                            &
                            (
                                custom_location_df[
                                    "Year"
                                ]
                                == custom_ghg_year
                            )
                        ]
                        .copy()
                    )


                    location_agri_fig = (
                        px.bar(
                            location_agri_df,
                            x="Location",
                            y="Value",
                            color="Scenario",
                            barmode="group",
                            title=(
                                "Agriculture GHG by Location"
                            ),
                            labels={
                                "Value": (
                                    "Contribution (Gt CO2e)"
                                ),
                                "Location": (
                                    "Location"
                                ),
                                "Scenario": (
                                    "Scenario"
                                ),
                            },
                        )
                    )


                    if (
                        custom_ghg_year
                        == 2050
                    ):

                        location_agri_fig.add_hline(
                            y=4.0,
                            line_dash="dash",
                            annotation_text=(
                                "Global target: 4 Gt CO2e"
                            ),
                        )


                    flex_prepare_figure(
                        location_agri_fig,
                        height=560,
                        tickangle=-55,
                    )


                    # -----------------------------------------
                    # LAND LOCATION DETAIL
                    # -----------------------------------------

                    location_land_df = (
                        custom_location_df[
                            (
                                custom_location_df[
                                    "Indicator"
                                ]
                                == (
                                    "Land use change GHG"
                                )
                            )
                            &
                            (
                                custom_location_df[
                                    "Year"
                                ]
                                == custom_ghg_year
                            )
                        ]
                        .copy()
                    )


                    location_land_fig = (
                        px.bar(
                            location_land_df,
                            x="Location",
                            y="Value",
                            color="Scenario",
                            barmode="group",
                            title=(
                                "Land use change GHG by Location"
                            ),
                            labels={
                                "Value": (
                                    "Contribution (Gt CO2e)"
                                ),
                                "Location": (
                                    "Location"
                                ),
                                "Scenario": (
                                    "Scenario"
                                ),
                            },
                        )
                    )


                    if (
                        custom_ghg_year
                        == 2050
                    ):

                        location_land_fig.add_hline(
                            y=-1.0,
                            line_dash="dash",
                            annotation_text=(
                                "Global target: -1 Gt CO2e"
                            ),
                        )


                    flex_prepare_figure(
                        location_land_fig,
                        height=560,
                        tickangle=-55,
                    )


                    # -----------------------------------------
                    # THREE SIDE BY SIDE
                    # -----------------------------------------

                    (
                        location_col_1,
                        location_col_2,
                        location_col_3,
                    ) = st.columns(
                        [
                            1.25,
                            1,
                            1,
                        ]
                    )


                    with location_col_1:

                        st.plotly_chart(
                            location_food_fig,
                            use_container_width=True,
                            key=(
                                "flex_location_food"
                            ),
                        )


                    with location_col_2:

                        st.plotly_chart(
                            location_agri_fig,
                            use_container_width=True,
                            key=(
                                "flex_location_agri"
                            ),
                        )


                    with location_col_3:

                        st.plotly_chart(
                            location_land_fig,
                            use_container_width=True,
                            key=(
                                "flex_location_land"
                            ),
                        )


                    st.caption(
                        "Location values are taken directly from the uploaded calculator. "
                        "Country calculators show countries and regional calculators show regions."
                    )


                    with st.expander(
                        "View selected Location data"
                    ):

                        st.dataframe(
                            pretty_dataframe(
                                custom_location_df.drop(
                                    columns=[
                                        "Scenario"
                                    ],
                                    errors="ignore",
                                )
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )


    # ========================================================
    # FLEXIBLE GLOBAL ANALYSIS STUDIO - END
    # ========================================================


    st.divider()


    st.subheader("Trade Analytics")

    st.caption(
        "Interactive analysis of published trade adjustment runs. "
        "This section is visible to all app users; admin access is "
        "not required. Clicking Run Analysis only builds charts from "
        "published results - it does not rerun the trade engine."
    )

    # --------------------------------------------------------
    # Find iterations with a current analytics artifact
    # --------------------------------------------------------

    try:
        analytics_configs = (
            list_iteration_configs()
        )

    except Exception as exc:

        analytics_configs = []

        st.error(
            f"Could not load iterations: {exc}"
        )


    available_analytics_iterations = []


    for cfg in analytics_configs:

        name = cfg.get(
            "iteration",
            "",
        )

        if not name:
            continue

        # Do not expose analytics from a stale run.
        if load_stale_marker(name):
            continue

        try:

            if github_get_file(
                analytics_path(name)
            ):

                available_analytics_iterations.append(
                    name
                )

        except Exception:
            pass


    # --------------------------------------------------------
    # No analytics yet
    # --------------------------------------------------------

    if not available_analytics_iterations:

        st.info(
            "No current analytics dataset is available. "
            "Run Trade Adjustment from the Admin tab. "
            "An iteration created by an older app version "
            "must be rerun once to generate its analytics file."
        )


    else:

        # ====================================================
        # FILTERS
        # ====================================================

        filter_c1, filter_c2, filter_c3 = (
            st.columns(3)
        )


        with filter_c1:

            selected_analytics_iteration = (
                st.selectbox(
                    "Iteration",
                    available_analytics_iterations,
                    key="analytics_iteration",
                )
            )


        # ----------------------------------------------------
        # Load current published iteration data.
        # This is needed to populate pathway/product filters.
        # ----------------------------------------------------

        with st.spinner(
            "Loading published analytics data..."
        ):

            analytics_df = (
                load_iteration_analytics(
                    selected_analytics_iteration
                )
            )

            run_summary = (
                load_run_summary(
                    selected_analytics_iteration
                )
            )

            analytics_config = (
                load_iteration_config(
                    selected_analytics_iteration
                )
                or {}
            )


        if analytics_df.empty:

            st.warning(
                "The selected iteration has an analytics file, "
                "but no usable analytics rows were found. "
                "Rerun Trade Adjustment from the Admin tab."
            )


        else:

            pathway_options = sorted(
                analytics_df[
                    "Pathway"
                ]
                .dropna()
                .astype(str)
                .unique()
            )


            with filter_c2:

                selected_analytics_pathway = (
                    st.selectbox(
                        "Pathway",
                        pathway_options,
                        key="analytics_pathway",
                    )
                )


            product_options = sorted(
                analytics_df.loc[
                    (
                        analytics_df[
                            "Pathway"
                        ].astype(str)
                        == selected_analytics_pathway
                    ),
                    "ProductDisplay",
                ]
                .dropna()
                .astype(str)
                .unique(),
                key=str.casefold,
            )


            with filter_c3:

                selected_analytics_product = (
                    st.selectbox(
                        "Product",
                        [
                            "All products"
                        ]
                        + product_options,
                        key="analytics_product",
                    )
                )


            # =================================================
            # RUN ANALYSIS BUTTON
            # =================================================

            analysis_token = (
                f"{selected_analytics_iteration}|"
                f"{selected_analytics_pathway}|"
                f"{selected_analytics_product}"
            )


            run_analysis = st.button(
                "Run Analysis",
                type="primary",
                key="run_trade_analytics",
                help=(
                    "Build the interactive charts for the "
                    "selected iteration, pathway and product."
                ),
            )


            if run_analysis:

                st.session_state[
                    "analytics_run_token"
                ] = analysis_token


            analysis_is_current = (
                st.session_state.get(
                    "analytics_run_token"
                )
                == analysis_token
            )


            if not analysis_is_current:

                st.info(
                    "Choose the filters above, then click "
                    "Run Analysis to build the interactive charts."
                )


            else:

                # =============================================
                # LIVE ANALYSIS STATUS
                # =============================================

                with st.status(
                    "Running trade analytics...",
                    expanded=True,
                ) as analysis_status:

                    analysis_progress = (
                        st.progress(0)
                    )


                    # -----------------------------------------
                    # STEP 1
                    # -----------------------------------------

                    analysis_status.write(
                        "Reading submission coverage "
                        "and run metadata..."
                    )

                    submission_meta = (
                        submission_metadata_df(
                            selected_analytics_iteration
                        )
                    )


                    submitted_participants = (
                        submission_meta[
                            "participant"
                        ]
                        .astype(str)
                        .nunique()
                        if not submission_meta.empty
                        else 0
                    )


                    configured_participants = len(
                        analytics_config.get(
                            "participants",
                            [],
                        )
                    )


                    analysis_progress.progress(
                        20
                    )


                    # -----------------------------------------
                    # STEP 2
                    # -----------------------------------------

                    analysis_status.write(
                        "Aggregating the selected pathway "
                        "and product scope..."
                    )


                    selected_pathway_products = (
                        analytics_df.loc[
                            (
                                analytics_df[
                                    "Pathway"
                                ].astype(str)
                                == selected_analytics_pathway
                            ),
                            "Product",
                        ]
                        .astype(str)
                        .nunique()
                    )


                    trade_view = (
                        aggregate_trade_view(
                            analytics_df,
                            pathway=(
                                selected_analytics_pathway
                            ),
                            product_display=(
                                selected_analytics_product
                            ),
                        )
                    )


                    analysis_progress.progress(
                        40
                    )


                    # -----------------------------------------
                    # STEP 3
                    # -----------------------------------------

                    analysis_status.write(
                        "Preparing product impact "
                        "and heatmap data..."
                    )


                    pathway_df = analytics_df[
                        (
                            analytics_df[
                                "Pathway"
                            ].astype(str)
                            == selected_analytics_pathway
                        )
                    ].copy()


                    product_impact = (
                        pathway_df
                        .groupby(
                            [
                                "Product",
                                "ProductDisplay",
                            ],
                            as_index=False,
                        )[
                            "AbsoluteExportAdjustment"
                        ]
                        .sum(
                            min_count=1
                        )
                        .sort_values(
                            "AbsoluteExportAdjustment",
                            ascending=False,
                            na_position="last",
                        )
                    )


                    analysis_progress.progress(
                        60
                    )


                    # -----------------------------------------
                    # STEP 4 - PATHWAY COMPARISON DATA
                    # -----------------------------------------

                    analysis_status.write(
                        "Preparing pathway and "
                        "iteration comparisons..."
                    )


                    pathway_comparison_source = (
                        analytics_df.copy()
                    )


                    if (
                        selected_analytics_product
                        != "All products"
                    ):

                        pathway_comparison_source = (
                            pathway_comparison_source[
                                (
                                    pathway_comparison_source[
                                        "ProductDisplay"
                                    ].astype(str)
                                    == selected_analytics_product
                                )
                            ].copy()
                        )


                    pathway_compare = (
                        pathway_comparison_source
                        .groupby(
                            [
                                "Pathway",
                                "Year",
                            ],
                            as_index=False,
                        )[
                            [
                                "ImportsOriginal",
                                "ExportsOriginal",
                                "ImportsAdj",
                                "ExportsAdj",
                            ]
                        ]
                        .sum(
                            min_count=1
                        )
                    )


                    if not pathway_compare.empty:

                        pathway_compare[
                            "TradeGapBefore"
                        ] = (
                            pathway_compare[
                                "ImportsOriginal"
                            ]
                            - pathway_compare[
                                "ExportsOriginal"
                            ]
                        ).abs()


                        pathway_compare[
                            "TradeGapAfter"
                        ] = (
                            pathway_compare[
                                "ImportsAdj"
                            ]
                            - pathway_compare[
                                "ExportsAdj"
                            ]
                        ).abs()


                        pathway_compare[
                            "ExportAdjustment"
                        ] = (
                            pathway_compare[
                                "ExportsAdj"
                            ]
                            - pathway_compare[
                                "ExportsOriginal"
                            ]
                        )


                        pathway_compare[
                            "GapReduction"
                        ] = (
                            pathway_compare[
                                "TradeGapBefore"
                            ]
                            - pathway_compare[
                                "TradeGapAfter"
                            ]
                        )


                    all_analytics = (
                        load_all_published_analytics()
                    )


                    analysis_progress.progress(
                        85
                    )


                    # -----------------------------------------
                    # STEP 5
                    # -----------------------------------------

                    analysis_status.write(
                        "Building interactive Plotly figures..."
                    )


                    analysis_progress.progress(
                        100
                    )


                    analysis_status.update(
                        label="Analysis ready",
                        state="complete",
                        expanded=False,
                    )


                # =================================================
                # RUN STATUS
                # =================================================

                complete = bool(
                    run_summary.get(
                        "complete",
                        False,
                    )
                )


                blocker_count = int(
                    run_summary.get(
                        "blocker_count",
                        0,
                    )
                    or 0
                )


                expected_reports = int(
                    run_summary.get(
                        "expected_reports",
                        0,
                    )
                    or 0
                )


                reports_used = int(
                    run_summary.get(
                        "submitted_reports",
                        run_summary.get(
                            "reports_used",
                            0,
                        ),
                    )
                    or 0
                )


                if (
                    complete
                    and blocker_count == 0
                ):

                    st.success(
                        "FINAL run - all configured reports "
                        "are present and the engine reported "
                        "0 QA blockers."
                    )


                elif complete:

                    st.warning(
                        f"PROVISIONAL run - all configured "
                        f"reports are present, but "
                        f"{blocker_count} QA blocker(s) "
                        "require review."
                    )


                else:

                    st.warning(
                        f"PROVISIONAL run - "
                        f"{reports_used}/{expected_reports} "
                        "configured reports were included "
                        "and/or QA blockers are present."
                    )


                if blocker_count:

                    st.warning(
                        "Some blocker-affected ExportsAdj values "
                        "may be blank. After-adjustment aggregates "
                        "are therefore provisional and can be "
                        "incomplete for affected "
                        "product/pathway/year combinations."
                    )


                # =================================================
                # KPI CARDS
                # =================================================

                latest_row = (
                    trade_view
                    .sort_values(
                        "Year"
                    )
                    .tail(1)
                    if not trade_view.empty
                    else pd.DataFrame()
                )


                before_latest = (
                    float(
                        latest_row[
                            "ExportsOriginal"
                        ].iloc[0]
                    )
                    if not latest_row.empty
                    else 0.0
                )


                after_latest = (
                    float(
                        latest_row[
                            "ExportsAdj"
                        ].iloc[0]
                    )
                    if (
                        not latest_row.empty
                        and pd.notna(
                            latest_row[
                                "ExportsAdj"
                            ].iloc[0]
                        )
                    )
                    else float(
                        "nan"
                    )
                )


                latest_adjustment = (
                    after_latest
                    - before_latest
                    if pd.notna(
                        after_latest
                    )
                    else float(
                        "nan"
                    )
                )


                k1, k2, k3, k4, k5 = (
                    st.columns(5)
                )


                k1.metric(
                    "Participants",
                    (
                        f"{submitted_participants}/"
                        f"{configured_participants}"
                        if configured_participants
                        else str(
                            submitted_participants
                        )
                    ),
                )


                k2.metric(
                    "Products",
                    int(
                        selected_pathway_products
                    ),
                )


                k3.metric(
                    "Latest exports before",
                    f"{before_latest:,.0f}",
                )


                k4.metric(
                    "Latest exports after",
                    (
                        f"{after_latest:,.0f}"
                        if pd.notna(
                            after_latest
                        )
                        else "Unavailable"
                    ),
                )


                k5.metric(
                    "Latest export change",
                    (
                        f"{latest_adjustment:,.0f}"
                        if pd.notna(
                            latest_adjustment
                        )
                        else "Unavailable"
                    ),
                )


                scope_label = (
                    "All products"
                    if (
                        selected_analytics_product
                        == "All products"
                    )
                    else selected_analytics_product
                )


                # =================================================
                # 1. AGGREGATE TRADE TRAJECTORY
                # =================================================

                st.markdown(
                    "### Aggregate trade trajectory"
                )


                trajectory_fig = (
                    go.Figure()
                )


                trajectory_fig.add_trace(
                    go.Scatter(
                        x=trade_view[
                            "Year"
                        ],
                        y=trade_view[
                            "ImportsOriginal"
                        ],
                        mode="lines+markers",
                        name="Imports",
                    )
                )


                trajectory_fig.add_trace(
                    go.Scatter(
                        x=trade_view[
                            "Year"
                        ],
                        y=trade_view[
                            "ExportsOriginal"
                        ],
                        mode="lines+markers",
                        name=(
                            "Exports before adjustment"
                        ),
                    )
                )


                trajectory_fig.add_trace(
                    go.Scatter(
                        x=trade_view[
                            "Year"
                        ],
                        y=trade_view[
                            "ExportsAdj"
                        ],
                        mode="lines+markers",
                        name=(
                            "Exports after adjustment"
                        ),
                    )
                )


                if not trade_view.empty:

                    trajectory_fig.add_vrect(
                        x0=float(
                            trade_view[
                                "Year"
                            ].min()
                        ),
                        x1=2020,
                        opacity=0.08,
                        line_width=0,
                        annotation_text=(
                            "Historical - unchanged"
                        ),
                        annotation_position=(
                            "top left"
                        ),
                    )


                trajectory_fig.update_layout(
                    title=(
                        "Submitted-participant aggregate | "
                        f"{selected_analytics_iteration} | "
                        f"{selected_analytics_pathway} | "
                        f"{scope_label}"
                    ),
                    xaxis_title="Year",
                    yaxis_title=(
                        "Submitted-participant "
                        "trade quantity"
                    ),
                    hovermode="x unified",
                    legend_title_text="Series",
                )


                st.plotly_chart(
                    trajectory_fig,
                    use_container_width=True,
                    key="analytics_trajectory",
                )


                # =================================================
                # 2 + 3. ADJUSTMENT + GAP
                # =================================================

                chart_left, chart_right = (
                    st.columns(2)
                )


                with chart_left:

                    st.markdown(
                        "### Adjustment by year"
                    )


                    adjustment_plot_df = (
                        trade_view[
                            [
                                "Year",
                                "ExportAdjustment",
                            ]
                        ].copy()
                    )


                    def _adjustment_direction(
                        value
                    ):

                        if pd.isna(
                            value
                        ):
                            return "Unavailable"

                        if value > 0:
                            return "Increase"

                        if value < 0:
                            return "Decrease"

                        return "No change"


                    adjustment_plot_df[
                        "Direction"
                    ] = (
                        adjustment_plot_df[
                            "ExportAdjustment"
                        ]
                        .apply(
                            _adjustment_direction
                        )
                    )


                    adjustment_fig = (
                        px.bar(
                            adjustment_plot_df,
                            x="Year",
                            y="ExportAdjustment",
                            color="Direction",
                            color_discrete_map={
                                "Increase": "#2ca02c",
                                "Decrease": "#d62728",
                                "No change": "#7f7f7f",
                                "Unavailable": "#f0ad4e",
                            },
                            labels={
                                "ExportAdjustment": (
                                    "Adjusted exports - "
                                    "original exports"
                                ),
                            },
                        )
                    )


                    adjustment_fig.update_layout(
                        legend_title_text=(
                            "Direction"
                        )
                    )


                    st.plotly_chart(
                        adjustment_fig,
                        use_container_width=True,
                        key=(
                            "analytics_adjustment_year"
                        ),
                    )


                with chart_right:

                    st.markdown(
                        "### Import-export gap"
                    )


                    gap_long = (
                        trade_view[
                            [
                                "Year",
                                "TradeGapBefore",
                                "TradeGapAfter",
                            ]
                        ]
                        .melt(
                            id_vars="Year",
                            var_name="Stage",
                            value_name=(
                                "Absolute gap"
                            ),
                        )
                    )


                    gap_long[
                        "Stage"
                    ] = (
                        gap_long[
                            "Stage"
                        ]
                        .map(
                            {
                                "TradeGapBefore": (
                                    "Before adjustment"
                                ),
                                "TradeGapAfter": (
                                    "After adjustment"
                                ),
                            }
                        )
                    )


                    gap_fig = px.bar(
                        gap_long,
                        x="Year",
                        y="Absolute gap",
                        color="Stage",
                        barmode="group",
                        labels={
                            "Absolute gap": (
                                "Absolute "
                                "import-export gap"
                            ),
                        },
                    )


                    gap_fig.update_layout(
                        title=(
                            "Submitted-participant "
                            "aggregate"
                        )
                    )


                    st.plotly_chart(
                        gap_fig,
                        use_container_width=True,
                        key="analytics_gap",
                    )


                # =================================================
                # 4. TOP PRODUCTS
                # =================================================

                st.markdown(
                    "### Top products affected"
                )


                impact_count = max(
                    5,
                    len(
                        product_impact
                    ),
                )


                top_n = st.slider(
                    "Number of products to show",
                    min_value=5,
                    max_value=min(
                        30,
                        impact_count,
                    ),
                    value=min(
                        15,
                        impact_count,
                    ),
                    key="analytics_top_n",
                )


                top_products = (
                    product_impact
                    .head(
                        top_n
                    )
                    .sort_values(
                        "AbsoluteExportAdjustment",
                        ascending=True,
                        na_position="first",
                    )
                )


                top_fig = px.bar(
                    top_products,
                    x="AbsoluteExportAdjustment",
                    y="ProductDisplay",
                    orientation="h",
                    labels={
                        "AbsoluteExportAdjustment": (
                            "Absolute export adjustment"
                        ),
                        "ProductDisplay": (
                            "Product"
                        ),
                    },
                )


                st.plotly_chart(
                    top_fig,
                    use_container_width=True,
                    key=(
                        "analytics_top_products"
                    ),
                )


                # =================================================
                # 5. HEATMAP
                # =================================================

                st.markdown(
                    "### Product × Year adjustment heatmap"
                )


                heatmap_product_keys = set(
                    product_impact
                    .head(
                        top_n
                    )[
                        "Product"
                    ]
                    .astype(str)
                )


                heatmap_df = (
                    pathway_df[
                        pathway_df[
                            "Product"
                        ]
                        .astype(str)
                        .isin(
                            heatmap_product_keys
                        )
                    ]
                    .copy()
                )


                heatmap_pivot = (
                    heatmap_df
                    .pivot_table(
                        index="ProductDisplay",
                        columns="Year",
                        values=(
                            "ExportAdjustment"
                        ),
                        aggfunc=(
                            lambda values:
                            values.sum(
                                min_count=1
                            )
                        ),
                    )
                )


                if not heatmap_pivot.empty:

                    heatmap_fig = (
                        go.Figure(
                            data=go.Heatmap(
                                z=(
                                    heatmap_pivot
                                    .values
                                ),
                                x=[
                                    int(
                                        value
                                    )
                                    for value
                                    in heatmap_pivot.columns
                                ],
                                y=list(
                                    heatmap_pivot.index
                                ),
                                colorscale="RdBu",
                                zmid=0,
                                colorbar={
                                    "title": (
                                        "Export adjustment"
                                    )
                                },
                                hovertemplate=(
                                    "Product: %{y}<br>"
                                    "Year: %{x}<br>"
                                    "Adjustment: "
                                    "%{z:,.2f}"
                                    "<extra></extra>"
                                ),
                            )
                        )
                    )


                    heatmap_fig.update_layout(
                        xaxis_title="Year",
                        yaxis_title="Product",
                        height=max(
                            450,
                            (
                                24
                                * len(
                                    heatmap_pivot.index
                                )
                                + 180
                            ),
                        ),
                    )


                    st.plotly_chart(
                        heatmap_fig,
                        use_container_width=True,
                        key="analytics_heatmap",
                    )


                else:

                    st.info(
                        "No product-year adjustment "
                        "values are available for "
                        "the selected scope."
                    )


                # =================================================
                # 6. PATHWAY COMPARISON
                # =================================================

                st.divider()

                st.markdown(
                    "## Pathway comparison"
                )


                st.caption(
                    "Compare CurrentTrends, "
                    "GlobalSustainability and "
                    "NationalCommitments within the "
                    "selected iteration. The Product "
                    "filter above also applies here."
                )


                pathway_metric_label = (
                    st.selectbox(
                        "Compare pathways by",
                        [
                            "Adjusted exports",
                            "Exports before adjustment",
                            "Export adjustment",
                            (
                                "Import-export gap "
                                "after adjustment"
                            ),
                            "Gap reduction",
                        ],
                        key=(
                            "analytics_pathway_metric"
                        ),
                    )
                )


                pathway_metric_column = {
                    "Adjusted exports": (
                        "ExportsAdj"
                    ),
                    "Exports before adjustment": (
                        "ExportsOriginal"
                    ),
                    "Export adjustment": (
                        "ExportAdjustment"
                    ),
                    (
                        "Import-export gap "
                        "after adjustment"
                    ): (
                        "TradeGapAfter"
                    ),
                    "Gap reduction": (
                        "GapReduction"
                    ),
                }[
                    pathway_metric_label
                ]


                if pathway_compare.empty:

                    st.info(
                        "No pathway-comparison data "
                        "is available for the selected "
                        "product scope."
                    )


                else:

                    pathway_line_fig = (
                        px.line(
                            pathway_compare,
                            x="Year",
                            y=(
                                pathway_metric_column
                            ),
                            color="Pathway",
                            markers=True,
                            color_discrete_map={
                                "CurrentTrends": (
                                    "#1f77b4"
                                ),
                                "GlobalSustainability": (
                                    "#2ca02c"
                                ),
                                "NationalCommitments": (
                                    "#9467bd"
                                ),
                            },
                            labels={
                                pathway_metric_column: (
                                    pathway_metric_label
                                )
                            },
                        )
                    )


                    pathway_line_fig.update_layout(
                        title=(
                            f"{pathway_metric_label} "
                            "across pathways | "
                            f"{selected_analytics_iteration} "
                            f"| {scope_label}"
                        ),
                        xaxis_title="Year",
                        yaxis_title=(
                            pathway_metric_label
                        ),
                        hovermode="x unified",
                        legend_title_text=(
                            "Pathway"
                        ),
                    )


                    if (
                        pathway_compare[
                            "Year"
                        ].min()
                        <= 2020
                    ):

                        pathway_line_fig.add_vrect(
                            x0=float(
                                pathway_compare[
                                    "Year"
                                ].min()
                            ),
                            x1=2020,
                            opacity=0.08,
                            line_width=0,
                            annotation_text=(
                                "Historical - unchanged"
                            ),
                            annotation_position=(
                                "top left"
                            ),
                        )


                    st.plotly_chart(
                        pathway_line_fig,
                        use_container_width=True,
                        key=(
                            "analytics_pathway_line"
                        ),
                    )


                    # -----------------------------------------
                    # Selected year pathway comparison
                    # -----------------------------------------

                    available_years = sorted(
                        pathway_compare[
                            "Year"
                        ]
                        .dropna()
                        .astype(int)
                        .unique()
                    )


                    selected_compare_year = (
                        st.selectbox(
                            (
                                "Year for pathway "
                                "before-vs-after comparison"
                            ),
                            available_years,
                            index=(
                                len(
                                    available_years
                                )
                                - 1
                            ),
                            key=(
                                "analytics_pathway_year"
                            ),
                        )
                    )


                    pathway_year = (
                        pathway_compare[
                            (
                                pathway_compare[
                                    "Year"
                                ].astype(int)
                                == int(
                                    selected_compare_year
                                )
                            )
                        ][
                            [
                                "Pathway",
                                "ExportsOriginal",
                                "ExportsAdj",
                            ]
                        ]
                        .copy()
                    )


                    pathway_year = (
                        pathway_year
                        .melt(
                            id_vars="Pathway",
                            var_name="Stage",
                            value_name="Exports",
                        )
                    )


                    pathway_year[
                        "Stage"
                    ] = (
                        pathway_year[
                            "Stage"
                        ]
                        .map(
                            {
                                "ExportsOriginal": (
                                    "Before adjustment"
                                ),
                                "ExportsAdj": (
                                    "After adjustment"
                                ),
                            }
                        )
                    )


                    pathway_bar_fig = (
                        px.bar(
                            pathway_year,
                            x="Pathway",
                            y="Exports",
                            color="Stage",
                            barmode="group",
                            labels={
                                "Exports": (
                                    "Export quantity"
                                )
                            },
                        )
                    )


                    pathway_bar_fig.update_layout(
                        title=(
                            "Exports before vs after "
                            "by pathway | "
                            f"{selected_compare_year} "
                            f"| {scope_label}"
                        ),
                        xaxis_title="Pathway",
                        yaxis_title=(
                            "Submitted-participant "
                            "export quantity"
                        ),
                        legend_title_text="Stage",
                    )


                    st.plotly_chart(
                        pathway_bar_fig,
                        use_container_width=True,
                        key=(
                            "analytics_pathway_bar"
                        ),
                    )


                # =================================================
                # 7. ITERATION COMPARISON
                # =================================================

                st.divider()

                st.markdown(
                    "## Comparison across iterations"
                )


                metric_label = (
                    st.selectbox(
                        "Iteration comparison metric",
                        [
                            "Adjusted exports",
                            "Original exports",
                            (
                                "Absolute import-export "
                                "gap after"
                            ),
                            "Export adjustment",
                        ],
                        key=(
                            "analytics_compare_metric"
                        ),
                    )
                )


                metric_column = {
                    "Adjusted exports": (
                        "ExportsAdj"
                    ),
                    "Original exports": (
                        "ExportsOriginal"
                    ),
                    (
                        "Absolute import-export "
                        "gap after"
                    ): (
                        "TradeGapAfter"
                    ),
                    "Export adjustment": (
                        "ExportAdjustment"
                    ),
                }[
                    metric_label
                ]


                if all_analytics.empty:

                    st.info(
                        "No published iterations "
                        "are available for comparison yet."
                    )


                else:

                    comparison = (
                        all_analytics[
                            (
                                all_analytics[
                                    "Pathway"
                                ].astype(str)
                                == selected_analytics_pathway
                            )
                        ]
                        .copy()
                    )


                    if (
                        selected_analytics_product
                        != "All products"
                    ):

                        comparison = (
                            comparison[
                                (
                                    comparison[
                                        "ProductDisplay"
                                    ].astype(str)
                                    == selected_analytics_product
                                )
                            ]
                            .copy()
                        )


                    comparison = (
                        comparison
                        .groupby(
                            [
                                "Iteration",
                                "Year",
                            ],
                            as_index=False,
                        )[
                            metric_column
                        ]
                        .sum(
                            min_count=1
                        )
                    )


                    if comparison.empty:

                        st.info(
                            "No comparable published data "
                            "exists for the selected filters."
                        )


                    else:

                        comparison_fig = (
                            px.line(
                                comparison,
                                x="Year",
                                y=metric_column,
                                color="Iteration",
                                markers=True,
                                labels={
                                    metric_column: (
                                        metric_label
                                    )
                                },
                            )
                        )


                        comparison_fig.update_layout(
                            hovermode="x unified",
                            legend_title_text=(
                                "Iteration"
                            ),
                            title=(
                                f"{selected_analytics_pathway} "
                                f"| {scope_label}"
                            ),
                        )


                        st.plotly_chart(
                            comparison_fig,
                            use_container_width=True,
                            key=(
                                "analytics_iteration_compare"
                            ),
                        )


                # =================================================
                # FOOTNOTE
                # =================================================

                st.caption(
                    "Historical years through 2020 are expected "
                    "to remain unchanged under the validated "
                    "trade adjustment method. Charts aggregate "
                    "the participants included in the selected "
                    "published run; they are not labelled global "
                    "unless the configured coverage itself "
                    "represents the full intended global set."
                )


# ============================================================
# 16. ADMIN
# ============================================================

with admin_tab:

    st.subheader(
        "Admin"
    )


    # ========================================================
    # ADMIN AUTHENTICATION STATE
    # ========================================================

    if (
        "fable_admin_authenticated"
        not in st.session_state
    ):

        st.session_state[
            "fable_admin_authenticated"
        ] = False


    # ========================================================
    # LOGIN VIEW
    # ========================================================

    if not st.session_state[
        "fable_admin_authenticated"
    ]:

        st.caption(
            "Enter the admin password to access "
            "iteration configuration, submission "
            "progress and storage controls."
        )


        with st.form(
            "admin_login_form",
            clear_on_submit=False,
        ):

            admin_password = (
                st.text_input(
                    "Admin password",
                    type="password",
                    placeholder=(
                        "Enter admin password"
                    ),
                )
            )


            login_clicked = (
                st.form_submit_button(
                    "Log in",
                    type="primary",
                    use_container_width=False,
                )
            )


        if login_clicked:

            if not admin_password:

                st.error(
                    "Please enter the admin password."
                )


            else:

                with st.status(
                    "Checking admin credentials...",
                    expanded=True,
                ) as login_status:

                    login_status.write(
                        "Validating password..."
                    )


                    if (
                        admin_password
                        != ADMIN_PASSWORD
                    ):

                        login_status.update(
                            label="Login failed",
                            state="error",
                            expanded=False,
                        )


                        st.error(
                            "Incorrect admin password. "
                            "Please try again."
                        )


                    else:

                        st.session_state[
                            "fable_admin_authenticated"
                        ] = True


                        login_status.update(
                            label=(
                                "Admin access enabled"
                            ),
                            state="complete",
                            expanded=False,
                        )


                        st.success(
                            "Admin access enabled."
                        )


                        st.rerun()


    # ========================================================
    # AUTHENTICATED ADMIN AREA
    # ========================================================

    else:

        admin_status_col, logout_col = (
            st.columns(
                [
                    5,
                    1,
                ]
            )
        )


        with admin_status_col:

            st.success(
                "Admin access enabled."
            )


        with logout_col:

            if st.button(
                "Log out",
                key="admin_logout_button",
                use_container_width=True,
            ):

                st.session_state[
                    "fable_admin_authenticated"
                ] = False

                st.rerun()


        (
            config_tab,
            progress_tab,
            storage_tab,
        ) = st.tabs(
            [
                "Configure Iteration",
                "Submission Progress",
                "Storage",
            ]
        )


        # =================================================
        # 15A. CONFIGURE ITERATION
        # =================================================

        with config_tab:

            st.markdown(
                "### Create or update an iteration"
            )

            configs = (
                list_iteration_configs()
            )

            existing_names = [
                cfg["iteration"]
                for cfg in configs
            ]

            mode = st.radio(
                "Configuration",
                [
                    "Create new iteration",
                    "Edit existing iteration",
                ],
                horizontal=True,
            )


            if (
                mode
                == "Edit existing iteration"
                and existing_names
            ):

                editing_name = (
                    st.selectbox(
                        "Iteration",
                        existing_names,
                        key="edit_iteration",
                    )
                )

                editing_config = (
                    load_iteration_config(
                        editing_name
                    )
                )

                participant_default = (
                    "\n".join(
                        editing_config.get(
                            "participants",
                            [],
                        )
                    )
                )

                pathway_default = (
                    editing_config.get(
                        "pathways",
                        DEFAULT_PATHWAYS,
                    )
                )

                open_default = (
                    editing_config.get(
                        "status"
                    )
                    == "OPEN"
                )

            else:

                editing_name = ""
                participant_default = ""
                pathway_default = (
                    DEFAULT_PATHWAYS
                )
                open_default = True


            with st.form(
                "iteration_config_form"
            ):

                iteration_name = (
                    st.text_input(
                        "Iteration name",
                        value=editing_name,
                        placeholder=(
                            "e.g. Scenathon 2026 "
                            "- Iteration 1"
                        ),
                    )
                )

                participant_text = (
                    st.text_area(
                        "Expected participants / countries",
                        value=(
                            participant_default
                        ),
                        height=240,
                        help=(
                            "Enter one participant "
                            "or country per line."
                        ),
                    )
                )

                pathway_text = (
                    st.text_area(
                        "Required pathways",
                        value=(
                            "\n".join(
                                pathway_default
                            )
                        ),
                        height=150,
                        help=(
                            "Enter one pathway per line. "
                            "The iteration configuration "
                            "defines which pathways exist."
                        ),
                    )
                )

                is_open = st.checkbox(
                    "Open for submissions",
                    value=open_default,
                )

                save_config = (
                    st.form_submit_button(
                        "Save iteration",
                        type="primary",
                    )
                )


            if save_config:

                participants = (
                    normalize_participants(
                        participant_text
                    )
                )

                selected_pathways = (
                    normalize_participants(
                        pathway_text
                    )
                )

                if not iteration_name.strip():

                    st.error(
                        "Enter an iteration name."
                    )

                elif not participants:

                    st.error(
                        "Add at least one participant."
                    )

                elif not selected_pathways:

                    st.error(
                        "Add at least one pathway."
                    )

                else:

                    save_iteration_config(
                        iteration_name=(
                            iteration_name
                        ),
                        participants=participants,
                        pathways=(
                            selected_pathways
                        ),
                        is_open=is_open,
                    )

                    expected_reports = (
                        len(participants)
                        * len(
                            selected_pathways
                        )
                    )

                    st.success(
                        "Iteration saved successfully."
                    )

                    c1, c2, c3 = (
                        st.columns(3)
                    )

                    c1.metric(
                        "Participants",
                        len(participants),
                    )

                    c2.metric(
                        "Pathways",
                        len(
                            selected_pathways
                        ),
                    )

                    c3.metric(
                        "Expected reports",
                        expected_reports,
                    )


        # =================================================
        # 15B. SUBMISSION PROGRESS
        # =================================================

        with progress_tab:

            st.markdown(
                "### Submission progress"
            )

            configs = (
                list_iteration_configs()
            )

            names = [
                cfg["iteration"]
                for cfg in configs
            ]

            if not names:

                st.info(
                    "Create an iteration first."
                )

            else:

                progress_iteration = (
                    st.selectbox(
                        "Iteration",
                        names,
                        key="progress_iteration",
                    )
                )

                config = (
                    load_iteration_config(
                        progress_iteration
                    )
                )

                participants = (
                    config.get(
                        "participants",
                        [],
                    )
                )

                pathways = (
                    config.get(
                        "pathways",
                        [],
                    )
                )

                submissions = (
                    submission_metadata_df(
                        progress_iteration,
                        stage="before",
                    )
                )

                after_submissions = (
                    submission_metadata_df(
                        progress_iteration,
                        stage="after",
                    )
                )

                expected = (
                    build_expected_matrix(
                        participants,
                        pathways,
                    )
                )

                expected_reports = (
                    len(expected)
                )

                submitted_reports = (
                    len(submissions)
                )

                participant_total = (
                    len(participants)
                )

                if submissions.empty:

                    submitted_participants = 0

                else:

                    submitted_participants = (
                        submissions[
                            "participant"
                        ].nunique()
                    )


                participant_completion = (
                    submitted_participants
                    / participant_total
                    if participant_total
                    else 0
                )

                report_completion = (
                    submitted_reports
                    / expected_reports
                    if expected_reports
                    else 0
                )


                c1, c2, c3, c4 = (
                    st.columns(4)
                )

                c1.metric(
                    "Participants submitted",
                    (
                        f"{submitted_participants}"
                        f" / {participant_total}"
                    ),
                )

                c2.metric(
                    "Reports received",
                    (
                        f"{submitted_reports}"
                        f" / {expected_reports}"
                    ),
                )

                c3.metric(
                    "Participant completion",
                    (
                        f"{participant_completion * 100:.0f}%"
                    ),
                )

                c4.metric(
                    "Report completion",
                    (
                        f"{report_completion * 100:.0f}%"
                    ),
                )


                st.markdown(
                    "#### Overall report progress"
                )

                st.progress(
                    min(
                        report_completion,
                        1.0,
                    )
                )


                # -----------------------------------------
                # BEFORE / AFTER WORKFLOW STATUS
                # -----------------------------------------

                st.markdown(
                    "#### Before and After workflow"
                )


                def admin_submission_lookup(
                    frame,
                ):

                    if frame.empty:

                        return {}

                    return {
                        (
                            str(
                                row.get(
                                    "participant",
                                    "",
                                )
                            ).strip(),
                            str(
                                row.get(
                                    "pathway",
                                    "",
                                )
                            ).strip(),
                        ): row
                        for row
                        in frame.to_dict(
                            orient="records"
                        )
                    }


                before_lookup = (
                    admin_submission_lookup(
                        submissions
                    )
                )

                after_lookup = (
                    admin_submission_lookup(
                        after_submissions
                    )
                )


                workflow_rows = []


                for _, expected_row in (
                    expected.iterrows()
                ):

                    participant_name = str(
                        expected_row[
                            "participant"
                        ]
                    ).strip()

                    pathway_name = str(
                        expected_row[
                            "pathway"
                        ]
                    ).strip()

                    key = (
                        participant_name,
                        pathway_name,
                    )

                    before_row = (
                        before_lookup.get(
                            key
                        )
                    )

                    after_row = (
                        after_lookup.get(
                            key
                        )
                    )

                    before_ok = (
                        before_row
                        is not None
                    )

                    after_ok = (
                        after_row
                        is not None
                    )


                    if (
                        before_ok
                        and after_ok
                    ):

                        workflow_status = (
                            "Before and After complete"
                        )

                    elif before_ok:

                        workflow_status = (
                            "Before only"
                        )

                    elif after_ok:

                        workflow_status = (
                            "After without Before"
                        )

                    else:

                        workflow_status = (
                            "Not submitted"
                        )


                    workflow_rows.append(
                        {
                            "Participant": (
                                participant_name
                            ),
                            "Pathway": (
                                pathway_name
                            ),
                            "Before": (
                                "Submitted"
                                if before_ok
                                else "Missing"
                            ),
                            "After": (
                                "Submitted"
                                if after_ok
                                else "Missing"
                            ),
                            "Workflow status": (
                                workflow_status
                            ),
                            "Before submitted": (
                                before_row.get(
                                    "submitted_at_utc",
                                    "",
                                )
                                if before_ok
                                else ""
                            ),
                            "After submitted": (
                                after_row.get(
                                    "submitted_at_utc",
                                    "",
                                )
                                if after_ok
                                else ""
                            ),
                        }
                    )


                workflow_df = (
                    pd.DataFrame(
                        workflow_rows
                    )
                )


                after_reports = (
                    len(
                        after_submissions
                    )
                )


                complete_pairs = (
                    int(
                        (
                            (
                                workflow_df[
                                    "Before"
                                ]
                                == "Submitted"
                            )
                            &
                            (
                                workflow_df[
                                    "After"
                                ]
                                == "Submitted"
                            )
                        ).sum()
                    )
                    if not workflow_df.empty
                    else 0
                )


                workflow_metric_1, workflow_metric_2, workflow_metric_3 = (
                    st.columns(
                        3
                    )
                )


                workflow_metric_1.metric(
                    "Before reports",
                    (
                        f"{submitted_reports}"
                        f" / {expected_reports}"
                    ),
                )


                workflow_metric_2.metric(
                    "After reports",
                    (
                        f"{after_reports}"
                        f" / {expected_reports}"
                    ),
                )


                workflow_metric_3.metric(
                    "Before and After complete",
                    (
                        f"{complete_pairs}"
                        f" / {expected_reports}"
                    ),
                )


                progress_before_col, progress_after_col = (
                    st.columns(
                        2
                    )
                )


                with progress_before_col:

                    st.caption(
                        "Before trade adjustment"
                    )

                    st.progress(
                        min(
                            (
                                submitted_reports
                                / expected_reports
                            )
                            if expected_reports
                            else 0,
                            1.0,
                        )
                    )


                with progress_after_col:

                    st.caption(
                        "After trade adjustment"
                    )

                    st.progress(
                        min(
                            (
                                after_reports
                                / expected_reports
                            )
                            if expected_reports
                            else 0,
                            1.0,
                        )
                    )


                if not workflow_df.empty:

                    st.dataframe(
                        workflow_df,
                        use_container_width=True,
                        hide_index=True,
                    )


                    after_without_before = (
                        workflow_df[
                            workflow_df[
                                "Workflow status"
                            ]
                            == "After without Before"
                        ]
                    )


                    if not after_without_before.empty:

                        st.warning(
                            "An After submission exists without "
                            "a matching Before submission for the "
                            "same iteration, participant and pathway."
                        )


                pathway_stage_rows = []


                for pathway_name in pathways:

                    before_count = (
                        int(
                            (
                                submissions[
                                    "pathway"
                                ]
                                == pathway_name
                            ).sum()
                        )
                        if not submissions.empty
                        else 0
                    )

                    after_count = (
                        int(
                            (
                                after_submissions[
                                    "pathway"
                                ]
                                == pathway_name
                            ).sum()
                        )
                        if not after_submissions.empty
                        else 0
                    )


                    pathway_stage_rows.extend(
                        [
                            {
                                "Pathway": (
                                    plain_text_label(
                                        pathway_name
                                    )
                                ),
                                "Trade stage": (
                                    "Before trade adjustment"
                                ),
                                "Submitted": (
                                    before_count
                                ),
                            },
                            {
                                "Pathway": (
                                    plain_text_label(
                                        pathway_name
                                    )
                                ),
                                "Trade stage": (
                                    "After trade adjustment"
                                ),
                                "Submitted": (
                                    after_count
                                ),
                            },
                        ]
                    )


                pathway_stage_df = (
                    pd.DataFrame(
                        pathway_stage_rows
                    )
                )


                if not pathway_stage_df.empty:

                    pathway_stage_fig = (
                        px.bar(
                            pathway_stage_df,
                            x="Pathway",
                            y="Submitted",
                            color="Trade stage",
                            barmode="group",
                            title=(
                                "Before and After submissions by pathway"
                            ),
                            labels={
                                "Submitted": (
                                    "Reports submitted"
                                ),
                                "Trade stage": (
                                    ""
                                ),
                            },
                        )
                    )


                    pathway_stage_fig.update_layout(
                        template="plotly_white",
                        height=360,
                        margin=dict(
                            l=10,
                            r=10,
                            t=55,
                            b=70,
                        ),
                        legend_title_text="",
                    )


                    st.plotly_chart(
                        pathway_stage_fig,
                        use_container_width=True,
                        key=(
                            "admin_before_after_pathway_chart"
                        ),
                    )


                # -----------------------------------------
                # PATHWAY VISUALIZATION
                # -----------------------------------------

                st.markdown(
                    "#### Reports by pathway"
                )

                pathway_rows = []

                for pathway in pathways:

                    if submissions.empty:
                        count = 0
                    else:
                        count = len(
                            submissions[
                                submissions[
                                    "pathway"
                                ]
                                == pathway
                            ]
                        )

                    pathway_rows.append(
                        {
                            "Pathway": pathway,
                            "Submitted": count,
                            "Expected": (
                                participant_total
                            ),
                        }
                    )


                pathway_df = (
                    pd.DataFrame(
                        pathway_rows
                    )
                )

                if not pathway_df.empty:

                    st.bar_chart(
                        pathway_df.set_index(
                            "Pathway"
                        )[
                            [
                                "Submitted",
                                "Expected",
                            ]
                        ]
                    )


                # -----------------------------------------
                # MISSING SUBMISSIONS
                # -----------------------------------------

                st.markdown(
                    "#### Missing submissions"
                )

                if submissions.empty:

                    missing_df = (
                        expected.copy()
                    )

                else:

                    submitted_keys = (
                        submissions[
                            [
                                "participant",
                                "pathway",
                            ]
                        ]
                        .drop_duplicates()
                    )

                    merged = (
                        expected.merge(
                            submitted_keys,
                            on=[
                                "participant",
                                "pathway",
                            ],
                            how="left",
                            indicator=True,
                        )
                    )

                    missing_df = (
                        merged[
                            merged["_merge"]
                            == "left_only"
                        ][
                            [
                                "participant",
                                "pathway",
                            ]
                        ]
                    )


                if missing_df.empty:

                    st.success(
                        "All expected reports "
                        "have been submitted."
                    )

                else:

                    st.dataframe(
                        missing_df,
                        use_container_width=True,
                        hide_index=True,
                    )


                # -----------------------------------------
                # SUBMISSION DETAILS
                # -----------------------------------------

                st.markdown(
                    "#### Submission details"
                )

                if submissions.empty:

                    st.info(
                        "No submissions yet."
                    )

                else:

                    st.dataframe(
                        submissions[
                            [
                                "participant",
                                "pathway",
                                "username",
                                "submitted_at_utc",
                                "row_count",
                            ]
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )


                # -----------------------------------------
                # TRADE ADJUSTMENT
                # -----------------------------------------

                st.divider()

                st.markdown(
                    "### Trade adjustment"
                )


                missing_count = len(
                    missing_df
                )

                submitted_count = (
                    submitted_reports
                )


                # =========================================
                # COMPLETENESS INFORMATION
                # =========================================

                if missing_count == 0:

                    st.success(
                        "All expected reports have "
                        "been submitted."
                    )

                else:

                    st.warning(
                        f"{missing_count} expected "
                        f"report(s) are still missing. "
                        "The administrator can run "
                        "a provisional adjustment "
                        "using the reports currently "
                        "available."
                    )


                m1, m2 = (
                    st.columns(2)
                )

                m1.metric(
                    "Reports available",
                    submitted_count,
                )

                m2.metric(
                    "Reports missing",
                    missing_count,
                )


                # =========================================
                # RUN BUTTON
                # =========================================

                if submitted_count == 0:

                    st.button(
                        "Run Trade Adjustment",
                        disabled=True,
                        type="primary",
                    )

                    st.info(
                        "At least one report "
                        "must be submitted first."
                    )

                else:

                    button_label = (
                        "Run Trade Adjustment"
                        if missing_count == 0
                        else
                        "Run Trade Adjustment Anyway"
                    )


                    run_trade = st.button(
                        button_label,
                        type="primary",
                        key="run_trade_adjustment",
                    )


                    if run_trade:

                        try:

                            with st.spinner(
                                "Running trade adjustment..."
                            ):

                                run_result = (
                                    run_iteration_trade_adjustment(
                                        progress_iteration
                                    )
                                )


                            if run_result.get("status") == "FINAL":

                                st.success(
                                    "FINAL trade adjustment completed. "
                                    "Participant result files and the "
                                    "analytics dataset were published."
                                )

                            else:

                                st.warning(
                                    "PROVISIONAL trade adjustment completed. "
                                    "Participant result files and the analytics "
                                    "dataset were published, but QA blockers "
                                    "and/or incomplete coverage require review."
                                )


                            r1, r2, r3, r4 = (
                                st.columns(4)
                            )


                            r1.metric(
                                "Engine version",
                                run_result[
                                    "engine_version"
                                ],
                            )


                            r2.metric(
                                "Input rows",
                                run_result[
                                    "input_rows"
                                ],
                            )


                            r3.metric(
                                "Adjusted rows",
                                run_result[
                                    "adjusted_rows"
                                ],
                            )


                            r4.metric(
                                "Result files",
                                run_result[
                                    "participants"
                                ],
                            )


                            # -----------------------------
                            # BLOCKERS
                            # -----------------------------

                            blockers = (
                                run_result[
                                    "blockers"
                                ]
                            )


                            if blockers:

                                st.warning(
                                    f"Engine completed with "
                                    f"{len(blockers)} QA "
                                    "blocker(s). Blocker-affected "
                                    "product/pathway/year cases can "
                                    "have blank ExportsAdj values; "
                                    "treat the published run and its "
                                    "analytics as provisional."
                                )


                                blocker_df = (
                                    pd.DataFrame(
                                        blockers
                                    )
                                )


                                st.dataframe(
                                    blocker_df,
                                    use_container_width=True,
                                    hide_index=True,
                                )


                            else:

                                st.success(
                                    "Engine QA: "
                                    "0 blockers."
                                )


                            # -----------------------------
                            # PUBLISHED FILES
                            # -----------------------------

                            st.markdown(
                                "#### Published participant results"
                            )


                            published_df = (
                                pd.DataFrame(
                                    run_result[
                                        "published_files"
                                    ]
                                )
                            )


                            if not published_df.empty:

                                st.dataframe(
                                    published_df[
                                        [
                                            "participant",
                                            "rows",
                                            "path",
                                        ]
                                    ],
                                    use_container_width=True,
                                    hide_index=True,
                                )


                            st.info(
                                "Users can now open "
                                "'Download Result', select "
                                "their iteration and country, "
                                "and download the copy-ready "
                                "FullDataTradeAdj CSV."
                            )


                        except Exception as exc:

                            st.error(
                                "Trade adjustment failed: "
                                f"{exc}"
                            )


        # =================================================
        # 15C. STORAGE
        # =================================================

        with storage_tab:

            st.markdown(
                "### GitHub storage"
            )

            st.write(
                "**Repository:** "
                f"{GITHUB_OWNER}/{GITHUB_REPO}"
            )

            st.write(
                "**Branch:** "
                f"{GITHUB_BRANCH}"
            )

            st.write(
                "**Storage folder:** "
                f"`{STORAGE_ROOT}/`"
            )

            ok, message = (
                github_connection_status()
            )

            if ok:

                st.success(
                    "GitHub storage connection "
                    "is working."
                )

            else:

                st.error(
                    message
                )

            st.caption(
                "Only extracted SCENATHON trade "
                "data and metadata are stored. "
                "The original uploaded workbook "
                "is not saved."
            )

            st.warning(
                "Deleting data removes it from "
                "the current repository state, "
                "but older versions may remain "
                "in Git history."
            )

            st.divider()

            st.markdown(
                "### Clear iteration data"
            )

            configs = (
                list_iteration_configs()
            )

            clear_names = [
                cfg["iteration"]
                for cfg in configs
            ]

            if clear_names:

                clear_iteration = (
                    st.selectbox(
                        "Iteration",
                        clear_names,
                        key="clear_iteration",
                    )
                )

                confirm_clear = (
                    st.checkbox(
                        "I understand that this "
                        "will delete the current "
                        "submission and result files "
                        "for this iteration."
                    )
                )

                if st.button(
                    "Clear submissions and results",
                    disabled=(
                        not confirm_clear
                    ),
                ):

                    deleted_submissions = (
                        github_delete_tree(
                            submission_folder(
                                clear_iteration
                            )
                        )
                    )

                    deleted_results = (
                        github_delete_tree(
                            result_folder(
                                clear_iteration
                            )
                        )
                    )

                    deleted_total = (
                        deleted_submissions
                        + deleted_results
                    )

                    st.success(
                        "Current iteration data "
                        f"cleared. Deleted files: "
                        f"{deleted_total}"
                    )

                    st.rerun()

            else:

                st.info(
                    "No iterations exist yet."
                )

