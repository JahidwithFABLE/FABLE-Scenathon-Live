

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4
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

APP_TITLE = "FABLE Scenathon"

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from trade_engine import calculate_trade_adjustment, ENGINE_VERSION
from scenathon_exports import build_scenathon_reports, csv_bytes

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


# ============================================================
# 3B. COMMON LABEL FORMATTING
#
# FIX: Shared across Analytics, Admin, and Storage tabs.
# Must be defined at module level.
# ============================================================

def plain_text_label(text):
    text = str(text)
    text = text.replace("_", " ")
    text = text.replace("—", "-").replace("–", "-")

    while "--" in text:
        text = text.replace("--", "-")

    while "  " in text:
        text = text.replace("  ", " ")

    return text.strip()


# ============================================================
# 3A. SCENATHON SESSION ID AND STORAGE ISOLATION
#
# Each session gets a permanent internal ID.
# Iteration names can repeat across sessions.
# "legacy" reads data using the original storage paths.
# ============================================================

def active_session_id():
    return st.session_state.get("active_scenathon_id", "legacy")


def session_config_path(session_id):
    return storage_path("sessions", safe_name(session_id), "session.json")


def list_scenathon_sessions():
    sessions = []

    for entry in github_list_dir(storage_path("sessions")):
        if entry.get("type") == "dir":
            cfg = github_get_json(session_config_path(entry["name"]))

            if cfg:
                sessions.append(cfg)

    return sorted(
        sessions,
        key=lambda c: c.get("created_at_utc", ""),
        reverse=True,
    )


def load_scenathon_session(session_id):
    if session_id == "legacy":
        return {
            "id": "legacy",
            "name": "Legacy Scenathon (old data)",
            "status": "LEGACY",
            "date": "",
        }

    return github_get_json(session_config_path(session_id))


def ensure_session_open():

    # FULL_RESET_WRITE_GUARD_09
    # Do not allow an old browser tab to submit into
    # a session that the Admin has paused or reset.

    published = get_published_active_scenathon()

    if (
        published == "__no_active__"
        or active_session_id() != published
    ):
        raise PermissionError(
            "This Scenathon is no longer active. "
            "New submissions are disabled."
        )
    session = load_scenathon_session(active_session_id())

    if session is None or session.get("status") == "CLOSED":
        raise PermissionError(
            "This Scenathon is closed. "
            "Submissions and edits are locked."
        )


def create_scenathon_session(name, date, time_text):
    if not str(name).strip():
        raise ValueError("Give this Scenathon a name.")

    # Internal ID guarantees uniqueness.
    session_id = "scenathon_" + uuid4().hex

    cfg = {
        "id": session_id,
        "name": str(name).strip(),
        "date": str(date),
        "time": str(time_text).strip(),
        "status": "OPEN",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "closed_at_utc": None,
    }

    github_put_json(
        session_config_path(session_id),
        cfg,
        "Create FABLE Scenathon session",
    )

    return cfg


def close_scenathon_session(session_id):
    if session_id == "legacy":
        raise ValueError(
            "Legacy data cannot be closed as a new session."
        )

    cfg = load_scenathon_session(session_id)

    if not cfg or cfg.get("status") != "OPEN":
        raise ValueError(
            "Scenathon not found or already closed."
        )

    cfg = {
        **cfg,
        "status": "CLOSED",
        "closed_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    github_put_json(
        session_config_path(session_id),
        cfg,
        "Close FABLE Scenathon session",
    )

    return cfg



# SECTION20_REOPEN_FUNCTION
def reopen_scenathon_session(session_id):

    if session_id in (
        "legacy", "__no_active__", "", None
    ):
        raise ValueError(
            "Select a closed Scenathon to reopen."
        )

    cfg = load_scenathon_session(session_id)

    if not cfg or cfg.get("status") != "CLOSED":
        raise ValueError(
            "Only a closed Scenathon can be reopened."
        )

    now = datetime.now(timezone.utc).isoformat()

    updated = dict(cfg)

    updated.update({
        "status": "OPEN",
        "closed_at_utc": None,
        "last_closed_at_utc": cfg.get(
            "closed_at_utc"
        ),
        "reopened_at_utc": now,
        "updated_at_utc": now,
    })

    github_put_json(
        session_config_path(session_id),
        updated,
        "Admin: reopen FABLE Scenathon",
    )

    return updated


def storage_path(*parts) -> str:
    clean_parts = [
        str(part).strip("/")
        for part in parts
        if str(part).strip("/")
    ]

    # Session isolation applies only to data roots.
    # Session configuration and archives stay separate.

    if clean_parts and clean_parts[0] in {
        "iterations",
        "submissions",
        "results",
    }:
        current_id = active_session_id()

        if current_id != "legacy":
            clean_parts = [
                "sessions",
                safe_name(current_id),
            ] + clean_parts

    return "/".join([STORAGE_ROOT] + clean_parts)


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


@st.cache_data(ttl=90, show_spinner=False)
def github_get_file_readonly(path: str):
    """Cached dashboard reads only; upload/delete still fetch a fresh SHA."""
    return github_get_file(path)


@st.cache_data(ttl=90, show_spinner=False)
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
    st.cache_data.clear()  # Invalidate all read caches when stored data changes.
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


@st.cache_data(ttl=90, show_spinner=False)
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
    st.cache_data.clear()
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
    ensure_session_open()

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
    SECTION16_STOP_AT_NEXT_TABLE

    Extract one SCENATHON_report table only.

    Stop when:
    - The next reporting section starts
    - Another reporting table header is encountered
    - Two blank identity rows follow existing data
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

    for row in rows[header_index + 1:]:

        row = list(row)

        cells = {
            str(value).strip()
            for value in row
            if value is not None
        }

        # -----------------------------------------------
        # DETECT NEXT REPORTING SECTION
        # -----------------------------------------------

        next_section = (
            "REPORTING_AGGREGATE" in cells
            or "REPORTING_BYPRODUCT" in cells
            or all(
                column in cells
                for column in REQUIRED_COLUMNS
            )
            or all(
                column in cells
                for column in AGGREGATE_COLUMNS
            )
        )

        if next_section and started:
            break

        # -----------------------------------------------
        # READ CURRENT TABLE ROW
        # -----------------------------------------------

        if len(row) <= max_index:
            row.extend(
                [None] * (
                    max_index + 1 - len(row)
                )
            )

        record = {
            column: row[index]
            for column, index
            in column_indexes.items()
        }

        present = [
            value is not None
            and str(value).strip() != ""
            for value in (
                record[column]
                for column in identity_columns
            )
        ]

        # -----------------------------------------------
        # HANDLE BLANK ROWS
        # -----------------------------------------------

        if not any(present):

            if started:
                blank_after_data += 1

                if blank_after_data >= 2:
                    break

            continue

        blank_after_data = 0

        # Reject incomplete identity records.
        if not all(present):
            raise ValueError(
                "Incomplete SCENATHON report record in "
                + ", ".join(identity_columns)
                + f": {record!r}"
            )

        started = True

        records.append(record)

    if not records:
        raise ValueError(
            "SCENATHON table header was found "
            "but no data rows were detected."
        )

    return pd.DataFrame(
        records,
        columns=required_columns,
    )



# ============================================================
# SECTION17_VALIDATION_HELPERS
# ============================================================

def cc17_validate_table(df, kind):
    import math

    if df.empty:
        raise ValueError(f"{kind} report table is empty")

    if kind == "trade":
        needed = [
            "Location", "Product", "Year",
            "Import_quantity", "Export_quantity",
        ]
        numeric = [
            "Year", "Import_quantity", "Export_quantity",
        ]
        identity = ["Location", "Product", "Year"]

    else:
        needed = [
            "Location", "Year",
            "kcal_feas", "kcal_mder",
            "CalcAllLandCO2e", "CalcAllAgriCO2e",
        ]
        numeric = needed[1:]
        identity = ["Location", "Year"]

    missing = [x for x in needed if x not in df.columns]

    if missing:
        raise ValueError(
            f"{kind}: missing columns: {missing}"
        )

    # Check identity fields.
    for field in ("Location", "Product"):

        if field not in identity:
            continue

        bad = (
            df[field].isna()
            | df[field].astype(str).str.strip().eq("")
        )

        if bad.any():
            raise ValueError(
                f"{kind}: blank {field} in "
                f"{int(bad.sum())} row(s)"
            )

    # Check every numeric field.
    for field in numeric:

        bad = ~df[field].map(
            lambda x: (
                pd.notna(x)
                and math.isfinite(float(x))
            )
        )

        if bad.any():
            raise ValueError(
                f"{kind}: invalid/missing {field} "
                f"in {int(bad.sum())} row(s). "
                "Nothing saved."
            )

    # Year must be an integer.
    bad_year = ~df["Year"].map(
        lambda x: (
            float(x).is_integer()
            and x >= 0
        )
    )

    if bad_year.any():
        raise ValueError(
            f"{kind}: invalid Year values in "
            f"{int(bad_year.sum())} row(s)"
        )

    # Trade quantities cannot be negative.
    if kind == "trade":

        for field in (
            "Import_quantity",
            "Export_quantity",
        ):

            if (df[field] < 0).any():
                raise ValueError(
                    f"{kind}: negative {field} "
                    "is not allowed"
                )

    else:
        for field in ("kcal_feas", "kcal_mder"):

            if (df[field] < 0).any():
                raise ValueError(
                    f"{kind}: negative {field} "
                    "is not allowed"
                )

    # Duplicate checking.
    keys = df[identity].copy()

    for field in ("Location", "Product"):

        if field in keys:
            keys[field] = (
                keys[field]
                .astype(str)
                .str.strip()
                .str.casefold()
            )

    keys["Year"] = keys["Year"].astype(int)

    if keys.duplicated().any():
        raise ValueError(
            f"{kind}: duplicate identity keys detected"
        )


def cc17_read_report_metadata(rows):
    """
    Read saved pathway and trade-adjustment setting.
    No second workbook load is needed.
    """

    wanted = (
        "pathway",
        "scenario on trade adjustment",
    )

    for n in range(min(30, len(rows) - 1)):

        cells = [
            str(v).strip().casefold()
            if v is not None
            else ""
            for v in rows[n]
        ]

        if all(label in cells for label in wanted):

            values = rows[n + 1]
            outcome = {}

            for label in wanted:

                pos = cells.index(label)

                outcome[label] = (
                    str(values[pos]).strip()
                    if (
                        pos < len(values)
                        and values[pos] is not None
                    )
                    else ""
                )

            return outcome

    raise ValueError(
        "SCENATHON_report pathway/trade-adjustment "
        "metadata not found. Check workbook version "
        "and save recalculated results."
    )


