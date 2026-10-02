
from __future__ import annotations

import base64
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
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
    "fable-local-admin",
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


def save_submission(
    iteration: str,
    username: str,
    participant: str,
    pathway: str,
    df: pd.DataFrame,
):

    clean_df = df[
        REQUIRED_COLUMNS
    ].copy()

    # Convert through JSON to guarantee
    # normal Python JSON-compatible values.
    rows = json.loads(
        clean_df.to_json(
            orient="records"
        )
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
        "rows": rows,
    }

    path = submission_path(
        iteration,
        participant,
        pathway,
    )

    github_put_json(
        path,
        payload,
        (
            "Submit SCENATHON report: "
            f"{iteration} / "
            f"{participant} / "
            f"{pathway}"
        ),
    )

    return path


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
# TRADE ENGINE — BUILD INPUT
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
# TRADE ENGINE — RUN + PUBLISH RESULTS
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


    # --------------------------------------------------------
    # strict=False:
    #
    # Admin is allowed to run even if:
    # - some participants are missing
    # - some pathways are missing
    # - engine QA blockers exist
    #
    # Blockers remain visible in result.
    # --------------------------------------------------------

    result = (
        calculate_trade_adjustment(
            engine_rows,
            strict=False,
        )
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
            for submission
            in submissions
            if str(
                submission.get(
                    "participant",
                    "",
                )
            ).strip()
        }
    )


    published_files = []


    # --------------------------------------------------------
    # Generate ONE copy-ready FullDataTradeAdj file
    # per participant.
    #
    # If participant submitted:
    # CT only      -> CT rows
    # CT + GS      -> both pathways
    # CT + GS + NC -> all three pathways
    # --------------------------------------------------------

    for participant in participants:

        participant_df = (
            adjusted_df[
                adjusted_df[
                    "CountryID"
                ].astype(str)
                == participant
            ]
            .copy()
        )


        if participant_df.empty:
            continue


        # Use ProductRaw so original FABLE
        # product naming is preserved.
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
        )


        # Clean order for manual copy/paste.
        output_df = (
            output_df
            .sort_values(
                [
                    "Pathway",
                    "Product",
                    "Year",
                ],
                kind="stable",
            )
            .reset_index(
                drop=True
            )
        )


        # Exact desired column order
        output_df = output_df[
            [
                "Pathway",
                "Product",
                "Year",
                "ImportsAdj",
                "ExportsAdj",
            ]
        ]


        csv_bytes = (
            output_df
            .to_csv(
                index=False
            )
            .encode(
                "utf-8-sig"
            )
        )


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
                "rows": len(
                    output_df
                ),
                "path": path,
            }
        )


    # --------------------------------------------------------
    # Save small run summary
    # --------------------------------------------------------

    run_summary = {
        "iteration": iteration,

        "engine_version": (
            result.get(
                "engine_version",
                ENGINE_VERSION,
            )
        ),

        "run_at_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),

        "reports_used": len(
            submissions
        ),

        "input_rows": len(
            engine_rows
        ),

        "adjusted_rows": len(
            adjusted_rows
        ),

        "blocker_count": len(
            blockers
        ),

        "published_participants": (
            len(
                published_files
            )
        ),

        "published_files": (
            published_files
        ),
    }


    github_put_json(
        (
            f"{result_folder(iteration)}"
            "/_run_summary.json"
        ),
        run_summary,
        (
            "Publish trade adjustment "
            f"run summary: {iteration}"
        ),
    )


    return {
        "engine_version": (
            result.get(
                "engine_version",
                ENGINE_VERSION,
            )
        ),
        "input_rows": len(
            engine_rows
        ),
        "adjusted_rows": len(
            adjusted_rows
        ),
        "reports_used": len(
            submissions
        ),
        "participants": len(
            published_files
        ),
        "published_files": (
            published_files
        ),
        "blockers": blockers,
        "global_qa": global_qa,
    }



# ============================================================
# 10. PAGE SETUP
# ============================================================

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="📊",
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
    "run the global trade adjustment and retrieve "
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

submit_tab, result_tab, admin_tab = (
    st.tabs(
        [
            "Submit Report",
            "Download Result",
            "Admin",
        ]
    )
)


# ============================================================
# 13. USER — SUBMIT REPORT
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

                        stored_path = (
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
                            )
                        )

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

                except Exception as exc:

                    st.error(
                        "Could not process "
                        f"the report: {exc}"
                    )


# ============================================================
# 14. USER — DOWNLOAD RESULT
# ============================================================

with result_tab:

    st.subheader(
        "Download adjusted trade"
    )

    try:
        configs = (
            list_iteration_configs()
        )
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

        result_config = (
            load_iteration_config(
                selected_result_iteration
            )
        )

        result_participant = (
            st.selectbox(
                "Country / participant",
                result_config.get(
                    "participants",
                    [],
                ),
                key="result_participant",
            )
        )

        path = result_path(
            selected_result_iteration,
            result_participant,
        )

        result_file = (
            github_get_file(
                path
            )
        )

        if result_file:

            st.success(
                "Adjusted trade result is ready."
            )

            st.caption(
                "The downloaded CSV is formatted for "
                "the FABLE-C FullDataTradeAdj table: "
                "Pathway, Product, Year, ImportsAdj, ExportsAdj."
            )

            st.download_button(
                "Download adjusted trade",
                data=result_file[
                    "content"
                ],
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
# 15. ADMIN
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
                                    "Running global trade "
                                    "adjustment..."
                                ):

                                    run_result = (
                                        run_iteration_trade_adjustment(
                                            progress_iteration
                                        )
                                    )


                                st.success(
                                    "Trade adjustment completed "
                                    "and participant result files "
                                    "were published."
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
                                        "blocker(s). Rows affected "
                                        "by a blocker can have a "
                                        "blank ExportsAdj value."
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
