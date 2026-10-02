
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

LOGO_PATH = (
    REPO_ROOT
    / "addin"
    / "assets"
    / "fable-logo-full.png"
)

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



def read_uploaded_report(uploaded_file):

    filename = uploaded_file.name.lower()


    # ========================================================
    # CSV
    # ========================================================

    if filename.endswith(".csv"):

        raw_bytes = uploaded_file.getvalue()


        # Excel UTF-8 CSV
        try:

            text = raw_bytes.decode(
                "utf-8-sig"
            )

        except UnicodeDecodeError:

            text = raw_bytes.decode(
                "cp1252",
                errors="replace",
            )


        # Auto-detect Excel delimiter
        try:

            dialect = csv.Sniffer().sniff(
                text[:20000],
                delimiters=",;	",
            )

            delimiter = dialect.delimiter

        except Exception:

            # French Excel normally uses semicolon
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
    # XLSX / XLSM
    # ========================================================

    if (
        filename.endswith(".xlsx")
        or filename.endswith(".xlsm")
    ):

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


        selected_sheet = None

        for sheet_name in candidate_names:

            if sheet_name in wb.sheetnames:

                selected_sheet = sheet_name
                break


        if selected_sheet is None:

            wb.close()

            raise ValueError(
                "SCENATHON_report sheet "
                "was not found in the workbook."
            )


        ws = wb[selected_sheet]


        rows = list(
            ws.iter_rows(
                values_only=True
            )
        )


        wb.close()


        return (
            _extract_reporting_byproduct_from_rows(
                rows
            )
        )


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

def submission_path(
    iteration: str,
    participant: str,
    pathway: str,
):

    filename = (
        f"{safe_name(participant)}"
        f"__{safe_name(pathway)}.json"
    )

    return (
        f"{submission_folder(iteration)}"
        f"/{filename}"
    )


def _rows_hash(rows) -> str:

    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def dataframe_report_hash(
    df: pd.DataFrame,
) -> str:

    clean_df = df[
        REQUIRED_COLUMNS
    ].copy()

    rows = json.loads(
        clean_df.to_json(
            orient="records"
        )
    )

    return _rows_hash(rows)


def get_submission_payload(
    iteration: str,
    participant: str,
    pathway: str,
):

    return github_get_json(
        submission_path(
            iteration,
            participant,
            pathway,
        )
    )