def cc17_verify_submission_context(
    bundle,
    participant,
    pathway,
    stage,
):
    """
    Stop invalid submissions before GitHub save.
    """

    location = str(
        bundle.get("location", "")
    ).strip()

    metadata = bundle.get(
        "report_metadata", {}
    )

    workbook_pathway = str(
        metadata.get("pathway", "")
    ).strip()

    trade_option = str(
        metadata.get(
            "scenario on trade adjustment", ""
        )
    ).strip().casefold()

    # SECTION17B_PARTICIPANT_POLICY
    #
    # The selected participant is the official
    # submission identity configured by the admin.
    #
    # Workbook Location is preserved independently.
    # Name differences are not upload blockers.
    #
    # The upload UI warns about mismatches after save.

    if not str(participant).strip():
        raise ValueError(
            "Please select a participant."
        )

    # SECTION17C_ADMIN_PATHWAY_AUTHORITY
    # The Admin-selected Pathway is the submission identity.
    # The Excel Pathway may use a different label.

    if not str(pathway).strip():
        raise ValueError(
            "Please select an Admin Pathway."
        )

    # Check trade-adjustment option.
    if trade_option not in ("yes", "no"):

        raise ValueError(
            f"Trade Adjustment field is "
            f"'{trade_option}', expected Yes/No. "
            "No data saved."
        )

    # Verify Before/After classification.
    if (
        (stage == "after")
        != (trade_option == "yes")
    ):

        raise ValueError(
            f"Wrong submission stage - workbook "
            f"Trade Adjustment is '{trade_option}', "
            f"but you selected '{stage}'. "
            "No data saved."
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

    cc17_validate_table(df, "trade")
    df = df.reset_index(drop=True)

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

    cc17_validate_table(df, "aggregate")
    df = df.reset_index(drop=True)

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

    report_metadata = cc17_read_report_metadata(rows)

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
        "report_metadata": report_metadata,
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
    workbook_pathway: str = "",
    replace_existing: bool = False,
):

    ensure_session_open()

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

        # SECTION17C_WORKBOOK_PATHWAY_METADATA
        "workbook_pathway": str(workbook_pathway).strip(),

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

    from concurrent.futures import (
        ThreadPoolExecutor,
    )

    items = [
        item
        for item in github_list_dir(
            folder
        )
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
        )
    ]


    if not items:

        return []


    def _load_submission_item(
        item,
    ):

        try:

            return github_get_json(
                item[
                    "path"
                ]
            )

        except Exception:

            return None


    max_workers = min(
        6,
        len(
            items
        ),
    )


    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:

        loaded = list(
            executor.map(
                _load_submission_item,
                items,
            )
        )


    payloads = [
        payload
        for payload in loaded
        if payload
    ]


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

    file_data = github_get_file_readonly(
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

    ensure_session_open()

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
# 9A. CONSOLIDATED SCENATHON CSV EXPORTS
# REPORTING_AGGREGATE is country/year data, not a numerical global sum.
# Downloads never rerun the trade adjustment calculation.
# ============================================================

def scenathon_report_dataframes(session_id):
    """Build the two reporting CSV tables for one selected session."""
    original = active_session_id()
    st.session_state["active_scenathon_id"] = session_id
    try:
        cfg = load_scenathon_session(session_id) or {}
        all_submissions = []
        for iteration_cfg in list_iteration_configs():
            iteration = iteration_cfg["iteration"]
            for payload in list_submission_payloads(iteration, stage=None):
                all_submissions.append((iteration, payload))
        aggregate_df, byproduct_df = build_scenathon_reports(
            all_submissions,
            session_id=session_id,
            session_date=str(cfg.get("date", "")),
        )
        return aggregate_df, byproduct_df
    finally:
        st.session_state["active_scenathon_id"] = original


def render_scenathon_exports(session_id, key_prefix="report"):
    """Both downloads from the same session. Only closed sessions are exposed."""
    cfg = load_scenathon_session(session_id)
    if not cfg or cfg.get("status") != "CLOSED":
        st.info("The global Scenathon CSV exports are available after closing the session.")
        return
    try:
        with st.spinner("Preparing the saved Scenathon reports..."):
            aggregate_df, byproduct_df = scenathon_report_dataframes(session_id)
        if aggregate_df.empty and byproduct_df.empty:
            st.warning("There are no saved submissions for this Scenathon.")
            return
        st.caption(f"Saved rows: {len(aggregate_df):,} aggregate; {len(byproduct_df):,} by product / country")
        c1, c2 = st.columns(2)
        c1.download_button(
            "Export REPORTING_AGGREGATE.csv",
            data=csv_bytes(aggregate_df),
            file_name=f"{safe_name(session_id)}_REPORTING_AGGREGATE.csv",
            mime="text/csv", key=f"{key_prefix}_aggregate_export",
            on_click="ignore",
        )
        c2.download_button(
            "Export REPORTING_BYPRODUCT.csv",
            data=csv_bytes(byproduct_df),
            file_name=f"{safe_name(session_id)}_REPORTING_BYPRODUCT.csv",
            mime="text/csv", key=f"{key_prefix}_byproduct_export",
            on_click="ignore",
        )
    except Exception as exc:
        st.error(f"Could not prepare Scenathon export: {exc}")



# ============================================================
# 9B. STORAGE CLEANUP: SCOPE, ARCHIVE AND VERIFY
# Clear only selected session/iteration results and submissions.
# An archive is stored in a separate root before any deletion begins.
# ============================================================

def _tree_file_paths(folder):
    result = []
    for item in github_list_dir(folder):
        if item.get("type") == "dir":
            result.extend(_tree_file_paths(item["path"]))
        elif item.get("type") == "file":
            result.append(item["path"])
    return sorted(result)


def _with_session_storage(session_id, iteration):
    """Path-only scope helper; does not change the dashboard's selected session."""
    if session_id == "legacy":
        return (f"{STORAGE_ROOT}/submissions/{safe_name(iteration)}",
                f"{STORAGE_ROOT}/results/{safe_name(iteration)}")
    root = storage_path("sessions", safe_name(session_id))
    return (f"{root}/submissions/{safe_name(iteration)}",
            f"{root}/results/{safe_name(iteration)}")


def clear_selected_iteration_data(selection, *, archive_first=True):
    """Archive then remove selected iteration data; raise on partial failure.

    selection: list[(session_id, iteration_name)]. No iteration config is deleted.
    Only valid configured iteration names can be used.
    """
    if not selection:
        raise ValueError("Select at least one iteration.")

    # Remove stale GitHub listing/cache before validation.

    st.cache_data.clear()

    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    files = []
    for session_id, iteration in selection:
        if session_id != "legacy" and not load_scenathon_session(session_id):
            raise ValueError(f"Unknown session {session_id}")
        # Validate against ACTUAL stored configuration filenames.
        # Never assume that filename == safe_name(iteration).

        root = (
            f"{STORAGE_ROOT}/iterations"
            if session_id == "legacy"
            else (
                f"{STORAGE_ROOT}/sessions/"
                f"{safe_name(session_id)}/iterations"
            )
        )

        configs = []

        for entry in github_list_dir(root):

            if (
                entry.get("type") != "file"
                or not entry.get("name", "").lower().endswith(".json")
            ):
                continue

            # Read fresh GitHub content, not a cached config.

            item = github_get_file(entry["path"])

            if item is None:
                continue

            config = json.loads(
                item["content"].decode("utf-8")
            )

            name = str(
                config.get("iteration", "")
            ).strip()

            if name:
                configs.append(
                    (name, entry["path"])
                )

        # Exact matching of the selected iteration label.

        matches = [
            path
            for name, path in configs
            if name == iteration
        ]

        # Also prevent different labels from resolving
        # to the same submission/result folder.

        conflicts = [
            (name, path)
            for name, path in configs
            if safe_name(name) == safe_name(iteration)
        ]

        if len(matches) != 1 or len(conflicts) != 1:

            available = ", ".join(
                f"{name} [{path}]"
                for name, path in configs
            )

            raise ValueError(
                f"Cannot safely resolve iteration "
                f"{iteration!r} in {session_id!r}. "
                f"Matching configs: {len(matches)}; "
                f"storage-name conflicts: {len(conflicts)}. "
                f"Available: {available or 'none'}"
            )
        for folder in _with_session_storage(session_id, iteration):
            for path in _tree_file_paths(folder):
                files.append((session_id, iteration, path))
    if not files:
        return {"deleted": 0, "archived": 0, "archive_root": ""}
    # Copy and verify every file before the first delete request.
    archive_root = storage_path("archives", now_stamp + "_" + uuid4().hex[:8])
    if archive_first:
        for session_id, iteration, path in files:
            original = github_get_file(path)
            if original is None:
                raise RuntimeError(f"File disappeared while archiving: {path}")
            # Preserve nested subdirectories and Before/After stages in the archive.
            # Distinguish submission files from published results.
            src_sub, src_res = _with_session_storage(session_id, iteration)
            subpath = ("submissions/" + path[len(src_sub):].lstrip("/")) if path.startswith(src_sub + "/") else ("results/" + path[len(src_res):].lstrip("/"))
            destination = f"{archive_root}/{safe_name(session_id)}/{safe_name(iteration)}/{subpath}"
            github_put_bytes(destination, original["content"], "Archive Scenathon before clear")
            confirmation = github_get_file(destination)
            if confirmation is None or confirmation["content"] != original["content"]:
                raise RuntimeError(f"Archive verification failed: {destination}")
    deleted = 0
    for _session, _iteration, path in files:
        if github_delete_file(path, "Admin clear selected Scenathon data"):
            if github_get_file(path) is not None:
                raise RuntimeError(f"Delete verification failed for {path}; {deleted} earlier files were deleted")
            deleted += 1
    return {"deleted": deleted, "archived": len(files) if archive_first else 0,
            "archive_root": archive_root if archive_first else ""}



# ============================================================
# ADMIN_ACTIVE_SCENATHON_PATCH_07
# ============================================================

NO_ACTIVE_SCENATHON = "__no_active__"

ACTIVE_SCENATHON_PATH = (
    f"{STORAGE_ROOT}/_control/active_scenathon.json"
)

DATA_REVISION_PATH = (
    f"{STORAGE_ROOT}/_control/data_revision.json"
)


def get_published_active_scenathon():
    """
    Read the Admin-selected active session from GitHub.
    This setting is shared across all users and browsers.
    """

    item = github_get_file(ACTIVE_SCENATHON_PATH)

    if item is None:
        return NO_ACTIVE_SCENATHON

    data = json.loads(
        item["content"].decode("utf-8")
    )

    session_id = data.get("session_id")

    if not isinstance(session_id, str) or not session_id:
        return NO_ACTIVE_SCENATHON

    return session_id


def set_published_active_scenathon(session_id):
    """
    Called only from the authenticated Admin interface.
    """

    if session_id != NO_ACTIVE_SCENATHON:

        session = load_scenathon_session(session_id)

        if (
            session is None
            or session.get("status") not in ("OPEN", "LEGACY")
        ):
            raise ValueError(
                "Only an open Scenathon can be activated."
            )

    github_put_json(
        ACTIVE_SCENATHON_PATH,
        {
            "session_id": session_id,
            "updated_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        },
        "Admin: update active Scenathon",
    )


def get_data_revision():
    """
    Fresh GitHub read for detecting data-clearing changes.
    """

    item = github_get_file(DATA_REVISION_PATH)

    if not item:
        return None

    data = json.loads(
        item["content"].decode("utf-8")
    )

    return data.get("revision")


def bump_data_revision():
    """
    Publish a new revision after successfully deleting data.
    """

    github_put_json(
        DATA_REVISION_PATH,
        {
            "revision": uuid4().hex,
            "updated_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        },
        "Admin: invalidate Scenathon analytics",
    )


def reset_loaded_dashboard_data():
    """
    Clear previously loaded Analytics data from
    the current user's Streamlit session.

    Does not delete any GitHub files.
    """

    keys = [
        "flex_global_normalized_df",
        "flex_global_summary_df",
        "flex_global_expected",
        "flex_global_payload_count",
        "gt_run_token",
        "analytics_run_token",
    ]

    for key in keys:
        st.session_state.pop(key, None)


# ============================================================
# COMPLETE_RESET_PATCH_09
# ============================================================

def complete_reset_inventory():
    """Find every file under the current app storage root."""

    def visit(folder):
        found = []

        for entry in github_list_dir(folder):
            path = str(entry.get("path", ""))

            if not path.startswith(
                STORAGE_ROOT.rstrip("/") + "/"
            ):
                raise RuntimeError(
                    f"Unsafe storage path: {path}"
                )

            if entry.get("type") == "dir":
                found.extend(visit(path))

            elif entry.get("type") == "file":
                found.append(
                    (path, entry.get("sha", ""))
                )

        return found

    return sorted(visit(STORAGE_ROOT))


def reset_entire_scenathon_storage(expected_snapshot):
    """
    Completely reset current Scenathon app storage.

    Includes:
    - Legacy configurations
    - Session configurations
    - All iteration configurations
    - Submissions and results
    - Archives
    - Active session control files

    Does not rewrite Git commit history.
    """

    # Restrict this destructive feature to the intended
    # Scenathon data branch and storage directory.

    if (
        GITHUB_BRANCH != "app-data"
        or STORAGE_ROOT.strip("/") != "app_storage"
    ):
        raise RuntimeError(
            "Full reset requires branch 'app-data' "
            "and storage root 'app_storage'."
        )

    if not isinstance(expected_snapshot, list):
        raise ValueError(
            "Preview all files before resetting."
        )

    st.cache_data.clear()

    current_files = complete_reset_inventory()

    # Do not delete if files changed after preview.

    if current_files != expected_snapshot:
        raise RuntimeError(
            "Storage changed since the preview. "
            "Preview again before resetting."
        )

    if not current_files:
        return {
            "deleted": 0,
            "remaining": 0,
        }

    # Stop new submissions first.

    set_published_active_scenathon(
        "__no_active__"
    )

    st.cache_data.clear()

    # Include the updated active-session control file.

    files_to_delete = complete_reset_inventory()

    deleted = 0
    failures = []

    for path, _sha in files_to_delete:

        try:
            success = github_delete_file(
                path,
                "Admin complete Scenathon storage reset",
            )

            if success:
                if github_get_file(path) is not None:
                    raise RuntimeError(
                        "Delete verification failed."
                    )

                deleted += 1

        except Exception as exc:
            failures.append(
                f"{path}: {exc}"
            )

    # Final independent verification.

    st.cache_data.clear()

    remaining = complete_reset_inventory()

    if failures or remaining:
        raise RuntimeError(
            f"Partial reset: {deleted} deleted, "
            f"{len(remaining)} files remain. "
            + "; ".join(failures[:5])
        )

    return {
        "deleted": deleted,
        "remaining": 0,
    }




# ============================================================
# CONTROL CENTER 12B — ADMIN EDITING HELPERS
# ============================================================

def cc12b_iteration_configs(session_id):

    root = (
        f"{STORAGE_ROOT}/iterations"
        if session_id == "legacy"
        else (
            f"{STORAGE_ROOT}/sessions/"
            f"{safe_name(session_id)}/iterations"
        )
    )

    output = {}

    for item in github_list_dir(root):

        if (
            item.get("type") != "file"
            or not item.get("name", "").endswith(".json")
        ):
            continue

        cfg = github_get_json(item["path"])

        if cfg and cfg.get("iteration"):

            name = str(cfg["iteration"])

            if name in output:
                raise ValueError(
                    f"Duplicate iteration label: {name}"
                )

            output[name] = (item["path"], cfg)

    return output


def cc12b_edit_session(
    session_id,
    name,
    session_day,
    start_time,
):

    if session_id == "legacy":
        raise ValueError(
            "Legacy has no editable Scenathon metadata."
        )

    cfg = load_scenathon_session(session_id)

    if not cfg:
        raise ValueError(
            "Scenathon not found. Refresh and try again."
        )

    if not name.strip():
        raise ValueError(
            "Enter a Scenathon name."
        )

    cfg.update({
        "name": name.strip(),
        "date": str(session_day),
        "time": str(start_time),
        "updated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    })

    github_put_json(
        session_config_path(session_id),
        cfg,
        "Admin: edit FABLE Scenathon information",
    )


def cc12b_save_iteration(
    session_id,
    previous_name,
    name,
    participant_lines,
    pathway_lines,
    is_open,
):

    if session_id == "legacy":
        raise ValueError(
            "Create/select a named Scenathon first."
        )

    session = load_scenathon_session(session_id)

    if not session:
        raise ValueError("Scenathon not found.")

    name = name.strip()

    people = normalize_participants(
        participant_lines
    )

    pathways = normalize_participants(
        pathway_lines
    )

    if not name or not people or not pathways:
        raise ValueError(
            "Name, participants and pathways are required."
        )

    existing = cc12b_iteration_configs(
        session_id
    )

    if previous_name and previous_name not in existing:
        raise ValueError(
            "Original iteration no longer exists."
        )

    if (
        not previous_name
        and session.get("status") != "OPEN"
    ):
        raise ValueError(
            "Cannot create iterations in a closed Scenathon."
        )

    if (
        is_open
        and session.get("status") != "OPEN"
    ):
        raise ValueError(
            "A closed Scenathon cannot accept submissions."
        )

    # Prevent duplicate iteration names and paths.

    for other_name in existing:

        if other_name == previous_name:
            continue

        if (
            other_name.casefold() == name.casefold()
            or safe_name(other_name) == safe_name(name)
        ):
            raise ValueError(
                "An iteration with this name/path already exists."
            )

    root = (
        f"{STORAGE_ROOT}/sessions/"
        f"{safe_name(session_id)}"
    )

    if previous_name:

        old_path, old = existing[previous_name]

        old_root = safe_name(previous_name)

        saved_files = []

        for kind in ("submissions", "results"):

            saved_files.extend(
                _tree_file_paths(
                    f"{root}/{kind}/{old_root}"
                )
            )

        # Protect stored data from an unsafe rename.

        if name != previous_name and saved_files:
            raise ValueError(
                "Cannot rename an iteration containing "
                "submissions/results."
            )

        # ====================================================
        # SECTION 12E - SAFE PARTICIPANT/PATHWAY REMOVAL
        #
        # A report from someone else must not block editing.
        # Only protect data belonging to removed entries.
        # ====================================================

        removed_people = {
            str(p).strip().casefold()
            for p in old.get("participants", [])
        } - {
            str(p).strip().casefold()
            for p in people
        }

        removed_pathways = {
            str(p).strip().casefold()
            for p in old.get("pathways", [])
        } - {
            str(p).strip().casefold()
            for p in pathways
        }

        if removed_people or removed_pathways:

            submission_root = (
                f"{root}/submissions/{old_root}"
            )

            submission_files = _tree_file_paths(
                submission_root
            )

            conflicting_reports = []

            for file_path in submission_files:

                if not file_path.lower().endswith(".json"):
                    continue

                payload = github_get_json(file_path)

                if not isinstance(payload, dict):
                    raise RuntimeError(
                        "Could not verify existing submission: "
                        + file_path
                    )

                participant = str(
                    payload.get("participant", "")
                ).strip().casefold()

                pathway = str(
                    payload.get("pathway", "")
                ).strip().casefold()

                if (
                    participant in removed_people
                    or pathway in removed_pathways
                ):
                    conflicting_reports.append(
                        file_path
                    )

            if conflicting_reports:
                raise ValueError(
                    "The selected participant or pathway "
                    "already has submitted data. "
                    "Its reports must be handled before "
                    "removal. No data was deleted."
                )

    else:
        old_path = None

    new_path = (
        f"{root}/iterations/{safe_name(name)}.json"
    )

    github_put_json(
        new_path,
        {
            "iteration": name,
            "participants": people,
            "pathways": pathways,
            "status": (
                "OPEN" if is_open else "CLOSED"
            ),
            "updated_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        },
        "Admin: save FABLE Scenathon iteration",
    )

    # Refresh results after changes to participants/pathways.

    if previous_name and (
        set(old.get("participants", [])) != set(people)
        or set(old.get("pathways", [])) != set(pathways)
    ):

        existing_results = _tree_file_paths(
            f"{root}/results/{old_root}"
        )

        if existing_results:

            github_put_json(
                f"{root}/results/{old_root}/_stale.json",
                {
                    "iteration": name,
                    "stale": True,
                    "reason": "Iteration configuration changed",
                    "marked_at_utc": datetime.now(
                        timezone.utc
                    ).isoformat(),
                },
                "Mark results stale after configuration edit",
            )

        st.cache_data.clear()

        reset_loaded_dashboard_data()

    if old_path and old_path != new_path:

        github_delete_file(
            old_path,
            "Admin: rename iteration config",
        )



# ============================================================
# CC12C_SCOPED_CLEAR_HELPERS
# Exact GitHub paths + SHA verification.
# ============================================================

def cc12c_inventory():
    """Read all files in app storage, including archives."""

    st.cache_data.clear()

    prefix = STORAGE_ROOT.rstrip("/") + "/"
    found = {}

    def walk(folder):
        for entry in github_list_dir(folder):

            path = str(entry.get("path", ""))

            if (
                not path.startswith(prefix)
                or ".." in path.split("/")
            ):
                raise RuntimeError(
                    f"Unsafe GitHub path: {path}"
                )

            if entry.get("type") == "dir":
                walk(path)

            elif entry.get("type") == "file":
                found[path] = str(
                    entry.get("sha", "")
                )

            else:
                raise RuntimeError(
                    f"Unexpected GitHub item: {path}"
                )

            if len(found) > 20000:
                raise RuntimeError(
                    "Too many files - use a smaller storage root"
                )

    walk(STORAGE_ROOT)

    return found


def cc12c_target_files(
    scope,
    sessions,
    iterations,
    pathways,
    inventory,
):
    """Find exact files belonging to selected clear scope."""

    if scope == "Entire storage":
        return dict(inventory)

    chosen_iterations = {
        tuple(json.loads(x))
        for x in iterations
    }

    chosen_pathways = {
        tuple(json.loads(x))
        for x in pathways
    }

    result = {}

    expanded_iters = (
        set(chosen_iterations)
        if scope == "Iteration"
        else set()
    )

    # Selecting every pathway means clearing the iteration.

    if scope == "Pathway":

        by_iteration = {}

        for sid, it, pw in chosen_pathways:
            by_iteration.setdefault(
                (sid, it), set()
            ).add(pw)

        for (sid, it), chosen_pws in by_iteration.items():

            configs = cc12b_iteration_configs(sid)

            if it not in configs:
                raise RuntimeError(
                    f"Missing iteration: {sid}/{it}"
                )

            configured = set(
                configs[it][1].get("pathways", [])
            )

            if (
                configured
                and configured.issubset(chosen_pws)
            ):
                expanded_iters.add((sid, it))

    actual_configs = {}

    for sid, it in expanded_iters:

        configs = cc12b_iteration_configs(sid)

        if it not in configs:
            raise RuntimeError(
                f"Iteration no longer exists: {sid}/{it}"
            )

        actual_configs[(sid, it)] = configs[it][0]

    prefix = STORAGE_ROOT.rstrip("/") + "/"

    for file_path, sha in inventory.items():

        rel = file_path[len(prefix):]
        parts = rel.split("/")

        # ---------------------------------------------
        # Entire selected Scenathon(s)
        # ---------------------------------------------

        if scope == "Scenathon":

            for sid in sessions:

                sid_slug = safe_name(sid)

                if sid == "legacy":

                    direct = rel.startswith(
                        (
                            "iterations/",
                            "submissions/",
                            "results/",
                        )
                    )

                else:

                    direct = rel.startswith(
                        f"sessions/{sid_slug}/"
                    )

                archived = (
                    len(parts) >= 4
                    and parts[0] == "archives"
                    and parts[2] == sid_slug
                )

                if direct or archived:

                    result[file_path] = sha
                    break

            continue

        # ---------------------------------------------
        # Selected iterations
        # ---------------------------------------------

        active_sets = (
            expanded_iters
            if scope == "Pathway"
            else chosen_iterations
        )

        for sid, it in active_sets:

            sid_slug = safe_name(sid)
            it_slug = safe_name(it)

            base = (
                ""
                if sid == "legacy"
                else f"sessions/{sid_slug}/"
            )

            cfg_file = (
                actual_configs.get((sid, it))
                or (
                    f"{prefix}{base}iterations/"
                    f"{it_slug}.json"
                )
            )

            direct = (
                file_path == cfg_file
                or rel.startswith(
                    f"{base}submissions/{it_slug}/"
                )
                or rel.startswith(
                    f"{base}results/{it_slug}/"
                )
            )

            archived = (
                len(parts) >= 5
                and parts[0] == "archives"
                and parts[2] == sid_slug
                and parts[3] == it_slug
            )

            if direct or archived:
                result[file_path] = sha

        if (
            scope != "Pathway"
            or file_path in result
        ):
            continue

        # ---------------------------------------------
        # Selected pathway submissions
        # Also invalidate iteration-wide results
        # ---------------------------------------------

        for sid, it, pw in chosen_pathways:

            if (sid, it) in expanded_iters:
                continue

            sid_slug = safe_name(sid)
            it_slug = safe_name(it)
            pw_slug = safe_name(pw)

            base = (
                ""
                if sid == "legacy"
                else f"sessions/{sid_slug}/"
            )

            is_submission = (
                rel.startswith(
                    f"{base}submissions/{it_slug}/"
                )
                and rel.endswith(
                    f"__{pw_slug}.json"
                )
            )

            is_result = rel.startswith(
                f"{base}results/{it_slug}/"
            )

            archived_submission = (
                len(parts) >= 6
                and parts[0] == "archives"
                and parts[2] == sid_slug
                and parts[3] == it_slug
                and parts[4] == "submissions"
                and rel.endswith(
                    f"__{pw_slug}.json"
                )
            )

            archived_result = (
                len(parts) >= 6
                and parts[0] == "archives"
                and parts[2] == sid_slug
                and parts[3] == it_slug
                and parts[4] == "results"
            )

            if (
                is_submission
                or is_result
                or archived_submission
                or archived_result
            ):
                result[file_path] = sha

    return result


# ============================================================
# EXECUTE VERIFIED CLEAR
# ============================================================

def cc12c_clear(
    scope,
    sessions,
    iterations,
    pathways,
    preview,
):

    if not st.session_state.get(
        "fable_admin_authenticated"
    ):
        raise PermissionError(
            "Admin login required"
        )

    if (
        GITHUB_BRANCH != "app-data"
        or STORAGE_ROOT.strip("/") != "app_storage"
    ):
        raise RuntimeError(
            "Clear is restricted to "
            "branch app-data / app_storage"
        )

    if not ADMIN_PASSWORD:
        raise RuntimeError(
            "Admin password is not configured"
        )

    current = cc12c_inventory()

    targets = cc12c_target_files(
        scope,
        sessions,
        iterations,
        pathways,
        current,
    )

    if targets != preview:
        raise RuntimeError(
            "Files changed since preview. "
            "Preview again; nothing deleted"
        )

    if not targets and scope != "Pathway":
        raise RuntimeError(
            "No matching files; nothing to clear"
        )

    active_sid = (
        get_published_active_scenathon()
    )

    affected = set(sessions)

    affected.update(
        json.loads(x)[0]
        for x in iterations
    )

    affected.update(
        json.loads(x)[0]
        for x in pathways
    )

    # Pause submissions before destructive changes.

    if (
        scope == "Entire storage"
        or active_sid in affected
    ):
        set_published_active_scenathon(
            "__no_active__"
        )

    # Full reset may have created or modified
    # the active-session control file.

    if scope == "Entire storage":

        refreshed = cc12c_inventory()

        active_path = (
            f"{STORAGE_ROOT}/_control/"
            "active_scenathon.json"
        )

        unexpected = (
            set(refreshed)
            - set(targets)
            - {active_path}
        )

        changed = [
            p for p in targets
            if (
                p in refreshed
                and p != active_path
                and refreshed[p] != targets[p]
            )
        ]

        if unexpected or changed:
            raise RuntimeError(
                "Storage changed while pausing; "
                "preview again"
            )

        targets = refreshed

    deleted = 0

    try:

        # -----------------------------------------
        # Delete each selected file
        # -----------------------------------------

        for path, sha in sorted(
            targets.items()
        ):

            original = github_get_file(path)

            if original is None:
                raise RuntimeError(
                    f"File disappeared: {path}"
                )

            control_file_changed = (
                scope == "Entire storage"
                and path.endswith(
                    "/_control/active_scenathon.json"
                )
            )

            if (
                original["sha"] != sha
                and not control_file_changed
            ):
                raise RuntimeError(
                    f"File changed during reset: {path}"
                )

            if not github_delete_file(
                path,
                "Admin clear FABLE Scenathon data",
            ):
                raise RuntimeError(
                    f"Delete failed: {path}"
                )

            if github_get_file(path) is not None:
                raise RuntimeError(
                    f"Deletion could not be verified: {path}"
                )

            deleted += 1

        # -----------------------------------------
        # Update pathway configuration
        # -----------------------------------------

        if scope == "Pathway":

            changed = {}

            for key in pathways:

                sid, it, pw = json.loads(key)

                changed.setdefault(
                    (sid, it), set()
                ).add(pw)

            for (sid, it), removed in changed.items():

                configs = (
                    cc12b_iteration_configs(sid)
                )

                if it not in configs:
                    # Entire iteration may have been deleted.
                    continue

                path, cfg = configs[it]

                remaining = [
                    x for x in cfg.get(
                        "pathways", []
                    )
                    if x not in removed
                ]

                if remaining == cfg.get(
                    "pathways", []
                ):
                    continue

                if not remaining:

                    github_delete_file(
                        path,
                        "Admin clear empty iteration configuration",
                    )

                else:

                    cfg["pathways"] = remaining

                    cfg["updated_at_utc"] = (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

                    github_put_json(
                        path,
                        cfg,
                        "Admin remove cleared pathways",
                    )

        # -----------------------------------------
        # Final verification
        # -----------------------------------------

        st.cache_data.clear()

        remaining_files = (
            cc12c_inventory()
        )

        leftover = sorted(
            set(targets).intersection(
                remaining_files
            )
        )

        if leftover:
            raise RuntimeError(
                f"{len(leftover)} targeted files "
                "remain after clear"
            )

        reset_loaded_dashboard_data()

        if scope != "Entire storage":
            bump_data_revision()

        return deleted

    except Exception as exc:

        st.cache_data.clear()

        raise RuntimeError(
            f"Clear may be partial "
            f"({deleted} files deleted). "
            f"Inspect GitHub before retrying. "
            f"Details: {exc}"
        ) from exc


# ============================================================
# COMPACT CLEAR DATA DIALOG
# ============================================================

@st.dialog("Clear Scenathon Data", width="large")
def cc12c_clear_dialog():

    st.warning(
        "Deletion removes files from the current GitHub "
        "branch. Git commit history is not erased."
    )

    scope = st.radio(
        "Clear scope",
        [
            "Scenathon",
            "Iteration",
            "Pathway",
            "Entire storage",
        ],
        horizontal=True,
        key="cc12c_scope",
    )

    sessions = {
        s["id"]: s.get("name", s["id"])
        for s in list_scenathon_sessions()
    }

    root = STORAGE_ROOT.rstrip("/")

    if github_list_dir(
        f"{root}/iterations"
    ):
        sessions["legacy"] = (
            "Legacy Scenathon"
        )

    picked_sessions = []
    picked_iterations = []
    picked_pathways = []

    # -----------------------------------------
    # Select Scenathons
    # -----------------------------------------

    if scope != "Entire storage":

        picked_sessions = st.multiselect(
            "Scenathon(s)",
            list(sessions),
            format_func=lambda sid: sessions[sid],
            key="cc12c_sessions",
        )

    # -----------------------------------------
    # Select Iterations
    # -----------------------------------------

    iter_map = {}

    if scope in ("Iteration", "Pathway"):

        for sid in picked_sessions:

            configs = (
                cc12b_iteration_configs(sid)
            )

            for name, (_path, cfg) in configs.items():

                key = json.dumps(
                    [sid, name]
                )

                iter_map[key] = (
                    sid, name, cfg
                )

        picked_iterations = st.multiselect(
            "Iteration(s)",
            list(iter_map),
            format_func=lambda key: (
                f"{iter_map[key][0]} - "
                f"{iter_map[key][1]}"
            ),
            key="cc12c_iterations",
        )

    # -----------------------------------------
    # Select Pathways
    # -----------------------------------------

    pathway_map = {}

    if scope == "Pathway":

        for key in picked_iterations:

            sid, name, cfg = iter_map[key]

            for pw in cfg.get(
                "pathways", []
            ):

                pw_key = json.dumps(
                    [sid, name, pw]
                )

                pathway_map[pw_key] = (
                    sid, name, pw
                )

        picked_pathways = st.multiselect(
            "Pathway(s)",
            list(pathway_map),
            format_func=lambda key: (
                " - ".join(
                    pathway_map[key]
                )
            ),
            key="cc12c_pathways",
        )

    # -----------------------------------------
    # Validate selection
    # -----------------------------------------

    valid = (
        scope == "Entire storage"
        or (
            scope == "Scenathon"
            and bool(picked_sessions)
        )
        or (
            scope == "Iteration"
            and bool(picked_iterations)
        )
        or (
            scope == "Pathway"
            and bool(picked_pathways)
        )
    )

    signature = json.dumps([
        scope,
        picked_sessions,
        picked_iterations,
        picked_pathways,
    ])

    # -----------------------------------------
    # Preview exact files
    # -----------------------------------------

    if st.button(
        "Preview exact files",
        disabled=not valid,
        key="cc12c_preview",
    ):

        try:

            inventory = (
                cc12c_inventory()
            )

            target = (
                cc12c_target_files(
                    scope,
                    picked_sessions,
                    picked_iterations,
                    picked_pathways,
                    inventory,
                )
            )

            st.session_state[
                "cc12c_preview_info"
            ] = (
                signature,
                target,
            )

        except Exception as exc:

            st.session_state.pop(
                "cc12c_preview_info",
                None,
            )

            st.error(
                f"Cannot preview: {exc}"
            )

    preview = st.session_state.get(
        "cc12c_preview_info"
    )

    preview_valid = bool(
        preview
        and preview[0] == signature
        and (
            preview[1]
            or (
                scope == "Pathway"
                and picked_pathways
            )
        )
    )

    if (
        preview
        and preview[0] == signature
    ):

        st.info(
            f"Selected GitHub files: "
            f"{len(preview[1])}"
        )

        with st.expander(
            "Review exact file paths"
        ):

            st.code(
                "\n".join(
                    sorted(preview[1])
                ) or "(empty)",
                language="text",
            )

        if scope == "Pathway":

            st.caption(
                "Pathway clear also invalidates "
                "iteration-wide results and their "
                "archived copies."
            )

    # -----------------------------------------
    # Admin password confirmation
    # -----------------------------------------

    pwd = st.text_input(
        "Confirm admin password",
        type="password",
        key="cc12c_password",
    )

    confirm = st.checkbox(
        "I understand this will delete "
        "the selected data",
        key="cc12c_confirm",
    )

    if st.button(
        "Delete selected data",
        type="primary",
        key="cc12c_execute",
        disabled=not (
            preview_valid
            and confirm
            and bool(pwd)
        ),
    ):

        if (
            not ADMIN_PASSWORD
            or pwd != ADMIN_PASSWORD
        ):

            st.error(
                "Incorrect admin password"
            )

            return

        try:

            with st.spinner(
                "Clearing and verifying GitHub files..."
            ):

                n = cc12c_clear(
                    scope,
                    picked_sessions,
                    picked_iterations,
                    picked_pathways,
                    preview[1],
                )

            st.session_state.pop(
                "cc12c_preview_info",
                None,
            )

            st.session_state.pop(
                "cc12c_storage_inventory",
                None,
            )

            st.session_state[
                "cc12c_last_message"
            ] = (
                f"Verified: {n} file(s) removed; "
                "selected configuration updated."
            )

            st.rerun()

        except Exception as exc:

            st.error(str(exc))


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
# 11A. ACTIVE SCENATHON — PUBLIC READ ONLY
#
# Admin selects the active Scenathon.
# Public users cannot change it.
# ============================================================

try:

    _selected_session = (
        get_published_active_scenathon()
    )

    if _selected_session != NO_ACTIVE_SCENATHON:

        _published_config = load_scenathon_session(
            _selected_session
        )

        if (
            not _published_config
            or _published_config.get("status")
            not in ("OPEN", "LEGACY")
        ):
            _selected_session = NO_ACTIVE_SCENATHON

    _current_revision = get_data_revision()

except Exception as exc:

    st.error(
        "Could not read active Scenathon configuration: "
        f"{exc}"
    )

    st.stop()


# Refresh old loaded data if active session or data changed.

session_changed = (
    st.session_state.get("_last_viewed_scenathon_id")
    != _selected_session
)

data_changed = (
    st.session_state.get("_last_data_revision")
    != _current_revision
)

if session_changed or data_changed:

    reset_loaded_dashboard_data()

    # These controls have not yet been rendered in this run.
    st.session_state.pop(
        "admin_clear_iterations", None
    )

    st.session_state.pop(
        "clear_confirmation", None
    )

    st.session_state["_last_viewed_scenathon_id"] = (
        _selected_session
    )

    st.session_state["_last_data_revision"] = (
        _current_revision
    )


st.session_state["active_scenathon_id"] = (
    _selected_session
)

if _selected_session == NO_ACTIVE_SCENATHON:

    _active_session_config = {
        "status": "INACTIVE"
    }

else:

    _active_session_config = (
        load_scenathon_session(_selected_session)
        or {"status": "INACTIVE"}
    )


# No public Active Scenathon selector is displayed.
# Uploads are disabled if no active session is published.


# After a complete reset, discard any previously loaded
# dashboards when the page is rerun or refreshed.

if not github_list_dir(STORAGE_ROOT):

    for key in (
        "flex_global_normalized_df",
        "flex_global_summary_df",
        "flex_global_expected",
        "flex_global_payload_count",
        "gt_run_token",
        "analytics_run_token",
    ):
        st.session_state.pop(key, None)

    st.session_state["active_scenathon_id"] = (
        "__no_active__"
    )

    _active_session_config = {
        "status": "INACTIVE"
    }

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
            if _active_session_config.get("status")
            in ("OPEN", "LEGACY")
            else []
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


                        # SECTION17_SUBMISSION_GATE
                        # Validate BEFORE GitHub save.
                        cc17_verify_submission_context(
                            workbook_bundle,
                            participant,
                            pathway,
                            submission_stage,
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


                        if before_missing:
                            raise ValueError(
                                "After submission requires "
                                "a matching Before report "
                                "for this participant, pathway "
                                "and iteration. Nothing saved."
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

                                # SECTION17C_PASS_WORKBOOK_PATHWAY
                                workbook_pathway=(
                                    workbook_bundle.get(
                                        "report_metadata", {}
                                    ).get("pathway", "")
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


                    # SECTION17C_PATHWAY_MISMATCH_WARNING

                    excel_pathway = str(
                        workbook_bundle.get(
                            "report_metadata", {}
                        ).get("pathway", "")
                    ).strip()

                    if (
                        excel_pathway
                        and excel_pathway.casefold()
                        != str(pathway).strip().casefold()
                    ):
                        st.warning(
                            f"Excel Pathway is '{excel_pathway}', "
                            f"but the Admin Pathway is '{pathway}'. "
                            "The submission is classified under "
                            "the selected Admin Pathway. "
                            "Please confirm that the correct "
                            "scenario workbook was submitted."
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

                    st.markdown("#### Download your submitted SCENATHON report")
                    left_csv, right_csv = st.columns(2)
                    left_csv.download_button(
                        "Download my REPORTING_AGGREGATE.csv",
                        data=csv_bytes(aggregate_df),
                        file_name=f"{safe_name(participant)}_{safe_name(pathway)}_{submission_stage}_AGGREGATE.csv",
                        mime="text/csv", key="my_submitted_aggregate_csv", on_click="ignore",
                    )
                    right_csv.download_button(
                        "Download my REPORTING_BYPRODUCT.csv",
                        data=csv_bytes(trade_df),
                        file_name=f"{safe_name(participant)}_{safe_name(pathway)}_{submission_stage}_BYPRODUCT.csv",
                        mime="text/csv", key="my_submitted_byproduct_csv", on_click="ignore",
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

                result_file = github_get_file_readonly(
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


    st.divider()

    st.markdown(
        "### Closed Scenathon reporting datasets"
    )

    closed_public_sessions = [
        session
        for session in list_scenathon_sessions()
        if session.get("status") == "CLOSED"
    ]

    if closed_public_sessions:

        closed_options = {
            session["id"]: (
                f"{session.get('name', 'Scenathon')} | "
                f"{session.get('date', '')}"
            )
            for session in closed_public_sessions
        }

        selected_closed_session = st.selectbox(
            "Completed Scenathon",
            list(closed_options),
            format_func=lambda sid: closed_options[sid],
            key="public_closed_csv_session",
        )

        render_scenathon_exports(
            selected_closed_session,
            key_prefix="public",
        )

    else:

        st.info(
            "No completed Scenathon reports "
            "are available yet."
        )


# ============================================================
# 15. PUBLIC ANALYTICS
# ============================================================

with analytics_tab:






    @st.fragment
    def _render_analytics_fragment():

        # ========================================================
        # GLOBAL TARGETS DASHBOARD - BEGIN
        # ========================================================
        # ========================================================
        # COMMON LABEL HELPER
        #
        # plain_text_label() is defined in Section 04A.
        # Shared by Analytics, Admin, and Storage.
        # ========================================================


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


            # SECTION23_ANALYSIS_MODE

            with gt_filter_3:

                gt_analysis_mode = st.selectbox(
                    "Analysis mode",
                    [
                        "Compare Before and After",
                        "Before trade adjustment",
                        "After trade adjustment",
                    ],
                    key="gt_analysis_mode23",
                    help=(
                        "Comparison uses separately uploaded "
                        "Before and After calculators."
                    ),
                )

            gt_stage_label = (
                "Before trade adjustment"
                if gt_analysis_mode
                == "Before trade adjustment"
                else "After trade adjustment"
            )

            gt_stage = (
                "before"
                if gt_stage_label
                == "Before trade adjustment"
                else "after"
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

                    # SECTION29_FINAL_ANALYTICS

                    st.caption(
                        "Target summary reflects "
                        + gt_stage_label
                        + ". Charts below follow the "
                        "selected analysis mode."
                    )

                    with st.expander(
                        "View detailed target assessment"
                    ):
                        st.dataframe(
                            pretty_dataframe(
                                gt_result["summary"]
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

                    # SECTION23_TARGET_CAPTION

                    st.caption(
                        "Target cards assess 2030/2050 food security and "
                        "2050 GHG targets. Charts below follow the "
                        "selected year and analysis mode."
                    )

                    gt_year = (
                        st.radio(
                            "Analysis year (visual dashboard)",
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


                    gt_compare_enabled = (
                        gt_analysis_mode
                        == "Compare Before and After"
                        and gt_compare_available
                    )

                    if (
                        gt_analysis_mode
                        == "Compare Before and After"
                        and not gt_compare_available
                    ):

                        st.warning(
                            "Both Before and After submissions "
                            "are required for comparison."
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



                    # SECTION29_CONSISTENT_VISUALS
                    # All three charts use the selected
                    # calculator stage(s) and analysis year.

                    _s29_stage_payloads = {
                        "Before trade adjustment": (
                            gt_compare_before_payloads
                        ),
                        "After trade adjustment": (
                            gt_compare_after_payloads
                        ),
                    }

                    _s29_requested = (
                        list(_s29_stage_payloads)
                        if gt_analysis_mode
                        == "Compare Before and After"
                        else [gt_stage_label]
                    )

                    _s29_expected = {
                        str(x).strip().casefold()
                        for x in gt_expected_participants
                        if str(x).strip()
                    }

                    _s29_frames = {}

                    # ----------------------------------------
                    # CHECK EACH SELECTED STAGE
                    # ----------------------------------------

                    for _s29_stage in _s29_requested:

                        _s29_payloads = [
                            p
                            for p in _s29_stage_payloads[
                                _s29_stage
                            ]
                            if str(
                                p.get("participant", "")
                            ).strip().casefold()
                            in _s29_expected
                        ]

                        _s29_received = [
                            str(
                                p.get("participant", "")
                            ).strip().casefold()
                            for p in _s29_payloads
                        ]

                        if (
                            not _s29_expected
                            or set(_s29_received)
                            != _s29_expected
                            or len(_s29_received)
                            != len(_s29_expected)
                        ):

                            st.warning(
                                f"{_s29_stage}: incomplete "
                                "participant coverage "
                                f"({len(_s29_received)}/"
                                f"{len(_s29_expected)}). "
                                "This stage is not plotted."
                            )

                            continue

                        _s29_frame = (
                            aggregate_rows_from_submissions(
                                _s29_payloads,
                                pathway=gt_pathway,
                            )
                        )

                        if _s29_frame.empty:

                            st.warning(
                                f"{_s29_stage}: "
                                "aggregate data unavailable."
                            )

                            continue

                        _s29_frame = _s29_frame.copy()

                        _s29_frame["Year"] = (
                            pd.to_numeric(
                                _s29_frame["Year"],
                                errors="coerce",
                            )
                        )

                        _s29_frame = _s29_frame.loc[
                            _s29_frame["Year"]
                            == int(gt_year)
                        ].copy()

                        _s29_cols = [
                            "kcal_feas",
                            "kcal_mder",
                            "CalcAllLandCO2e",
                            "CalcAllAgriCO2e",
                        ]

                        _s29_missing = [
                            c
                            for c in _s29_cols
                            if c not in _s29_frame.columns
                        ]

                        if _s29_missing:

                            st.warning(
                                f"{_s29_stage}: missing "
                                "aggregate columns: "
                                + ", ".join(_s29_missing)
                            )

                            continue

                        for _s29_col in _s29_cols:

                            _s29_frame[_s29_col] = (
                                pd.to_numeric(
                                    _s29_frame[_s29_col],
                                    errors="coerce",
                                )
                            )

                        if (
                            len(_s29_frame)
                            != len(_s29_expected)
                            or _s29_frame[
                                "Location"
                            ].isna().any()
                            or _s29_frame[
                                "Location"
                            ].duplicated().any()
                            or _s29_frame[
                                _s29_cols
                            ].isna().any().any()
                            or (
                                _s29_frame["kcal_mder"]
                                <= 0
                            ).any()
                        ):

                            st.warning(
                                f"{_s29_stage}: "
                                f"{gt_year} aggregate data "
                                "is incomplete or invalid. "
                                "Chart omitted for this stage."
                            )

                            continue

                        _s29_frame[
                            "Food security margin (%)"
                        ] = (
                            (
                                _s29_frame["kcal_feas"]
                                /
                                _s29_frame["kcal_mder"]
                                - 1.0
                            )
                            * 100.0
                        )

                        _s29_frames[
                            _s29_stage
                        ] = _s29_frame

                    if (
                        gt_analysis_mode
                        == "Compare Before and After"
                        and len(_s29_frames) != 2
                    ):

                        st.warning(
                            "Before/After comparison is "
                            "incomplete for the selected year. "
                            "Charts are not presented as "
                            "a full comparison."
                        )

                    # ========================================
                    # FOOD SECURITY CHART
                    # ========================================

                    _s29_food_rows = []

                    for (
                        _s29_stage,
                        _s29_frame,
                    ) in _s29_frames.items():

                        for _, _s29_item in (
                            _s29_frame.iterrows()
                        ):

                            _s29_food_rows.append({
                                "Location": (
                                    plain_text_label(
                                        _s29_item["Location"]
                                    )
                                ),
                                "Stage": _s29_stage,
                                "Margin": float(
                                    _s29_item[
                                        "Food security margin (%)"
                                    ]
                                ),
                            })

                    food_chart_ready = bool(
                        _s29_food_rows
                    )

                    gt_food_fig = go.Figure()

                    if food_chart_ready:

                        _s29_food_df = pd.DataFrame(
                            _s29_food_rows
                        )

                        gt_food_fig = px.bar(
                            _s29_food_df,
                            x="Location",
                            y="Margin",
                            color="Stage",
                            barmode="group",
                            color_discrete_map={
                                "Before trade adjustment": (
                                    "#4474A5"
                                ),
                                "After trade adjustment": (
                                    "#28886E"
                                ),
                            },
                        )

                        for _s29_limit in (10, 50):

                            gt_food_fig.add_hline(
                                y=_s29_limit,
                                line_dash="dash",
                                line_color="#334155",
                                opacity=0.65,
                            )

                        gt_food_fig.update_layout(
                            title=(
                                f"Food security margin "
                                f"- {gt_year}"
                            ),
                            template="plotly_white",
                            height=495,
                            yaxis_title=(
                                "Food security margin (%)"
                            ),
                            legend_title_text="",
                            xaxis_tickangle=-35,
                            margin=dict(
                                l=8,
                                r=8,
                                t=70,
                                b=115,
                            ),
                        )

                    # ========================================
                    # CONSISTENT GHG CHART FUNCTION
                    # ========================================

                    def _s29_ghg_plot(
                        col,
                        title,
                        target,
                    ):

                        fig = go.Figure()

                        for (
                            stage,
                            df_stage,
                        ) in _s29_frames.items():

                            for _, item in (
                                df_stage.iterrows()
                            ):

                                location = str(
                                    item["Location"]
                                )

                                fig.add_trace(
                                    go.Bar(
                                        x=[stage],
                                        y=[
                                            float(
                                                item[col]
                                            ) / 1000.0
                                        ],
                                        name=(
                                            plain_text_label(
                                                location
                                            )
                                        ),
                                        legendgroup=location,
                                        marker_color=(
                                            gt_location_colors.get(
                                                location
                                            )
                                        ),
                                        showlegend=not any(
                                            t.name
                                            == plain_text_label(
                                                location
                                            )
                                            for t in fig.data
                                        ),
                                        hovertemplate=(
                                            f"{plain_text_label(location)}"
                                            "<br>%{x}<br>"
                                            "%{y:.3f} Gt CO2e"
                                            "<extra></extra>"
                                        ),
                                    )
                                )

                            total = (
                                float(
                                    df_stage[col].sum()
                                ) / 1000.0
                            )

                            fig.add_trace(
                                go.Scatter(
                                    x=[stage],
                                    y=[total],
                                    mode="markers",
                                    marker=dict(
                                        symbol="diamond",
                                        size=11,
                                        color="#111827",
                                    ),
                                    showlegend=False,
                                    hovertemplate=(
                                        "Net total: "
                                        "%{y:.3f} Gt CO2e"
                                        "<extra></extra>"
                                    ),
                                )
                            )

                        # 2050 targets are not applied
                        # as 2030 achievement tests.

                        if int(gt_year) == 2050:

                            fig.add_hline(
                                y=target,
                                line_dash="dash",
                                line_color="#334155",
                                opacity=0.7,
                                annotation_text=(
                                    "2050 target: "
                                    f"{target:g} Gt"
                                ),
                            )

                        fig.update_layout(
                            title=(
                                f"{title} - {gt_year}"
                            ),
                            barmode="relative",
                            template="plotly_white",
                            height=495,
                            yaxis_title="Gt CO2e",
                            legend_title_text="Region",
                            showlegend=(
                                len(gt_location_colors)
                                <= 12
                            ),
                            margin=dict(
                                l=8,
                                r=8,
                                t=70,
                                b=95,
                            ),
                        )

                        if not _s29_frames:

                            fig.add_annotation(
                                text=(
                                    "Comparable data "
                                    "unavailable"
                                ),
                                x=0.5,
                                y=0.5,
                                xref="paper",
                                yref="paper",
                                showarrow=False,
                            )

                        return fig

                    gt_land_fig = _s29_ghg_plot(
                        "CalcAllLandCO2e",
                        "Land-use change GHG",
                        -1.0,
                    )

                    gt_agri_fig = _s29_ghg_plot(
                        "CalcAllAgriCO2e",
                        "Agriculture GHG",
                        4.0,
                    )

                    st.caption(
                        "GHG targets apply to 2050. "
                        "Figures show the selected year "
                        f"({gt_year}) and selected "
                        "calculator stage(s). "
                        "Black diamonds represent "
                        "global totals."
                    )

                    # =================================================
                    # SECTION30_FIXED_VISUAL_POLISH
                    # Keep 2050 target labels outside the bars.
                    gt_food_fig.update_layout(
                        height=550,
                        legend=dict(
                            orientation="h", x=0.5, xanchor="center",
                            y=-0.23, yanchor="top",
                            font=dict(size=10, color="#182438"),
                            title=None,
                        ),
                        margin=dict(l=10, r=12, t=65, b=165),
                    )

                    _s30x_region_names = {
                        "AFRICA": "Africa",
                        "Asia and Pacific": "Asia-Pacific",
                        "Latin America and the Caribbean": "Latin America",
                        "Middle East, North Africa and Central Asia": "MENA & Central Asia",
                        "North America": "North America",
                    }
                    for _s30x_ghg_fig in (gt_land_fig, gt_agri_fig):
                        _s30x_ghg_fig.update_layout(
                            height=550,
                            legend=dict(
                                orientation="h", x=0.5, xanchor="center",
                                y=-0.20, yanchor="top",
                                font=dict(size=9, color="#182438"),
                                title=None,
                            ),
                            margin=dict(l=12, r=10, t=90, b=165),
                        )
                        for _s30x_ann in _s30x_ghg_fig.layout.annotations or ():
                            if "target" in str(_s30x_ann.text or "").lower():
                                _s30x_ann.update(
                                    xref="paper", yref="paper",
                                    x=0.02, y=1.03,
                                    xanchor="left", yanchor="bottom",
                                    text="<b>" + str(_s30x_ann.text) + "</b>",
                                    font=dict(size=12, color="#111827"),
                                    bgcolor="rgba(255,255,255,0.96)",
                                    bordercolor="#94A3B8", borderwidth=1,
                                    borderpad=4, showarrow=False,
                                )
                        for _s30x_trace in _s30x_ghg_fig.data:
                            _s30x_name = str(_s30x_trace.name or "")
                            if _s30x_name in _s30x_region_names:
                                _s30x_trace.name = _s30x_region_names[_s30x_name]

                    # SECTION31_EXACT_TARGET_VALUES
                    # Presentation-only correction.

                    def _s31_fmt(number):
                        return (
                            f"{float(number):,.3f}"
                            .replace("-", "−")
                        )

                    def _s31_stage_totals(column):
                        result = {}

                        for stage, frame in _s29_frames.items():

                            if (
                                frame is None
                                or frame.empty
                                or column not in frame
                            ):
                                continue

                            values = pd.to_numeric(
                                frame[column],
                                errors="coerce",
                            )

                            if not values.notna().all():
                                continue

                            result[stage] = (
                                float(values.sum()) / 1000.0
                            )

                        return result

                    _s31_land_totals = _s31_stage_totals(
                        "CalcAllLandCO2e"
                    )

                    _s31_agri_totals = _s31_stage_totals(
                        "CalcAllAgriCO2e"
                    )

                    _s31_before = "Before trade adjustment"
                    _s31_after = "After trade adjustment"

                    def _s31_totals_caption(totals):

                        entries = []

                        for stage, label in (
                            (_s31_before, "Before"),
                            (_s31_after, "After"),
                        ):

                            if stage in totals:
                                entries.append(
                                    f"{label}: "
                                    f"{_s31_fmt(totals[stage])} "
                                    "Gt CO₂e"
                                )

                        if (
                            _s31_before in totals
                            and _s31_after in totals
                        ):

                            difference = (
                                totals[_s31_after]
                                - totals[_s31_before]
                            )

                            entries.append(
                                "Change (After − Before): "
                                f"{_s31_fmt(difference)} Gt"
                            )

                        return (
                            " · ".join(entries)
                            if entries
                            else "Actual totals unavailable"
                        )

                    # ----------------------------------------
                    # 2. REMOVE OLD FLOATING TARGET BOXES
                    # ----------------------------------------

                    for fig, totals in (
                        (gt_land_fig, _s31_land_totals),
                        (gt_agri_fig, _s31_agri_totals),
                    ):

                        fig.layout.annotations = tuple(
                            annotation
                            for annotation in (
                                fig.layout.annotations or ()
                            )
                            if "2050 target" not in str(
                                annotation.text or ""
                            ).casefold()
                        )

                        # ------------------------------------
                        # 3. LABEL BLACK DIAMOND TOTALS
                        # ------------------------------------

                        for stage, total in totals.items():

                            fig.add_annotation(
                                x=stage,
                                y=total,
                                xref="x",
                                yref="y",

                                text=(
                                    "<b>"
                                    + _s31_fmt(total)
                                    + " Gt</b>"
                                ),

                                showarrow=False,

                                yshift=(
                                    -22
                                    if fig is gt_agri_fig
                                    else 24
                                ),

                                xanchor="center",
                                yanchor="middle",

                                font=dict(
                                    size=11,
                                    color="#111827",
                                ),

                                bgcolor=(
                                    "rgba(255,255,255,0.96)"
                                ),

                                bordercolor="#CBD5E1",
                                borderwidth=1,
                                borderpad=3,
                            )

                        fig.update_layout(
                            hovermode="closest",
                            margin=dict(
                                l=12,
                                r=12,
                                t=65,
                                b=165,
                            ),
                        )

                    # ----------------------------------------
                    # 4. FOOD SECURITY LIMIT LABELS
                    # ----------------------------------------

                    if food_chart_ready:

                        for level, label, shift in (
                            (10, "Minimum: 10%", 13),
                            (50, "Maximum: 50%", -13),
                        ):

                            gt_food_fig.add_annotation(
                                x=0.99,
                                xref="paper",
                                y=level,
                                yref="y",

                                text=f"<b>{label}</b>",
                                showarrow=False,
                                yshift=shift,
                                xanchor="right",

                                font=dict(
                                    size=11,
                                    color="#172554",
                                ),

                                bgcolor=(
                                    "rgba(255,255,255,0.96)"
                                ),

                                bordercolor="#CBD5E1",
                                borderwidth=1,
                                borderpad=3,
                            )

                    # GHG targets only apply to 2050.
                    _s31_year_is_target = (
                        int(gt_year) == 2050
                    )

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

                        if _s31_year_is_target:
                            st.markdown(
                                "**2050 target: ≤ −1.000 Gt CO₂e**"
                            )
                        else:
                            st.caption(
                                "2030 outcome — 2050 GHG "
                                "target applies only in 2050"
                            )

                        st.caption(
                            _s31_totals_caption(
                                _s31_land_totals
                            )
                        )

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

                        if _s31_year_is_target:
                            st.markdown(
                                "**2050 target: ≤ 4.000 Gt CO₂e**"
                            )
                        else:
                            st.caption(
                                "2030 outcome — 2050 GHG "
                                "target applies only in 2050"
                            )

                        st.caption(
                            _s31_totals_caption(
                                _s31_agri_totals
                            )
                        )

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
            "Select iterations, pathways, participants and indicators to explore results."
        )


        # ========================================================
        # HELPERS
        # ========================================================

        # SECTION22_PATHWAY_ITERATION_EVOLUTION

        def render_pathway_iteration_evolution():

            import re

            st.markdown("#### Build a comparison")

            st.caption(
                "Compare the effects of trade adjustment "
                "and sustainability outcomes across "
                "pathways, participants and Scenathon rounds."
            )

            # ----------------------------------------------------
            # ITERATION CONFIGURATION
            # ----------------------------------------------------

            configs = [
                config
                for config in list_iteration_configs()
                if str(
                    config.get("iteration", "")
                ).strip()
            ]

            def iteration_order(config):

                name = str(
                    config.get("iteration", "")
                )

                numbers = re.findall(
                    r"\d+", name
                )

                return (
                    int(numbers[-1])
                    if numbers else 10**9,
                    name.casefold(),
                )

            configs.sort(
                key=iteration_order
            )

            config_lookup = {
                str(config["iteration"]).strip(): config
                for config in configs
            }

            iterations = list(
                config_lookup
            )

            if not iterations:

                st.info(
                    "No configured iteration is available."
                )

                return

            # ----------------------------------------------------
            # SELECT ITERATIONS
            # ----------------------------------------------------

            _sc26_top = st.columns([2.2, 1], gap="medium")  # SECTION26_COMPACT_STUDIO
            with _sc26_top[0]:
                with st.container(border=True):
                    selected_iterations = st.multiselect(
                        "Iterations",
                        iterations,
                        default=iterations,
                        key="pe22_iterations",
                    )

            if not selected_iterations:

                st.info(
                    "Select at least one iteration."
                )

                return

            # ----------------------------------------------------
            # READ-ONLY SNAPSHOT
            # ----------------------------------------------------

            session_id = str(
                active_session_id()
            )

            snapshot_key = (
                session_id,
                tuple(selected_iterations),
            )

            with _sc26_top[1]:
                with st.container(border=True):
                    refresh_clicked = st.button(
                        "Load / refresh comparison data",
                        type="primary",
                        key="pe22_refresh",
                    )

            snapshot = st.session_state.get(
                "pe22_snapshot"
            )

            if refresh_clicked:

                trade_records = []
                global_records = []
                diagnostics = []

                stage_names = {
                    "before": "Before trade adjustment",
                    "after": "After trade adjustment",
                }

                with st.spinner(
                    "Loading Scenathon comparison data..."
                ):

                    for iteration in selected_iterations:

                        config = config_lookup[
                            iteration
                        ]

                        expected_participants = {
                            str(value).strip().casefold()
                            for value in config.get(
                                "participants", []
                            )
                            if str(value).strip()
                        }

                        expected_pathways = {
                            str(value).strip().casefold()
                            for value in config.get(
                                "pathways", []
                            )
                            if str(value).strip()
                        }

                        # ----------------------------------------
                        # CALCULATOR SUBMISSIONS
                        # ----------------------------------------

                        for stage, stage_label in (
                            stage_names.items()
                        ):

                            try:

                                submissions = (
                                    list_submission_payloads(
                                        iteration,
                                        stage=stage,
                                    )
                                )

                            except Exception as exc:

                                diagnostics.append(
                                    f"{iteration} / "
                                    f"{stage_label}: {exc}"
                                )

                                continue

                            for payload in submissions:

                                participant = str(
                                    payload.get(
                                        "participant", ""
                                    )
                                ).strip()

                                pathway = str(
                                    payload.get(
                                        "pathway", ""
                                    )
                                ).strip()

                                if (
                                    participant.casefold()
                                    not in expected_participants
                                    or pathway.casefold()
                                    not in expected_pathways
                                ):
                                    continue

                                metadata = {
                                    "Iteration": iteration,
                                    "Pathway": pathway,
                                    "Participant": participant,
                                    "Stage": stage_label,
                                }

                                # TRADE DATA

                                for row in payload.get(
                                    "rows", []
                                ):

                                    trade_records.append({
                                        **metadata,

                                        "Product": str(
                                            row.get(
                                                "Product", ""
                                            )
                                        ).strip().casefold(),

                                        "Year": row.get(
                                            "Year"
                                        ),

                                        "Imports": row.get(
                                            "Import_quantity"
                                        ),

                                        "Exports": row.get(
                                            "Export_quantity"
                                        ),
                                    })

                                # GLOBAL INDICATORS

                                for row in payload.get(
                                    "aggregate_rows", []
                                ):

                                    global_records.append({
                                        **metadata,

                                        "Year": row.get(
                                            "Year"
                                        ),

                                        "kcal_feas": row.get(
                                            "kcal_feas"
                                        ),

                                        "kcal_mder": row.get(
                                            "kcal_mder"
                                        ),

                                        "Agriculture": row.get(
                                            "CalcAllAgriCO2e"
                                        ),

                                        "Land": row.get(
                                            "CalcAllLandCO2e"
                                        ),
                                    })

                        # ----------------------------------------
                    # SECTION29_STUDIO_SUBMITTED_DATA_ONLY

                    try:
                        summary = (
                            load_run_summary(iteration)
                            or {}
                        )

                        stale = bool(
                            load_stale_marker(iteration)
                        )

                        diagnostics.append(
                            f"{iteration}: trade run "
                            + (
                                "STALE"
                                if stale
                                else str(
                                    summary.get(
                                        "status",
                                        "NOT RUN",
                                    )
                                )
                            )
                            + "; QA blockers = "
                            + str(
                                summary.get(
                                    "blocker_count",
                                    "N/A",
                                )
                            )
                        )

                    except Exception as exc:

                        diagnostics.append(
                            f"{iteration}: "
                            f"run status unavailable: {exc}"
                        )

                # PREPARE DATAFRAMES
                # -----------------------------------------------

                trade_df = pd.DataFrame(
                    trade_records
                )

                global_df = pd.DataFrame(
                    global_records
                )

                numeric_specs = [
                    (
                        trade_df,
                        [
                            "Year",
                            "Imports",
                            "Exports",
                        ],
                    ),
                    (
                        global_df,
                        [
                            "Year",
                            "kcal_feas",
                            "kcal_mder",
                            "Agriculture",
                            "Land",
                        ],
                    ),
                ]

                for df, columns in numeric_specs:

                    if df.empty:

                        continue

                    for column in columns:

                        df[column] = pd.to_numeric(
                            df[column],
                            errors="coerce",
                        )

                    df.dropna(
                        subset=["Year"],
                        inplace=True,
                    )

                    df["Year"] = (
                        df["Year"].astype(int)
                    )

                snapshot = {
                    "key": snapshot_key,
                    "trade": trade_df,
                    "indicators": global_df,
                    "diagnostics": diagnostics,
                }

                st.session_state[
                    "pe22_snapshot"
                ] = snapshot

                st.success(
                    "Comparison data loaded successfully."
                )

            if (
                not snapshot
                or snapshot.get("key") != snapshot_key
            ):

                st.info(
                    "Click Load / refresh comparison data "
                    "to explore the selected iterations."
                )

                return

            trade_df = snapshot["trade"]

            global_df = snapshot[
                "indicators"
            ]

            if (
                trade_df.empty
                and global_df.empty
            ):

                st.warning(
                    "No usable comparison data is available."
                )

                return

            with st.expander(
                "Data freshness and calculation status"
            ):

                for message in snapshot[
                    "diagnostics"
                ]:

                    st.caption(
                        message
                    )

                st.caption(
                    "This comparison uses a read-only snapshot. "
                    "Refresh after new submissions or "
                    "trade adjustment calculations."
                )

            # ====================================================
            # COMPARISON MODE
            # ====================================================

            _sc26_first = st.columns([1.2, 1.45, 1], gap="medium")
            with _sc26_first[0]:
                with st.container(border=True):
                    view = st.radio(
                        "Comparison view",
                        [
                            "Within iteration",
                            "Across iterations",
                            "Participant journey",
                        ],
                        horizontal=False,
                        key="pe22_view",
                    )

            # ----------------------------------------------------
            # PATHWAY FILTER
            # ----------------------------------------------------

            pathway_values = set()

            if not trade_df.empty:

                pathway_values.update(
                    trade_df[
                        "Pathway"
                    ].tolist()
                )

            if not global_df.empty:

                pathway_values.update(
                    global_df[
                        "Pathway"
                    ].tolist()
                )

            pathway_options = sorted(
                pathway_values,
                key=str.casefold,
            )

            with _sc26_first[1]:
                with st.container(border=True):
                    selected_pathways = st.multiselect(
                        "Pathways",
                        pathway_options,
                        default=pathway_options,
                        key="pe22_pathways",
                    )

            if not selected_pathways:

                st.info(
                    "Select at least one pathway."
                )

                return

            # ----------------------------------------------------
            # ITERATION SCOPE
            # ----------------------------------------------------

            if view == "Within iteration":

                with _sc26_first[2]:
                    with st.container(border=True):
                        selected_iteration = st.selectbox(
                            "Iteration",
                            selected_iterations,
                            key="pe22_single_iteration",
                        )

                iterations_in_view = [
                    selected_iteration
                ]

            else:

                iterations_in_view = (
                    selected_iterations
                )

            # ----------------------------------------------------
            # PARTICIPANT SCOPE
            # ----------------------------------------------------

            participant_sets = [
                {
                    str(value).strip()
                    for value in (
                        config_lookup[iteration].get(
                            "participants", []
                        )
                    )
                    if str(value).strip()
                }
                for iteration in iterations_in_view
            ]

            common_participants = (
                set.intersection(
                    *participant_sets
                )
                if participant_sets
                else set()
            )

            all_participants = sorted(
                (
                    set.union(
                        *participant_sets
                    )
                    if participant_sets
                    else set()
                ),
                key=str.casefold,
            )

            _sc26_second = st.columns([1, 1, 1.25], gap="medium")
            with _sc26_second[0]:
                with st.container(border=True):
                    scope_mode = st.selectbox(
                        "Participant scope",
                        [
                            "Common participants",
                            "All configured participants",
                            "Choose participants",
                        ],
                        index=(
                            0
                            if len(iterations_in_view) > 1
                            else 1
                        ),
                        key="pe22_scope",
                    )

            if scope_mode == "Common participants":

                selected_participants = sorted(
                    common_participants,
                    key=str.casefold,
                )

            elif scope_mode == "All configured participants":

                selected_participants = (
                    all_participants
                )

            else:

                selected_participants = st.multiselect(
                    "Participants",
                    all_participants,
                    default=all_participants,
                    key="pe22_participants",
                )

            if not selected_participants:

                st.warning(
                    "No participants are available "
                    "for the selected comparison scope."
                )

                return

            if (
                len(iterations_in_view) > 1
                and scope_mode == "All configured participants"
            ):

                if any(
                    group != participant_sets[0]
                    for group in participant_sets[1:]
                ):

                    st.warning(
                        "Participant membership differs across "
                        "the selected iterations. "
                        "Common participants provide a "
                        "more comparable assessment."
                    )

            # ====================================================
            # INDICATOR SELECTION
            # ====================================================

            with _sc26_second[1]:
                with st.container(border=True):
                    indicator = st.selectbox(
                        "Indicator",
                        [
                            "Product-level trade imbalance",
                            "Total exports",
                            "Total imports",
                            "Food security target achievement (%)",
                            "Food security margin (%)",
                            "Agriculture GHG (Gt CO2e)",
                            "Land-use change GHG (Gt CO2e)",
                        ],
                        key="pe22_indicator",
                    )

            trade_indicators = {
                "Product-level trade imbalance",
                "Total exports",
                "Total imports",
            }

            is_trade = (
                indicator in trade_indicators
            )

            # SECTION28_STUDIO_STAGES
            available_stages = [
                "Before trade adjustment",
                "After trade adjustment",
            ]

            with _sc26_second[2]:
                with st.container(border=True):
                    selected_stages = st.multiselect(
                        "Results to compare",
                        available_stages,
                        default=available_stages,
                        key=(
                            "pe22_stages_trade"
                            if is_trade
                            else "pe22_stages_global"
                        ),
                    )

            if not selected_stages:

                st.info(
                    "Select at least one result stage."
                )

                return

            source_df = (
                trade_df
                if is_trade
                else global_df
            )

            source_df = source_df[
                source_df[
                    "Iteration"
                ].isin(
                    iterations_in_view
                )
                &
                source_df[
                    "Pathway"
                ].isin(
                    selected_pathways
                )
                &
                source_df[
                    "Participant"
                ].isin(
                    selected_participants
                )
                &
                source_df[
                    "Stage"
                ].isin(
                    selected_stages
                )
            ].copy()

            if source_df.empty:

                st.info(
                    "No matching data for the selected filters."
                )

                return

            # ----------------------------------------------------
            # PRODUCT FILTER
            # ----------------------------------------------------

            _sc26_third = st.columns([1.3, 1, 1], gap="medium")
            if is_trade:

                products = sorted(
                    source_df[
                        "Product"
                    ]
                    .dropna()
                    .astype(str)
                    .unique()
                    .tolist(),
                    key=str.casefold,
                )

                with _sc26_third[0]:
                    with st.container(border=True):
                        selected_product = st.selectbox(
                            "Product",
                            ["All products"] + products,
                            key="pe22_product",
                        )

                if selected_product != "All products":

                    source_df = source_df[
                        source_df[
                            "Product"
                        ] == selected_product
                    ].copy()

            # ----------------------------------------------------
            # YEAR FILTER
            # ----------------------------------------------------

            years = sorted(
                source_df[
                    "Year"
                ].dropna().unique().tolist()
            )

            if indicator.startswith(
                "Food security"
            ):

                years = [
                    year
                    for year in years
                    if year in (
                        2030, 2050
                    )
                ]

            if not years:

                st.info(
                    "No suitable years are available."
                )

                return

            with _sc26_third[1]:
                with st.container(border=True):
                    selected_year = st.selectbox(
                        "Year",
                        years,
                        index=(
                            years.index(2050)
                            if 2050 in years
                            else len(years) - 1
                        ),
                        key=(
                            "pe22_year_food"
                            if indicator.startswith(
                                "Food security"
                            )
                            else "pe22_year_other"
                        ),
                    )

            # ----------------------------------------------------
            # VISUALIZATION CONTROLS
            # ----------------------------------------------------

            with _sc26_third[2]:
                with st.container(border=True):
                    chart_type = st.radio(
                        "Chart type",
                        [
                            "Grouped bars",
                            "Lines",
                            "Data table",
                        ],
                        horizontal=False,
                        key="pe22_chart",
                    )

            with _sc26_third[2]:
                with st.container(border=True):
                    show_time_series = st.checkbox(
                        "Show changes over time",
                        value=False,
                        key="pe22_time_series",
                        disabled=(
                            chart_type == "Data table"
                        ),
                    )

            if not show_time_series:

                source_df = source_df[
                    source_df[
                        "Year"
                    ] == int(selected_year)
                ].copy()

            # ====================================================
            # BUILD COMPARABLE ANALYSIS
            # ====================================================

            group_columns = [
                "Iteration",
                "Pathway",
                "Year",
            ]

            if (
                view == "Participant journey"
                or indicator == "Food security margin (%)"
            ):

                group_columns.append(
                    "Participant"
                )

            output_rows = []

            omitted_products = 0

            incomplete_groups = 0

            grouped = source_df.groupby(
                group_columns,
                dropna=False,
                sort=False,
            )

            for identity, frame in grouped:

                if not isinstance(
                    identity, tuple
                ):

                    identity = (
                        identity,
                    )

                group_info = dict(
                    zip(
                        group_columns,
                        identity,
                    )
                )

                iteration_name = group_info[
                    "Iteration"
                ]

                # -----------------------------------------------
                # REQUIRED PARTICIPANTS
                # -----------------------------------------------

                if "Participant" in group_info:

                    required_participants = [
                        group_info[
                            "Participant"
                        ]
                    ]

                else:

                    configured = {
                        str(value).strip()
                        for value in (
                            config_lookup[
                                iteration_name
                            ].get(
                                "participants", []
                            )
                        )
                    }

                    required_participants = [
                        participant
                        for participant
                        in selected_participants
                        if participant in configured
                    ]

                if (
                    "Participant" not in group_info
                    and scope_mode
                    != "All configured participants"
                    and len(required_participants)
                    != len(selected_participants)
                ):

                    incomplete_groups += 1

                    continue

                stage_data = {}

                # -----------------------------------------------
                # DATA AVAILABILITY BY STAGE
                # -----------------------------------------------

                for stage in selected_stages:

                    stage_df = frame[
                        frame[
                            "Stage"
                        ] == stage
                    ]

                    if is_trade:

                        product_data = {}

                        for product, product_df in (
                            stage_df.groupby(
                                "Product"
                            )
                        ):

                            complete = (
                                product_df[
                                    "Participant"
                                ].nunique()
                                == len(
                                    required_participants
                                )
                                and not product_df[
                                    "Imports"
                                ].isna().any()
                                and not product_df[
                                    "Exports"
                                ].isna().any()
                            )

                            if not complete:

                                continue

                            product_data[
                                product
                            ] = (
                                float(
                                    product_df[
                                        "Imports"
                                    ].sum()
                                ),

                                float(
                                    product_df[
                                        "Exports"
                                    ].sum()
                                ),
                            )

                        stage_data[
                            stage
                        ] = product_data

                    else:

                        complete = (
                            not stage_df.empty
                            and stage_df[
                                "Participant"
                            ].nunique()
                            == len(
                                required_participants
                            )
                        )

                        stage_data[
                            stage
                        ] = (
                            stage_df
                            if complete
                            else None
                        )

                # -----------------------------------------------
                # COMMON PRODUCT COVERAGE
                # -----------------------------------------------

                if is_trade:

                    available_sets = [
                        set(product_data)
                        for product_data in (
                            stage_data.values()
                        )
                        if product_data
                    ]

                    if len(
                        available_sets
                    ) != len(
                        selected_stages
                    ):

                        incomplete_groups += 1

                        continue

                    common_products = (
                        set.intersection(
                            *available_sets
                        )
                    )

                    universe = set(
                        source_df.loc[
                            (
                                source_df[
                                    "Iteration"
                                ] == group_info[
                                    "Iteration"
                                ]
                            )
                            &
                            (
                                source_df[
                                    "Pathway"
                                ] == group_info[
                                    "Pathway"
                                ]
                            )
                            &
                            (
                                source_df[
                                    "Year"
                                ] == group_info[
                                    "Year"
                                ]
                            ),
                            "Product",
                        ].unique()
                    )

                    omitted_products += max(
                        0,
                        len(universe)
                        - len(common_products),
                    )

                    if not common_products:

                        incomplete_groups += 1

                        continue

                # -----------------------------------------------
                # CALCULATE INDICATORS
                # -----------------------------------------------

                for stage in selected_stages:

                    values = stage_data.get(
                        stage
                    )

                    if (
                        values is None
                        or (
                            is_trade
                            and not values
                        )
                    ):

                        incomplete_groups += 1

                        continue

                    if is_trade:

                        imports = sum(
                            values[product][0]
                            for product in common_products
                        )

                        exports = sum(
                            values[product][1]
                            for product in common_products
                        )

                        if indicator == "Total imports":

                            result = imports

                        elif indicator == "Total exports":

                            result = exports

                        else:

                            result = sum(
                                abs(
                                    values[product][0]
                                    - values[product][1]
                                )
                                for product in common_products
                            )

                        coverage = (
                            f"{len(common_products)}/"
                            f"{len(universe)} products"
                        )

                    else:

                        data = values.copy()

                        for column in [
                            "kcal_feas",
                            "kcal_mder",
                            "Agriculture",
                            "Land",
                        ]:

                            data[
                                column
                            ] = pd.to_numeric(
                                data[column],
                                errors="coerce",
                            )

                        if indicator.startswith(
                            "Food security"
                        ):

                            invalid = (
                                (
                                    data[
                                        "kcal_mder"
                                    ] <= 0
                                ).any()
                                or data[
                                    [
                                        "kcal_feas",
                                        "kcal_mder",
                                    ]
                                ].isna().any().any()
                            )

                            if invalid:

                                incomplete_groups += 1

                                continue

                            margins = (
                                (
                                    data[
                                        "kcal_feas"
                                    ]
                                    /
                                    data[
                                        "kcal_mder"
                                    ]
                                )
                                - 1.0
                            ) * 100.0

                            if (
                                indicator
                                == "Food security margin (%)"
                            ):

                                if len(data) != 1:

                                    incomplete_groups += 1

                                    continue

                                result = float(
                                    margins.iloc[0]
                                )

                            else:

                                result = float(
                                    margins.between(
                                        10, 50
                                    ).mean()
                                    * 100.0
                                )

                        else:

                            column = (
                                "Agriculture"
                                if indicator.startswith(
                                    "Agriculture"
                                )
                                else "Land"
                            )

                            if data[
                                column
                            ].isna().any():

                                incomplete_groups += 1

                                continue

                            result = (
                                float(
                                    data[
                                        column
                                    ].sum()
                                )
                                / 1000.0
                            )

                        coverage = (
                            f"{len(required_participants)} "
                            "participants"
                        )

                    output_rows.append({
                        **group_info,

                        "Stage": stage,

                        "Value": result,

                        "Coverage": coverage,
                    })

            # ====================================================
            # RESULTS
            # ====================================================

            plot_df = pd.DataFrame(
                output_rows
            )

            if plot_df.empty:

                st.warning(
                    "No complete comparison is available "
                    "for these filters. Try fewer stages, "
                    "a specific product or common participants."
                )

                return

            if omitted_products:

                st.warning(
                    f"{omitted_products} product-year "
                    "comparisons were excluded because "
                    "some calculated values were unavailable. "
                    "The displayed totals use the same "
                    "available products across compared stages."
                )

            if incomplete_groups:

                st.warning(
                    f"{incomplete_groups} incomplete "
                    "scope/stage checks were excluded. "
                    "Missing values were not treated as zero."
                )

            # ----------------------------------------------------
            # CHART CATEGORIES
            # ----------------------------------------------------

            if view == "Within iteration":

                if "Participant" in plot_df:

                    plot_df[
                        "Series"
                    ] = (
                        plot_df[
                            "Participant"
                        ].astype(str)
                        + " | "
                        + plot_df[
                            "Stage"
                        ]
                    )

                else:

                    plot_df[
                        "Series"
                    ] = plot_df[
                        "Stage"
                    ]

                plot_df[
                    "Category"
                ] = plot_df[
                    "Pathway"
                ]

            elif view == "Across iterations":

                if "Participant" in plot_df:

                    prefix = (
                        plot_df[
                            "Participant"
                        ].astype(str)
                        + " | "
                    )

                else:

                    prefix = ""

                plot_df[
                    "Series"
                ] = (
                    prefix
                    + plot_df[
                        "Pathway"
                    ]
                    + " | "
                    + plot_df[
                        "Stage"
                    ]
                )

                plot_df[
                    "Category"
                ] = plot_df[
                    "Iteration"
                ]

            else:

                plot_df[
                    "Series"
                ] = (
                    plot_df[
                        "Participant"
                    ].astype(str)
                    + " | "
                    + plot_df[
                        "Pathway"
                    ]
                    + " | "
                    + plot_df[
                        "Stage"
                    ]
                )

                plot_df[
                    "Category"
                ] = plot_df[
                    "Iteration"
                ]

            # ----------------------------------------------------
            # TIME SERIES MODE
            # ----------------------------------------------------

            if show_time_series:

                plot_df[
                    "Category"
                ] = plot_df[
                    "Year"
                ].astype(int)

                if view == "Within iteration":

                    if "Participant" in plot_df:

                        prefix = (
                            plot_df[
                                "Participant"
                            ].astype(str)
                            + " | "
                        )

                    else:

                        prefix = ""

                    plot_df[
                        "Series"
                    ] = (
                        prefix
                        + plot_df[
                            "Pathway"
                        ]
                        + " | "
                        + plot_df[
                            "Stage"
                        ]
                    )

                elif view == "Across iterations":

                    plot_df[
                        "Series"
                    ] = (
                        plot_df[
                            "Iteration"
                        ]
                        + " | "
                        + plot_df[
                            "Pathway"
                        ]
                        + " | "
                        + plot_df[
                            "Stage"
                        ]
                    )

                else:

                    plot_df[
                        "Series"
                    ] = (
                        plot_df[
                            "Iteration"
                        ]
                        + " | "
                        + plot_df[
                            "Series"
                        ]
                    )

            # ====================================================
            # PLOTTING
            # ====================================================

            st.markdown(
                "#### " + indicator
            )

            st.caption(
                f"{view} | "
                + (
                    "All available years"
                    if show_time_series
                    else str(selected_year)
                )
                + " | "
                + ", ".join(
                    selected_stages
                )
            )

            if chart_type != "Data table":

                chart_kwargs = {
                    "data_frame": plot_df,
                    "x": "Category",
                    "y": "Value",
                    "color": "Series",
                    "hover_data": [
                        "Coverage",
                        "Year",
                        "Iteration",
                        "Pathway",
                        "Stage",
                    ],
                    "labels": {
                        "Value": indicator,
                        "Category": (
                            "Year"
                            if show_time_series
                            else view
                        ),
                    },
                }

                # Consistent stage colors where practical.

                if (
                    view == "Within iteration"
                    and "Participant" not in plot_df
                ):

                    chart_kwargs[
                        "color_discrete_map"
                    ] = {
                        "Before trade adjustment": (
                            "#4169A1"
                        ),
                        "After trade adjustment": (
                            "#2C9077"
                        ),
                    }

                if chart_type == "Grouped bars":

                    figure = px.bar(
                        **chart_kwargs,
                        barmode="group",
                    )

                else:

                    figure = px.line(
                        **chart_kwargs,
                        markers=True,
                    )

                # -----------------------------------------------
                # SCIENTIFIC REFERENCE LINES
                # -----------------------------------------------

                if indicator == "Food security margin (%)":

                    for threshold in (
                        10, 50
                    ):

                        figure.add_hline(
                            y=threshold,
                            line_dash="dash",
                            opacity=0.55,
                        )

                elif indicator == (
                    "Food security target achievement (%)"
                ):

                    figure.add_hline(
                        y=100,
                        line_dash="dash",
                        opacity=0.55,
                    )

                elif (
                    not show_time_series
                    and int(selected_year) == 2050
                    and view != "Participant journey"
                    and len(selected_participants)
                        == len(all_participants)
                ):

                    if indicator.startswith(
                        "Agriculture GHG"
                    ):

                        figure.add_hline(
                            y=4.0,
                            line_dash="dash",
                            opacity=0.55,
                        )

                    elif indicator.startswith(
                        "Land-use change GHG"
                    ):

                        figure.add_hline(
                            y=-1.0,
                            line_dash="dash",
                            opacity=0.55,
                        )

                figure.update_layout(
                    template="plotly_white",
                    height=520,
                    hovermode="x unified",
                    legend_title_text="",
                    margin={
                        "l": 10,
                        "r": 10,
                        "t": 30,
                        "b": 75,
                    },
                )

                st.plotly_chart(
                    figure,
                    use_container_width=True,
                    key="pe22_main_chart",
                )

            # ====================================================
            # DATA TABLE + EXPORT
            # ====================================================

            display_columns = [
                "Iteration",
                "Pathway",
                "Stage",
                "Year",
            ]

            if "Participant" in plot_df:

                display_columns.append(
                    "Participant"
                )

            display_columns += [
                "Value",
                "Coverage",
            ]

            result_table = (
                plot_df[
                    display_columns
                ].sort_values(
                    [
                        "Iteration",
                        "Pathway",
                        "Year",
                        "Stage",
                    ]
                )
            )

            st.dataframe(
                result_table,
                hide_index=True,
                use_container_width=True,
            )

            st.download_button(
                "Download comparison CSV",
                data=(
                    result_table.to_csv(
                        index=False
                    ).encode(
                        "utf-8-sig"
                    )
                ),
                file_name=(
                    "scenathon_pathway_iteration_comparison.csv"
                ),
                mime="text/csv",
                key="pe22_download",
            )

            st.caption(
                "Trade quantities retain their source units. "
                "Food security target: 10%-50% above minimum "
                "dietary energy requirement in 2030 and 2050. "
                "Agriculture GHG target: at most 4 Gt CO2e "
                "in 2050. Land-use GHG target: at most "
                "-1 Gt CO2e in 2050. "
                "Interpret global totals using the "
                "selected participant coverage."
            )

        render_pathway_iteration_evolution()

        st.divider()

        # SECTION29_LEGACY_STUDIO_REMOVED
        # One unified comparison workspace above.

        # SECTION28_CALCULATOR_COMPARISON

        st.divider()

        st.subheader("Trade Analytics")

        st.caption(
            "Compare imports, exports and trade balance "
            "using submitted FABLE-C calculator results "
            "before and after trade adjustment."
        )

        configs = list_iteration_configs()

        iterations = [
            str(c["iteration"])
            for c in configs
            if str(c.get("iteration", "")).strip()
        ]

        if not iterations:

            st.info(
                "No iteration is available."
            )

        else:

            c1, c2, c3 = st.columns(
                [1, 1.5, 1]
            )

            with c1:

                iteration = st.selectbox(
                    "Iteration",
                    iterations,
                    key="sc28_iteration",
                )

            config = (
                load_iteration_config(iteration)
                or {}
            )

            pathways = [
                str(p).strip()
                for p in config.get(
                    "pathways", []
                )
                if str(p).strip()
            ]

            with c2:

                selected_pathway = st.selectbox(
                    "Pathway",
                    pathways,
                    key="sc29_trade_pathway",
                )

                selected_pathways = (
                    [selected_pathway]
                    if selected_pathway
                    else []
                )

            with c3:

                st.write("")

                refresh = st.button(
                    "Load / Refresh",
                    type="primary",
                    key="sc28_refresh",
                    use_container_width=True,
                )

            snapshot_key = (
                iteration,
                tuple(sorted(selected_pathways)),
            )

            snapshot = st.session_state.get(
                "sc28_trade_snapshot"
            )

            # ================================================
            # LOAD BEFORE AND AFTER WORKBOOK SUBMISSIONS
            # ================================================

            if refresh:

                records = []
                warnings = []

                expected_participants = [
                    str(p).strip()
                    for p in config.get(
                        "participants", []
                    )
                    if str(p).strip()
                ]

                stage_labels = {
                    "before": "Before trade adjustment",
                    "after": "After trade adjustment",
                }

                with st.spinner(
                    "Loading submitted calculator results..."
                ):

                    for stage, label in (
                        stage_labels.items()
                    ):

                        try:

                            payloads = (
                                list_submission_payloads(
                                    iteration,
                                    stage=stage,
                                )
                            )

                        except Exception as exc:

                            warnings.append(
                                f"{label}: {exc}"
                            )

                            continue

                        for payload in payloads:

                            participant = str(
                                payload.get(
                                    "participant", ""
                                )
                            ).strip()

                            pathway = str(
                                payload.get(
                                    "pathway", ""
                                )
                            ).strip()

                            if (
                                participant not in expected_participants
                                or pathway not in selected_pathways
                            ):
                                continue

                            for row in payload.get(
                                "rows", []
                            ):

                                records.append({
                                    "Iteration": iteration,
                                    "Pathway": pathway,
                                    "Participant": participant,
                                    "Stage": label,

                                    "Product": str(
                                        row.get(
                                            "Product", ""
                                        )
                                    ).strip().casefold(),

                                    "Year": row.get(
                                        "Year"
                                    ),

                                    "Imports": row.get(
                                        "Import_quantity"
                                    ),

                                    "Exports": row.get(
                                        "Export_quantity"
                                    ),
                                })

                frame = pd.DataFrame(records)

                if not frame.empty:

                    for col in [
                        "Year",
                        "Imports",
                        "Exports",
                    ]:

                        frame[col] = pd.to_numeric(
                            frame[col],
                            errors="coerce",
                        )

                    frame = frame.dropna(
                        subset=["Year"]
                    )

                    frame["Year"] = (
                        frame["Year"].astype(int)
                    )

                    duplicate = frame.duplicated(
                        subset=[
                            "Pathway",
                            "Participant",
                            "Stage",
                            "Product",
                            "Year",
                        ],
                        keep=False,
                    )

                    if duplicate.any():

                        raise ValueError(
                            "Duplicate product/year records "
                            "found in submitted calculators."
                        )

                snapshot = {
                    "key": snapshot_key,
                    "data": frame,
                    "warnings": warnings,
                    "expected": expected_participants,
                }

                st.session_state[
                    "sc28_trade_snapshot"
                ] = snapshot

            if not selected_pathways:

                st.info(
                    "Select at least one pathway."
                )

            elif (
                snapshot is None
                or snapshot.get("key") != snapshot_key
            ):

                st.info(
                    "Click Load / Refresh to display "
                    "trade analytics."
                )

            else:

                df = snapshot["data"].copy()

                for message in snapshot["warnings"]:
                    st.warning(message)

                if df.empty:

                    st.info(
                        "No matching calculator reports "
                        "were found."
                    )

                else:

                    # ========================================
                    # FILTERS
                    # ========================================

                    products = sorted(
                        df["Product"]
                        .dropna()
                        .unique()
                        .tolist()
                    )

                    f1, f2 = st.columns(
                        [1.8, 1]
                    )

                    with f1:

                        selected_product = st.selectbox(
                            "Product",
                            ["All products"] + products,
                            key="sc28_product",
                        )

                    with f2:

                        years = sorted(
                            df["Year"].unique().tolist()
                        )

                        selected_year = st.selectbox(
                            "Focus year",
                            years,
                            index=(
                                years.index(2050)
                                if 2050 in years
                                else len(years) - 1
                            ),
                            key="sc28_year",
                        )


                    if (
                        selected_product
                        != "All products"
                    ):

                        df = df[
                            df["Product"]
                            == selected_product
                        ].copy()

                    # ========================================
                    # PAIRED DATA VALIDATION
                    # ========================================

                    expected = set(
                        snapshot["expected"]
                    )

                    stages = [
                        "Before trade adjustment",
                        "After trade adjustment",
                    ]

                    # A product-year-pathway is comparable
                    # only when both stages have complete
                    # reports from all configured participants.

                    summed = {}

                    for identity, group in df.groupby(
                        [
                            "Pathway",
                            "Product",
                            "Year",
                            "Stage",
                        ],
                        dropna=False,
                    ):

                        pathway, product, year, stage = identity

                        complete = (
                            set(group["Participant"])
                            == expected
                            and len(group) == len(expected)
                            and group[
                                ["Imports", "Exports"]
                            ].notna().all().all()
                        )

                        if not complete:
                            continue

                        summed[identity] = (
                            float(group["Imports"].sum()),
                            float(group["Exports"].sum()),
                        )

                    # Keep the same product set
                    # for every displayed year and stage.

                    common = {}

                    for pathway in selected_pathways:

                        universe = set(
                            df.loc[
                                df["Pathway"] == pathway,
                                "Product",
                            ]
                        )

                        paired = set(universe)

                        for year in years:

                            for stage in stages:

                                available_products = {
                                    product
                                    for (
                                        pw, product, yr, stg
                                    ) in summed
                                    if (
                                        pw == pathway
                                        and yr == year
                                        and stg == stage
                                    )
                                }

                                paired &= available_products

                        common[pathway] = paired

                        if len(paired) < len(universe):

                            st.warning(
                                f"{pathway}: "
                                f"{len(paired)}/{len(universe)} "
                                "products have complete matching "
                                "Before and After reports across "
                                "all displayed years. "
                                "Charts use only this common set."
                            )

                    # ========================================
                    # YEARLY TRADE TOTALS AND GAPS
                    # ========================================

                    annual_rows = []
                    product_rows = []

                    for pathway in selected_pathways:

                        paired_products = common[
                            pathway
                        ]

                        for year in years:

                            for stage in stages:

                                if not paired_products:
                                    continue

                                imports = sum(
                                    summed[
                                        (
                                            pathway,
                                            p,
                                            year,
                                            stage,
                                        )
                                    ][0]
                                    for p in paired_products
                                )

                                exports = sum(
                                    summed[
                                        (
                                            pathway,
                                            p,
                                            year,
                                            stage,
                                        )
                                    ][1]
                                    for p in paired_products
                                )

                                gap = sum(
                                    abs(
                                        summed[
                                            (
                                                pathway,
                                                p,
                                                year,
                                                stage,
                                            )
                                        ][0]
                                        -
                                        summed[
                                            (
                                                pathway,
                                                p,
                                                year,
                                                stage,
                                            )
                                        ][1]
                                    )
                                    for p in paired_products
                                )

                                annual_rows.append({
                                    "Pathway": pathway,
                                    "Year": year,
                                    "Stage": stage,
                                    "Imports": imports,
                                    "Exports": exports,
                                    "Product-level gap": gap,
                                    "Products compared": (
                                        len(paired_products)
                                    ),
                                })

                        # Product changes in selected year

                        for product in paired_products:

                            before_i, before_e = summed[
                                (
                                    pathway,
                                    product,
                                    selected_year,
                                    stages[0],
                                )
                            ]

                            after_i, after_e = summed[
                                (
                                    pathway,
                                    product,
                                    selected_year,
                                    stages[1],
                                )
                            ]

                            product_rows.append({
                                "Pathway": pathway,
                                "Product": product,
                                "Import change": (
                                    after_i - before_i
                                ),
                                "Export change": (
                                    after_e - before_e
                                ),
                            })

                    annual = pd.DataFrame(
                        annual_rows
                    )

                    changes = pd.DataFrame(
                        product_rows
                    )

                    if annual.empty:

                        st.warning(
                            "No complete Before/After comparison "
                            "is available for this scope."
                        )

                    else:

                        colors = {
                            "Before trade adjustment": (
                                "#406EA5"
                            ),
                            "After trade adjustment": (
                                "#28886E"
                            ),
                        }

                        # SECTION29_TRADE_SUMMARY

                        _s29_focus = annual.loc[
                            (
                                annual["Pathway"]
                                == selected_pathway
                            )
                            &
                            (
                                annual["Year"]
                                == selected_year
                            )
                        ]

                        _s29_gaps = dict(
                            zip(
                                _s29_focus["Stage"],
                                _s29_focus[
                                    "Product-level gap"
                                ],
                            )
                        )

                        _s29_before_gap = (
                            _s29_gaps.get(
                                "Before trade adjustment"
                            )
                        )

                        _s29_after_gap = (
                            _s29_gaps.get(
                                "After trade adjustment"
                            )
                        )

                        st.markdown(
                            f"#### Trade balance summary "
                            f"- {selected_year}"
                        )

                        if (
                            _s29_before_gap is not None
                            and _s29_after_gap is not None
                        ):

                            _s29_cols = st.columns(3)

                            _s29_cols[0].metric(
                                "Before adjustment",
                                f"{_s29_before_gap:,.0f}",
                            )

                            _s29_cols[1].metric(
                                "After adjustment",
                                f"{_s29_after_gap:,.0f}",
                            )

                            if _s29_before_gap > 0:

                                _s29_pct = (
                                    (
                                        _s29_before_gap
                                        - _s29_after_gap
                                    )
                                    / _s29_before_gap
                                    * 100.0
                                )

                                _s29_cols[2].metric(
                                    "Imbalance reduction",
                                    f"{_s29_pct:+.1f}%",
                                )

                            else:

                                _s29_cols[2].metric(
                                    "Imbalance reduction",
                                    "N/A",
                                )

                            st.caption(
                                f"{selected_pathway} | "
                                f"{len(common[selected_pathway])} "
                                "matched products | "
                                f"{len(snapshot['expected'])} "
                                "configured participants. "
                                "Calculated as the sum of "
                                "absolute global product gaps."
                            )

                        # ================================
                        # PLOT 1 - IMPORT AND EXPORT TRENDS
                        # ================================

                        st.markdown(
                            "### 1. Import and Export Trends"
                        )

                        st.caption(
                            "Annual imports and exports from "
                            "submitted calculators before "
                            "and after trade adjustment."
                        )

                        trajectory = annual[
                            annual["Pathway"]
                            == selected_pathway
                        ].copy()

                        trajectory_long = trajectory.melt(
                            id_vars=["Year", "Stage"],
                            value_vars=[
                                "Imports",
                                "Exports",
                            ],
                            var_name="Trade flow",
                            value_name="Quantity",
                        )

                        trajectory_long["Series"] = (
                            trajectory_long["Trade flow"]
                            + " - "
                            + trajectory_long["Stage"].map({
                                "Before trade adjustment": (
                                    "Before"
                                ),
                                "After trade adjustment": (
                                    "After"
                                ),
                            })
                        )

                        fig1 = px.line(
                            trajectory_long,
                            x="Year",
                            y="Quantity",
                            color="Series",
                            markers=True,
                            color_discrete_map={
                                "Imports - Before": "#4474A5",
                                "Exports - Before": "#779BC6",
                                "Imports - After": "#236F5C",
                                "Exports - After": "#43A384",
                            },
                        )

                        # Before = dashed, After = solid.

                        for _s29_trace in fig1.data:

                            _s29_trace.line.dash = (
                                "dash"
                                if "Before" in _s29_trace.name
                                else "solid"
                            )

                            _s29_trace.line.width = 2.6

                        fig1.update_layout(
                            template="plotly_white",
                            height=450,
                            legend_title_text="",
                            hovermode="x unified",
                            xaxis_title="Year",
                            yaxis_title=(
                                "Reported trade quantity"
                            ),
                        )

                        # SECTION30_FIXED_VISUAL_POLISH : fig1
                        fig1.update_layout(
                            height=520,
                            margin=dict(l=12, r=12, t=45, b=115),
                            legend=dict(
                                orientation="h", x=0.5, xanchor="center",
                                y=-0.18, yanchor="top",
                                font=dict(size=11, color="#182438"),
                                title=None,
                            ),
                        )

                        st.plotly_chart(
                            fig1,
                            use_container_width=True,
                            key="sc28_trends",
                        )

                        # ================================
                        # PLOT 2 - PRODUCT-LEVEL TRADE GAP
                        # ================================

                        st.markdown(
                            "### 2. Product-Level Trade Imbalance"
                        )

                        st.caption(
                            "Sum of absolute product-level "
                            "import-export gaps. Lower values "
                            "indicate a smaller imbalance."
                        )

                        gap_data = annual[
                            annual["Pathway"]
                            == selected_pathway
                        ]

                        fig2 = px.bar(
                            gap_data,
                            x="Year",
                            y="Product-level gap",
                            color="Stage",
                            barmode="group",
                            color_discrete_map=colors,
                            hover_data=[
                                "Products compared"
                            ],
                        )

                        fig2.update_layout(
                            template="plotly_white",
                            height=430,
                            legend_title_text="",
                            yaxis_title=(
                                "Absolute trade gap "
                                "(reported units)"
                            ),
                        )

                        # SECTION30_FIXED_VISUAL_POLISH : fig2
                        fig2.update_layout(
                            height=490,
                            margin=dict(l=12, r=12, t=45, b=100),
                            legend=dict(
                                orientation="h", x=0.5, xanchor="center",
                                y=-0.18, yanchor="top",
                                font=dict(size=11, color="#182438"),
                                title=None,
                            ),
                        )

                        st.plotly_chart(
                            fig2,
                            use_container_width=True,
                            key="sc28_gap",
                        )

                        # ================================
                        # PLOT 3 - TRADE CHANGES BY PRODUCT
                        # ================================

                        st.markdown(
                            "### 3. Changes in Trade by Product"
                        )

                        st.caption(
                            "Difference between After and "
                            "Before calculator results "
                            f"for {selected_year}."
                        )

                        if selected_product == "All products":

                            top_n = st.slider(
                                "Number of products",
                                min_value=5,
                                max_value=25,
                                value=10,
                                key="sc28_top_n",
                            )

                        else:

                            top_n = 1

                        filtered_changes = changes[
                            changes["Pathway"]
                            == selected_pathway
                        ].copy()

                        if filtered_changes.empty:

                            st.info(
                                "No matched product changes "
                                "are available."
                            )

                        else:

                            filtered_changes[
                                "Impact"
                            ] = (
                                filtered_changes[
                                    "Import change"
                                ].abs()
                                +
                                filtered_changes[
                                    "Export change"
                                ].abs()
                            )

                            filtered_changes = (
                                filtered_changes.sort_values(
                                    "Impact",
                                    ascending=False,
                                ).head(top_n)
                            )

                            change_long = (
                                filtered_changes.melt(
                                    id_vars=[
                                        "Product"
                                    ],
                                    value_vars=[
                                        "Import change",
                                        "Export change",
                                    ],
                                    var_name="Trade flow",
                                    value_name="Change",
                                )
                            )

                            change_long[
                                "Product label"
                            ] = (
                                change_long[
                                    "Product"
                                ].astype(str)
                                .str.replace(
                                    "_",
                                    " ",
                                    regex=False,
                                )
                                .str.title()
                            )

                            fig3 = px.bar(
                                change_long,
                                x="Change",
                                y="Product label",
                                color="Trade flow",
                                orientation="h",
                                barmode="group",
                                color_discrete_map={
                                    "Import change": "#4474A5",
                                    "Export change": "#28886E",
                                },
                            )

                            fig3.update_layout(
                                template="plotly_white",
                                height=480,
                                legend_title_text="",
                                yaxis={
                                    "categoryorder": (
                                        "total ascending"
                                    )
                                },
                            )

                            # Show increase and decrease
                            # on a balanced horizontal axis.

                            _s29_lim = max(
                                abs(
                                    float(
                                        change_long[
                                            "Change"
                                        ].min()
                                    )
                                ),
                                abs(
                                    float(
                                        change_long[
                                            "Change"
                                        ].max()
                                    )
                                ),
                                1.0,
                            ) * 1.15

                            fig3.update_xaxes(
                                range=[
                                    -_s29_lim,
                                    _s29_lim,
                                ]
                            )

                            fig3.add_vline(
                                x=0,
                                line_color="#444444",
                            )

                            # SECTION30_FIXED_VISUAL_POLISH : fig3
                            fig3.update_layout(
                                height=535,
                                margin=dict(l=12, r=12, t=45, b=110),
                                legend=dict(
                                    orientation="h", x=0.5, xanchor="center",
                                    y=-0.2, yanchor="top",
                                    font=dict(size=11, color="#182438"),
                                    title=None,
                                ),
                            )

                            st.plotly_chart(
                                fig3,
                                use_container_width=True,
                                key="sc28_product_changes",
                            )

                        # ================================
                        # DATA TABLE AND EXPORT
                        # ================================

                        with st.expander(
                            "View data and download CSV"
                        ):

                            st.dataframe(
                                annual,
                                hide_index=True,
                                use_container_width=True,
                            )

                            st.download_button(
                                "Download trade comparison",
                                data=annual.to_csv(
                                    index=False
                                ).encode(
                                    "utf-8-sig"
                                ),
                                file_name=(
                                    "scenathon_trade_before_after.csv"
                                ),
                                mime="text/csv",
                                key="sc28_download",
                            )

                        st.caption(
                            "All comparisons are based on "
                            "submitted FABLE-C calculator "
                            "results. Trade adjustment "
                            "calculation outputs are not "
                            "used as a separate chart series."
                        )


    _render_analytics_fragment()


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
                "Control Center",
                "Submission Progress",
                "Storage",
            ]
        )


        # =================================================
        # =================================================
        # COMPACT CONTROL CENTER REPLACEMENT


        # ====================================================
        # COMPACT CONTROL CENTER 12B
        # ====================================================



        # ====================================================
        # SCENATHON - SELECT / CREATE / EDIT / ACTIVATE
        # Editing forms are displayed only on demand.
        # ====================================================

        with config_tab:

            st.markdown("#### 1. Scenathon")

            cc_sessions = {
                item["id"]: item
                for item in list_scenathon_sessions()
            }

            sid_options = list(cc_sessions)

            published_sid = (
                get_published_active_scenathon()
            )

            # Apply newly created session selection
            # before rendering the selection widget.

            next_sid = st.session_state.pop(
                "_cc12d_select_new_sid",
                None,
            )

            if next_sid in sid_options:

                st.session_state[
                    "cc12d_session_picker"
                ] = next_sid

            elif (
                st.session_state.get(
                    "cc12d_session_picker"
                ) not in sid_options
            ):

                st.session_state.pop(
                    "cc12d_session_picker",
                    None,
                )

            choose_col, new_col, edit_col, close_col = (
                st.columns(
                    [5, 2, 2, 2],
                    gap="small",
                    vertical_alignment="bottom",
                )
            )

            # -----------------------------------------------
            # SCENATHON SELECTION
            # -----------------------------------------------

            with choose_col:

                chosen_sid = st.selectbox(
                    "Choose Scenathon",
                    options=sid_options,
                    index=(
                        sid_options.index(published_sid)
                        if published_sid in sid_options
                        else 0
                    ),
                    format_func=lambda sid: (
                        f"{cc_sessions[sid].get('name', sid)} "
                        f"({cc_sessions[sid].get('status', '')})"
                    ),
                    placeholder="Create a Scenathon",
                    key="cc12d_session_picker",
                ) if sid_options else None

            # -----------------------------------------------
            # CREATE SCENATHON
            # -----------------------------------------------

            with new_col:

                with st.popover(
                    "+ New",
                    use_container_width=True,
                ):

                    with st.form(
                        "cc12d_new_session"
                    ):

                        new_name = st.text_input(
                            "Scenathon name"
                        )

                        new_day = st.date_input(
                            "Date"
                        )

                        new_time = st.time_input(
                            "Time"
                        )

                        make_session = (
                            st.form_submit_button(
                                "Create and activate"
                            )
                        )

                    if make_session:

                        try:

                            item = create_scenathon_session(
                                new_name,
                                new_day,
                                new_time,
                            )

                            set_published_active_scenathon(
                                item["id"]
                            )

                            st.session_state[
                                "_cc12d_select_new_sid"
                            ] = item["id"]

                            st.cache_data.clear()

                            st.success(
                                "Scenathon created and activated. "
                                "Add an iteration below."
                            )

                            st.rerun()

                        except Exception as exc:

                            st.error(str(exc))

            selected_session = cc_sessions.get(
                chosen_sid
            )

            # -----------------------------------------------
            # EDIT SCENATHON
            # -----------------------------------------------

            with edit_col:

                with st.popover(
                    "✏ Edit",
                    use_container_width=True,
                    disabled=not bool(selected_session),
                ):

                    if selected_session:

                        from datetime import date, time

                        try:

                            day_value = date.fromisoformat(
                                selected_session.get(
                                    "date", ""
                                )
                            )

                        except (ValueError, TypeError):

                            day_value = date.today()

                        try:

                            time_value = time.fromisoformat(
                                selected_session.get(
                                    "time", ""
                                )
                            )

                        except (ValueError, TypeError):

                            time_value = time(10, 0)

                        with st.form(
                            f"cc12d_edit_session_{chosen_sid}"
                        ):

                            edit_name = st.text_input(
                                "Name",
                                value=selected_session.get(
                                    "name", ""
                                ),
                            )

                            edit_day = st.date_input(
                                "Date",
                                value=day_value,
                            )

                            edit_time = st.time_input(
                                "Time",
                                value=time_value,
                            )

                            save_session = (
                                st.form_submit_button(
                                    "Save changes"
                                )
                            )

                        if save_session:

                            try:

                                cc12b_edit_session(
                                    chosen_sid,
                                    edit_name,
                                    edit_day,
                                    edit_time,
                                )

                                st.cache_data.clear()

                                st.success(
                                    "Scenathon updated"
                                )

                                st.rerun()

                            except Exception as exc:

                                st.error(str(exc))

            # -----------------------------------------------
            # CLOSE SCENATHON
            # -----------------------------------------------

            with close_col:

                with st.popover(
                    "Close",
                    use_container_width=True,
                    disabled=not (
                        selected_session
                        and selected_session.get("status")
                        == "OPEN"
                    ),
                ):

                    st.caption(
                        "Close submissions for this Scenathon?"
                    )

                    confirmed_close = st.checkbox(
                        "Yes, close it",
                        key="cc12d_confirm_close",
                    )

                    if st.button(
                        "Confirm close",
                        disabled=not confirmed_close,
                        key="cc12d_close",
                    ):

                        try:

                            close_scenathon_session(
                                chosen_sid
                            )

                            if published_sid == chosen_sid:

                                set_published_active_scenathon(
                                    "__no_active__"
                                )

                            st.cache_data.clear()

                            st.success(
                                "Scenathon closed"
                            )

                            st.rerun()

                        except Exception as exc:

                            st.error(str(exc))


            # SECTION20_REOPEN_CONTROL
            if (
                selected_session
                and selected_session.get("status") == "CLOSED"
            ):

                st.info(
                    "This Scenathon is closed. "
                    "You can reopen the same Scenathon "
                    "without deleting its saved data."
                )

                confirm_reopen = st.checkbox(
                    "Reopen this Scenathon and allow "
                    "submissions to its OPEN iterations",
                    value=False,
                    key=(
                        "sc20_reopen_"
                        + safe_name(chosen_sid)
                    ),
                )

                if st.button(
                    "Reopen & Set Active",
                    type="primary",
                    disabled=not confirm_reopen,
                    key=(
                        "sc20_reopen_btn_"
                        + safe_name(chosen_sid)
                    ),
                ):

                    try:

                        reopen_scenathon_session(
                            chosen_sid
                        )

                        set_published_active_scenathon(
                            chosen_sid
                        )

                        st.cache_data.clear()

                        st.success(
                            "Scenathon reopened and activated."
                        )

                        st.rerun()

                    except Exception as exc:

                        st.error(
                            "Could not reopen Scenathon: "
                            f"{exc}"
                        )


            # -----------------------------------------------
            # ACTIVE SCENATHON STATUS
            # -----------------------------------------------

            if selected_session:

                details = selected_session

                live = (
                    chosen_sid == published_sid
                    and details.get("status") == "OPEN"
                )

                st.caption(
                    f"{details.get('date', '')}  |  "
                    f"{details.get('time', '')}  |  "
                    + (
                        "ACTIVE - submissions enabled"
                        if live
                        else "Not active for submissions"
                    )
                )

                if (
                    details.get("status") == "OPEN"
                    and not live
                ):

                    if st.button(
                        "Set as active Scenathon",
                        key="cc12d_activate",
                    ):

                        try:

                            set_published_active_scenathon(
                                chosen_sid
                            )

                            st.cache_data.clear()

                            st.success(
                                "Scenathon is now active"
                            )

                            st.rerun()

                        except Exception as exc:

                            st.error(str(exc))

            else:

                st.info(
                    "Create a Scenathon first, "
                    "then add an iteration."
                )



        # ====================================================
        # ITERATION - SELECT / CREATE / EDIT / GO LIVE
        # ====================================================

        with config_tab:

            st.markdown("#### 2. Iteration")

            if not chosen_sid:

                st.caption(
                    "Choose or create a Scenathon above."
                )

            else:

                iteration_map = (
                    cc12b_iteration_configs(
                        chosen_sid
                    )
                )

                choices = [
                    "+ New iteration"
                ] + sorted(iteration_map)

                iter_key = (
                    f"cc12d_iteration_picker_{chosen_sid}"
                )

                next_iter = st.session_state.pop(
                    "_cc12d_select_new_iter",
                    None,
                )

                if (
                    isinstance(next_iter, tuple)
                    and next_iter[0] == chosen_sid
                    and next_iter[1] in choices
                ):

                    st.session_state[
                        iter_key
                    ] = next_iter[1]

                elif st.session_state.get(iter_key) not in choices:

                    st.session_state.pop(
                        iter_key,
                        None,
                    )

                iter_col, configure_col = (
                    st.columns(
                        [5, 2],
                        gap="small",
                        vertical_alignment="bottom",
                    )
                )

                # -------------------------------------------
                # ITERATION SELECTION
                # -------------------------------------------

                with iter_col:

                    chosen_iter = st.selectbox(
                        "Choose iteration",
                        choices,
                        key=iter_key,
                    )

                is_new = (
                    chosen_iter == "+ New iteration"
                )

                original_name = (
                    "" if is_new else chosen_iter
                )

                original_cfg = (
                    {}
                    if is_new
                    else iteration_map[original_name][1]
                )

                can_edit = bool(
                    selected_session
                    and selected_session.get("status")
                    == "OPEN"
                )

                # -------------------------------------------
                # CREATE OR EDIT ITERATION
                # -------------------------------------------

                with configure_col:

                    with st.popover(
                        (
                            "+ Create"
                            if is_new
                            else "✏ Edit"
                        ),
                        use_container_width=True,
                        disabled=not can_edit,
                    ):

                        with st.form(
                            f"cc12d_iter_form_"
                            f"{chosen_sid}_"
                            f"{safe_name(chosen_iter)}"
                        ):

                            name_val = st.text_input(
                                "Iteration name",
                                value=original_name,
                                placeholder="Iteration 1",
                            )

                            participant_val = st.text_area(
                                "Participants - one per line",
                                value="\n".join(
                                    original_cfg.get(
                                        "participants", []
                                    )
                                ),
                                height=110,
                            )

                            pathway_val = st.text_area(
                                "Pathways - one per line",
                                value="\n".join(
                                    original_cfg.get(
                                        "pathways",
                                        DEFAULT_PATHWAYS,
                                    )
                                ),
                                height=110,
                            )

                            open_val = st.checkbox(
                                "Live - allow submissions",
                                value=(
                                    original_cfg.get(
                                        "status", "OPEN"
                                    ) == "OPEN"
                                ),
                            )

                            save_iter = (
                                st.form_submit_button(
                                    "Create iteration"
                                    if is_new
                                    else "Save iteration"
                                )
                            )

                        if save_iter:

                            try:

                                cc12b_save_iteration(
                                    chosen_sid,
                                    original_name,
                                    name_val,
                                    participant_val,
                                    pathway_val,
                                    open_val,
                                )

                                # Publish OPEN session for users.

                                if open_val:

                                    set_published_active_scenathon(
                                        chosen_sid
                                    )

                                st.session_state[
                                    "_cc12d_select_new_iter"
                                ] = (
                                    chosen_sid,
                                    name_val.strip(),
                                )

                                st.cache_data.clear()

                                st.success(
                                    "Iteration saved. "
                                    + (
                                        "Submit Report is live."
                                        if open_val
                                        else
                                        "Submissions are closed "
                                        "for this iteration."
                                    )
                                )

                                st.rerun()

                            except Exception as exc:

                                st.error(str(exc))

                # -------------------------------------------
                # ITERATION SUMMARY
                # -------------------------------------------

                if not is_new:

                    live = (
                        selected_session.get("status")
                        == "OPEN"
                        and original_cfg.get("status")
                        == "OPEN"
                        and get_published_active_scenathon()
                        == chosen_sid
                    )

                    st.caption(
                        f"{len(original_cfg.get('participants', []))} "
                        "participants  |  "
                        f"{len(original_cfg.get('pathways', []))} "
                        "pathways  |  "
                        + (
                            "LIVE - available in Submit Report"
                            if live
                            else "Not live"
                        )
                    )

                elif can_edit:

                    st.caption(
                        "Click + Create to enter "
                        "participants and pathways."
                    )

                else:

                    st.warning(
                        "This Scenathon is closed. "
                        "Select or create an OPEN Scenathon "
                        "to add iterations."
                    )


        # ====================================================
        # CC12C_CLEAR_BUTTON
        # Single-page Admin Control Center
        # ====================================================

        with config_tab:

            st.divider()

            c_left, c_right = st.columns(
                [2, 7],
                vertical_alignment="center",
            )

            with c_left:

                if st.button(
                    "🗑 Clear Data",
                    type="primary",
                    key="cc12c_open_clear",
                ):

                    cc12c_clear_dialog()

            with c_right:

                st.caption(
                    "Scenathon / Iteration / Pathway / "
                    "Entire storage - password protected"
                )

            if st.session_state.get(
                "cc12c_last_message"
            ):

                st.success(
                    st.session_state[
                        "cc12c_last_message"
                    ]
                )

                if st.button(
                    "Dismiss",
                    key="cc12c_dismiss",
                ):

                    st.session_state.pop(
                        "cc12c_last_message",
                        None,
                    )

                    st.rerun()

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

        # ====================================================
        # CC12C_STORAGE_READONLY
        # Saved data, archives and reporting exports
        # ====================================================

        with storage_tab:

            st.markdown(
                "#### Saved Data and Archives"
            )

            st.caption(
                f"Repository: {GITHUB_OWNER}/{GITHUB_REPO} "
                f"- branch: {GITHUB_BRANCH}"
            )

            st.caption(
                "Read-only inventory. "
                "Clear Data is in Admin - Control Center."
            )

            if st.button(
                "Refresh saved data",
                key="cc12c_storage_refresh",
            ):

                try:

                    st.session_state[
                        "cc12c_storage_inventory"
                    ] = cc12c_inventory()

                except Exception as exc:

                    st.error(
                        f"Could not read storage: {exc}"
                    )

            saved = st.session_state.get(
                "cc12c_storage_inventory"
            )

            if saved is not None:

                archived = [
                    p for p in saved
                    if "/archives/" in p
                ]

                others = [
                    p for p in saved
                    if "/archives/" not in p
                ]

                a, b, c = st.columns(3)

                a.metric(
                    "All files",
                    len(saved),
                )

                b.metric(
                    "Current files",
                    len(others),
                )

                c.metric(
                    "Archive files",
                    len(archived),
                )

                if not saved:

                    st.info(
                        "Storage is empty. "
                        "No saved Scenathon data."
                    )

                else:

                    with st.expander(
                        "Saved data - file paths"
                    ):

                        st.code(
                            "\n".join(
                                sorted(others)
                            ) or "(none)",
                            language="text",
                        )

                    with st.expander(
                        "Archives - file paths"
                    ):

                        st.code(
                            "\n".join(
                                sorted(archived)
                            ) or "(none)",
                            language="text",
                        )

            else:

                st.info(
                    "Click Refresh saved data "
                    "to view GitHub storage and archives."
                )

            # -----------------------------------------
            # Completed Scenathon CSV exports
            # -----------------------------------------

            with st.expander(
                "Completed Scenathon CSV exports"
            ):

                closed = {
                    item["id"]: item.get(
                        "name",
                        item["id"],
                    )
                    for item in list_scenathon_sessions()
                    if item.get("status") == "CLOSED"
                }

                if not closed:

                    st.caption(
                        "No closed Scenathon "
                        "available for export."
                    )

                else:

                    export_id = st.selectbox(
                        "Closed Scenathon",
                        list(closed),
                        format_func=lambda sid: closed[sid],
                        key="cc12c_closed_export",
                    )

                    if st.button(
                        "Prepare reporting CSVs",
                        key="cc12c_prepare_csv",
                    ):

                        st.session_state[
                            "cc12c_download_session"
                        ] = export_id

                    if (
                        st.session_state.get(
                            "cc12c_download_session"
                        ) == export_id
                    ):

                        render_scenathon_exports(
                            export_id,
                            key_prefix="cc12c_admin",
                        )