def iteration_has_published_results(
    iteration: str,
) -> bool:

    return bool(
        github_get_file(
            run_summary_path(iteration)
        )
        or github_get_file(
            analytics_path(iteration)
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
        stale_marker_path(iteration),
        {
            "iteration": iteration,
            "stale": True,
            "reason": reason,
            "marked_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
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
        stale_marker_path(iteration),
        (
            "Clear stale marker after trade "
            f"adjustment rerun: {iteration}"
        ),
    )


def load_stale_marker(
    iteration: str,
):

    return github_get_json(
        stale_marker_path(iteration)
    )


def save_submission(
    iteration: str,
    username: str,
    participant: str,
    pathway: str,
    df: pd.DataFrame,
    *,
    replace_existing: bool = False,
):

    clean_df = df[
        REQUIRED_COLUMNS
    ].copy()

    rows = json.loads(
        clean_df.to_json(
            orient="records"
        )
    )

    report_hash = _rows_hash(rows)

    path = submission_path(
        iteration,
        participant,
        pathway,
    )

    existing = github_get_json(path)

    if existing:

        old_hash = existing.get(
            "report_hash"
        )

        if not old_hash:
            old_hash = _rows_hash(
                existing.get("rows", [])
            )

        if old_hash == report_hash:
            return {
                "path": path,
                "status": "DUPLICATE",
                "replaced": False,
                "report_hash": report_hash,
            }

        if not replace_existing:
            raise FileExistsError(
                "A different report is already "
                "submitted for this iteration, "
                "participant and pathway."
            )

    payload = {
        "iteration": iteration,
        "username": username,
        "participant": participant,
        "pathway": pathway,
        "submitted_at_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "row_count": len(rows),
        "report_hash": report_hash,
        "rows": rows,
    }

    github_put_json(
        path,
        payload,
        (
            (
                "Replace SCENATHON report: "
                if existing
                else "Submit SCENATHON report: "
            )
            + f"{iteration} / "
            + f"{participant} / "
            + f"{pathway}"
        ),
    )

    mark_iteration_results_stale(
        iteration,
        reason=(
            "A submission was replaced."
            if existing
            else "A new submission was added after a run."
        ),
    )

    return {
        "path": path,
        "status": (
            "REPLACED"
            if existing
            else "SUBMITTED"
        ),
        "replaced": bool(existing),
        "report_hash": report_hash,
    }


def list_submission_payloads(
    iteration: str,
):

    items = github_list_dir(
        submission_folder(
            iteration
        )
    )

    submissions = []

    for item in items:

        if (
            item["type"] == "file"
            and item["name"]
            .lower()
            .endswith(".json")
        ):

            try:

                payload = github_get_json(
                    item["path"]
                )

                if payload:
                    submissions.append(
                        payload
                    )

            except Exception:
                pass

    return submissions


def submission_metadata_df(
    iteration: str,
):

    rows = []

    for payload in (
        list_submission_payloads(
            iteration
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
                        0,
                    )
                ),
            }
        )

    return pd.DataFrame(rows)


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

    submissions = (
        list_submission_payloads(
            iteration
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
                    # Actual Location from
                    # SCENATHON report
                    "Country": location,

                    # Used to map engine output
                    # back to the participant
                    "CountryID": participant,

                    "Pathway": pathway,

                    # Preserve native product
                    # name from submission
                    "Product": product,

                    "Year": row.get(
                        "Year"
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

                    "Iteration": iteration,

                    "RunID": None,
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
        "Submit SCENATHON report"
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
                st.columns(2)
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

                st.text_input(
                    "Iteration",
                    value=selected_iteration,
                    disabled=True,
                )


            uploaded_file = (
                st.file_uploader(
                    "Upload SCENATHON report",
                    type=[
                        "csv",
                        "xlsx",
                        "xlsm",
                    ],
                )
            )

            replace_existing = st.checkbox(
                "Replace the previous submission if "
                "this participant/pathway already has "
                "a different report."
            )

            submitted = (
                st.form_submit_button(
                    "Submit report",
                    type="primary",
                )
            )


        if submitted:

            if not username.strip():

                st.error(
                    "Please enter your name."
                )

            elif uploaded_file is None:

                st.error(
                    "Please upload a report."
                )

            else:

                try:

                    report_df = (
                        read_uploaded_report(
                            uploaded_file
                        )
                    )

                    missing_columns = (
                        validate_report(
                            report_df
                        )
                    )

                    if missing_columns:

                        st.error(
                            "Missing required columns: "
                            + ", ".join(
                                missing_columns
                            )
                        )

                    else:

                        clean_df = (
                            report_df[
                                REQUIRED_COLUMNS
                            ]
                            .copy()
                        )

                        save_result = (
                            save_submission(
                                iteration=(
                                    selected_iteration
                                ),
                                username=username,
                                participant=(
                                    participant
                                ),
                                pathway=pathway,
                                df=clean_df,
                                replace_existing=(
                                    replace_existing
                                ),
                            )
                        )

                        stored_path = (
                            save_result["path"]
                        )

                        if (
                            save_result["status"]
                            == "DUPLICATE"
                        ):
                            st.info(
                                "This exact report is already "
                                "submitted. Nothing was changed."
                            )
                        elif (
                            save_result["status"]
                            == "REPLACED"
                        ):
                            st.success(
                                "Previous submission replaced "
                                "successfully."
                            )
                            st.warning(
                                "Any previously published result "
                                "for this iteration is now stale. "
                                "The admin must rerun the trade "
                                "adjustment before users download "
                                "or analyse results."
                            )
                        else:
                            st.success(
                                "Report submitted successfully."
                            )

                        c1, c2 = (
                            st.columns(2)
                        )

                        c1.metric(
                            "Rows received",
                            len(clean_df),
                        )

                        c2.metric(
                            "Pathway",
                            pathway,
                        )

                        st.caption(
                            "Stored in private "
                            "GitHub storage:"
                        )

                        st.code(
                            stored_path
                        )

                        st.dataframe(
                            clean_df.head(20),
                            use_container_width=True,
                            hide_index=True,
                        )

                except FileExistsError:

                    st.warning(
                        "A different report already exists for "
                        "this participant and pathway. Tick "
                        "'Replace the previous submission' and "
                        "submit again only if you intend to "
                        "replace it."
                    )

                except Exception as exc:

                    st.error(
                        "Could not process "
                        f"the report: {exc}"
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
                "trade-adjustment run. The published "
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

    st.subheader("Trade Analytics")

    st.caption(
        "Interactive analysis of published trade-adjustment runs. "
        "This section is visible to all app users; admin access is "
        "not required. Clicking Run Analysis only builds charts from "
        "published results — it does not rerun the trade engine."
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
                        "Preparing product-impact "
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
                    # STEP 4 — PATHWAY COMPARISON DATA
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
                        "FINAL run — all configured reports "
                        "are present and the engine reported "
                        "0 QA blockers."
                    )


                elif complete:

                    st.warning(
                        f"PROVISIONAL run — all configured "
                        f"reports are present, but "
                        f"{blocker_count} QA blocker(s) "
                        "require review."
                    )


                else:

                    st.warning(
                        f"PROVISIONAL run — "
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
                            "Historical — unchanged"
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
                                "Historical — unchanged"
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
                    "trade-adjustment method. Charts aggregate "
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

    admin_password = (
        st.text_input(
            "Admin password",
            type="password",
        )
    )

    if admin_password:

        if (
            admin_password
            != ADMIN_PASSWORD
        ):

            st.error(
                "Incorrect admin password."
            )

        else:

            st.success(
                "Admin access enabled."
            )

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

                    selected_pathways = (
                        st.multiselect(
                            "Required pathways",
                            DEFAULT_PATHWAYS,
                            default=(
                                pathway_default
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
                            "Select at least one pathway."
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
                            progress_iteration
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


                # FABLE_FULL_APP_DATA_RESET_DANGER_ZONE_V1
                # =================================================
                # DANGER ZONE — RESET ALL RUNTIME APP DATA
                # =================================================

                st.divider()

                # -------------------------------------------------
                # CSS:
                # Only forms containing our special marker get
                # red destructive-action styling.
                # Other buttons in the application are untouched.
                # -------------------------------------------------

                st.markdown(
                    """
                    <style>

                    div[data-testid="stForm"]:has(
                        .fable-danger-zone-marker
                    ) {
                        border: 1px solid rgba(220, 38, 38, 0.70);
                        border-radius: 12px;
                        padding: 1rem;
                        background:
                            rgba(220, 38, 38, 0.045);
                    }

                    div[data-testid="stForm"]:has(
                        .fable-danger-zone-marker
                    )
                    button[kind="primary"] {
                        background-color:
                            #b91c1c !important;
                        border-color:
                            #b91c1c !important;
                        color:
                            white !important;
                    }

                    div[data-testid="stForm"]:has(
                        .fable-danger-zone-marker
                    )
                    button[kind="primary"]:hover {
                        background-color:
                            #991b1b !important;
                        border-color:
                            #991b1b !important;
                    }

                    .fable-danger-title {
                        color: #b91c1c;
                        font-weight: 800;
                        font-size: 1.12rem;
                        letter-spacing: 0.03em;
                        margin-bottom: 0.35rem;
                    }

                    .fable-danger-warning {
                        color: #b91c1c;
                        font-weight: 750;
                        line-height: 1.45;
                    }

                    </style>
                    """,
                    unsafe_allow_html=True,
                )


                # -------------------------------------------------
                # Put destructive controls at the lower-right side
                # of the Storage page.
                # -------------------------------------------------

                danger_spacer, danger_column = (
                    st.columns(
                        [1.55, 1.0]
                    )
                )


                with danger_column:

                    st.markdown(
                        """
                        <div class="fable-danger-title">
                            DANGER ZONE
                        </div>

                        <div class="fable-danger-warning">
                            DO NOT TOUCH THIS UNLESS THE SYSTEM
                            IS NOT WORKING OR YOU INTENTIONALLY
                            WANT TO RESET ALL RUNTIME DATA.
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

                    st.caption(
                        "This is a destructive administrator "
                        "maintenance action."
                    )


                    st.warning(
                        "A complete reset permanently removes "
                        "the CURRENT runtime data used by the app."
                    )


                    st.markdown(
                        """
                        **The reset removes:**

                        - all iteration configurations
                        - all submitted SCENATHON reports
                        - all participant result CSV files
                        - all analytics files
                        - all run summaries
                        - all stale markers
                        - any other files stored under the
                          configured runtime storage folder

                        **The reset does NOT remove:**

                        - Streamlit application code
                        - notebooks
                        - backend code
                        - Excel add-in code
                        - Streamlit secrets
                        """
                    )


                    # ---------------------------------------------
                    # Extra protection:
                    # Never allow this bulk reset when runtime
                    # storage accidentally points to main/master.
                    # ---------------------------------------------

                    runtime_branch_is_safe = (
                        GITHUB_BRANCH
                        .strip()
                        .casefold()
                        not in {
                            "main",
                            "master",
                        }
                    )


                    if not runtime_branch_is_safe:

                        st.error(
                            "FULL RESET IS DISABLED because the "
                            f"configured storage branch is "
                            f"`{GITHUB_BRANCH}`. "
                            "Use a dedicated runtime branch such "
                            "as `app-data` before enabling this "
                            "maintenance action."
                        )


                    else:

                        st.caption(
                            "Runtime branch: "
                            f"`{GITHUB_BRANCH}`"
                        )

                        st.caption(
                            "Runtime storage root: "
                            f"`{STORAGE_ROOT}/`"
                        )


                    # =============================================
                    # FIRST STEP — OPEN CONFIRMATION
                    # =============================================

                    with st.form(
                        "fable_open_full_reset_form",
                        border=True,
                    ):

                        st.markdown(
                            '<div class="fable-danger-zone-marker">'
                            '</div>',
                            unsafe_allow_html=True,
                        )

                        open_full_reset = (
                            st.form_submit_button(
                                "RESET APP DATA",
                                type="primary",
                                use_container_width=True,
                                disabled=(
                                    not runtime_branch_is_safe
                                ),
                            )
                        )


                    if open_full_reset:

                        st.session_state[
                            "fable_show_full_reset_confirmation"
                        ] = True


                    # =============================================
                    # SECOND STEP — STRONG CONFIRMATION
                    # =============================================

                    if st.session_state.get(
                        "fable_show_full_reset_confirmation",
                        False,
                    ):

                        st.error(
                            "FINAL CONFIRMATION REQUIRED"
                        )

                        st.write(
                            "After a successful reset, the app "
                            "will behave like a fresh installation "
                            "with no iterations, submissions, "
                            "results or analytics."
                        )


                        with st.form(
                            "fable_full_reset_confirmation_form",
                            clear_on_submit=False,
                            border=True,
                        ):

                            st.markdown(
                                '<div '
                                'class="fable-danger-zone-marker">'
                                '</div>',
                                unsafe_allow_html=True,
                            )


                            reset_admin_password = (
                                st.text_input(
                                    "Re-enter admin password",
                                    type="password",
                                    key=(
                                        "fable_reset_admin_password"
                                    ),
                                )
                            )


                            reset_confirmation_phrase = (
                                st.text_input(
                                    "Type exactly: RESET APP DATA",
                                    key=(
                                        "fable_reset_confirmation_phrase"
                                    ),
                                )
                            )


                            reset_acknowledged = (
                                st.checkbox(
                                    "I understand that all current "
                                    "runtime app data will be "
                                    "permanently removed from the "
                                    "current GitHub branch state.",
                                    key=(
                                        "fable_reset_acknowledged"
                                    ),
                                )
                            )


                            reset_cancel_col, reset_confirm_col = (
                                st.columns(
                                    [1, 1.65]
                                )
                            )


                            with reset_cancel_col:

                                cancel_full_reset = (
                                    st.form_submit_button(
                                        "Cancel",
                                        use_container_width=True,
                                    )
                                )


                            with reset_confirm_col:

                                permanently_reset = (
                                    st.form_submit_button(
                                        "PERMANENTLY RESET APP DATA",
                                        type="primary",
                                        use_container_width=True,
                                        disabled=(
                                            not reset_acknowledged
                                            or (
                                                reset_confirmation_phrase
                                                .strip()
                                                != "RESET APP DATA"
                                            )
                                            or (
                                                not runtime_branch_is_safe
                                            )
                                        ),
                                    )
                                )


                        # -----------------------------------------
                        # CANCEL
                        # -----------------------------------------

                        if cancel_full_reset:

                            st.session_state[
                                "fable_show_full_reset_confirmation"
                            ] = False

                            st.session_state.pop(
                                "fable_reset_admin_password",
                                None,
                            )

                            st.session_state.pop(
                                "fable_reset_confirmation_phrase",
                                None,
                            )

                            st.session_state.pop(
                                "fable_reset_acknowledged",
                                None,
                            )

                            st.rerun()


                        # -----------------------------------------
                        # PERMANENT RESET
                        # -----------------------------------------

                        if permanently_reset:

                            if (
                                reset_admin_password
                                != ADMIN_PASSWORD
                            ):

                                st.error(
                                    "Incorrect admin password. "
                                    "Nothing was deleted."
                                )


                            elif (
                                reset_confirmation_phrase.strip()
                                != "RESET APP DATA"
                            ):

                                st.error(
                                    "Confirmation phrase is "
                                    "incorrect. Nothing was deleted."
                                )


                            elif not reset_acknowledged:

                                st.error(
                                    "You must acknowledge the "
                                    "destructive reset first."
                                )


                            elif not runtime_branch_is_safe:

                                st.error(
                                    "Reset blocked because the "
                                    "runtime storage branch is "
                                    "main/master."
                                )


                            else:

                                try:

                                    with st.status(
                                        "Resetting runtime app data...",
                                        expanded=True,
                                    ) as reset_status:

                                        reset_status.write(
                                            "Connected to "
                                            f"`{GITHUB_OWNER}/"
                                            f"{GITHUB_REPO}`."
                                        )

                                        reset_status.write(
                                            "Runtime branch: "
                                            f"`{GITHUB_BRANCH}`"
                                        )

                                        reset_status.write(
                                            "Deleting all current "
                                            "runtime files under "
                                            f"`{STORAGE_ROOT}/`..."
                                        )


                                        # ---------------------------------
                                        # Existing helper recursively
                                        # deletes every file beneath
                                        # STORAGE_ROOT.
                                        # ---------------------------------

                                        deleted_file_count = (
                                            github_delete_tree(
                                                STORAGE_ROOT
                                            )
                                        )


                                        reset_status.write(
                                            "Deleted current runtime "
                                            "files: "
                                            f"{deleted_file_count}"
                                        )

                                        reset_status.update(
                                            label=(
                                                "Runtime app data "
                                                "reset completed"
                                            ),
                                            state="complete",
                                            expanded=True,
                                        )


                                    st.success(
                                        "All current runtime app data "
                                        "was cleared successfully. "
                                        "The application is now in a "
                                        "fresh state."
                                    )


                                    st.info(
                                        "Application code, notebooks, "
                                        "backend, add-in and secrets "
                                        "were not deleted."
                                    )


                                    st.warning(
                                        "This reset removes files from "
                                        "the CURRENT GitHub branch state. "
                                        "Older file versions can still "
                                        "exist in Git commit history. "
                                        "A separate Git history cleanup "
                                        "is required if you later want "
                                        "to reclaim repository storage "
                                        "used by old commits."
                                    )


                                    # Clean confirmation state

                                    st.session_state[
                                        "fable_show_full_reset_confirmation"
                                    ] = False

                                    st.session_state.pop(
                                        "fable_reset_admin_password",
                                        None,
                                    )

                                    st.session_state.pop(
                                        "fable_reset_confirmation_phrase",
                                        None,
                                    )

                                    st.session_state.pop(
                                        "fable_reset_acknowledged",
                                        None,
                                    )


                                    # Clear Analytics UI state so old
                                    # filters/results do not survive
                                    # after the storage reset.

                                    analytics_state_keys = [
                                        key
                                        for key
                                        in list(
                                            st.session_state.keys()
                                        )
                                        if str(key).startswith(
                                            "analytics_"
                                        )
                                    ]

                                    for key in analytics_state_keys:

                                        st.session_state.pop(
                                            key,
                                            None,
                                        )


                                    st.rerun()


                                except Exception as exc:

                                    st.error(
                                        "Full runtime-data reset "
                                        "failed before completion: "
                                        f"{exc}"
                                    )

                                    st.warning(
                                        "Some files may already have "
                                        "been deleted before the error. "
                                        "Check Admin -> Storage and "
                                        "GitHub before retrying."
                                    )

