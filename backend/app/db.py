from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Mapping, Sequence


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create/migrate tables non-destructively. Existing submission data is preserved."""
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS iterations (
                iteration_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open','closed','processing','ready','failed')),
                expected_reports INTEGER,
                created_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS submissions (
                submission_id TEXT PRIMARY KEY,
                iteration_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                username TEXT NOT NULL,
                country_id TEXT NOT NULL,
                location TEXT NOT NULL,
                pathway TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                report_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted',
                submitted_at TEXT NOT NULL,
                FOREIGN KEY(iteration_id) REFERENCES iterations(iteration_id),
                UNIQUE(iteration_id, country_id, pathway, version)
            );

            CREATE INDEX IF NOT EXISTS idx_submissions_iteration
            ON submissions(iteration_id);

            CREATE INDEX IF NOT EXISTS idx_submissions_identity
            ON submissions(iteration_id, country_id, pathway, version);

            CREATE INDEX IF NOT EXISTS idx_submissions_hash
            ON submissions(iteration_id, country_id, pathway, report_hash);

            CREATE TABLE IF NOT EXISTS iteration_expected_reports (
                iteration_id TEXT NOT NULL,
                country_id TEXT NOT NULL,
                pathway TEXT NOT NULL,
                PRIMARY KEY(iteration_id, country_id, pathway),
                FOREIGN KEY(iteration_id) REFERENCES iterations(iteration_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_iteration_expected_reports
            ON iteration_expected_reports(iteration_id, pathway, country_id);

            CREATE TABLE IF NOT EXISTS trade_rows (
                submission_id TEXT NOT NULL,
                product_raw TEXT NOT NULL,
                product_key TEXT NOT NULL,
                year INTEGER NOT NULL,
                import_quantity REAL NOT NULL,
                export_quantity REAL NOT NULL,
                PRIMARY KEY(submission_id, product_key, year),
                FOREIGN KEY(submission_id) REFERENCES submissions(submission_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                iteration_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running','completed','blocked','failed')),
                engine_version TEXT NOT NULL,
                input_submission_count INTEGER NOT NULL DEFAULT 0,
                input_row_count INTEGER NOT NULL DEFAULT 0,
                adjusted_row_count INTEGER NOT NULL DEFAULT 0,
                blocker_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error_message TEXT,
                FOREIGN KEY(iteration_id) REFERENCES iterations(iteration_id)
            );

            CREATE INDEX IF NOT EXISTS idx_runs_iteration
            ON runs(iteration_id, created_at);

            CREATE TABLE IF NOT EXISTS run_submissions (
                run_id TEXT NOT NULL,
                submission_id TEXT NOT NULL,
                country_id TEXT NOT NULL,
                pathway TEXT NOT NULL,
                version INTEGER NOT NULL,
                report_hash TEXT NOT NULL,
                PRIMARY KEY(run_id, submission_id),
                FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY(submission_id) REFERENCES submissions(submission_id)
            );

            CREATE INDEX IF NOT EXISTS idx_run_submissions_identity
            ON run_submissions(run_id, country_id, pathway);

            CREATE TABLE IF NOT EXISTS adjusted_trade_rows (
                run_id TEXT NOT NULL,
                country_id TEXT NOT NULL,
                location TEXT NOT NULL,
                pathway TEXT NOT NULL,
                product_raw TEXT NOT NULL,
                product_key TEXT NOT NULL,
                year INTEGER NOT NULL,
                imports_adj REAL NOT NULL,
                exports_adj REAL NOT NULL,
                import_original REAL NOT NULL,
                export_original REAL NOT NULL,
                adj_factor_export REAL NOT NULL,
                PRIMARY KEY(run_id, country_id, pathway, product_key, year),
                FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_adjusted_rows_country
            ON adjusted_trade_rows(run_id, country_id);

            CREATE TABLE IF NOT EXISTS run_global_qa (
                run_id TEXT NOT NULL,
                pathway TEXT NOT NULL,
                product_key TEXT NOT NULL,
                year INTEGER NOT NULL,
                global_imports REAL NOT NULL,
                global_exports_before REAL NOT NULL,
                alpha_2020 REAL,
                adj_factor_export REAL,
                global_exports_after REAL,
                status TEXT NOT NULL,
                raw_product_variants_json TEXT NOT NULL,
                PRIMARY KEY(run_id, pathway, product_key, year),
                FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS run_blockers (
                run_id TEXT NOT NULL,
                pathway TEXT NOT NULL,
                product_key TEXT NOT NULL,
                year INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY(run_id, pathway, product_key, year, reason),
                FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
            );
            """
        )


def get_active_iteration_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'active_iteration_id'"
    ).fetchone()
    if row is None or row["value"] is None:
        return None
    value = str(row["value"]).strip()
    return value or None


def set_active_iteration(
    conn: sqlite3.Connection,
    iteration_id: str | None,
) -> dict[str, Any] | None:
    """Persist the active submission iteration. Only open iterations can be active."""
    if iteration_id is None:
        conn.execute(
            "DELETE FROM app_settings WHERE key = 'active_iteration_id'"
        )
        return None

    iteration_id = str(iteration_id).strip()
    if not iteration_id:
        raise ValueError("iteration_id is blank")

    row = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if row is None:
        raise KeyError(iteration_id)
    if row["status"] != "open":
        raise ValueError(
            f"Iteration {iteration_id} is {row['status']}; only an open iteration can be active."
        )

    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES ('active_iteration_id', ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (iteration_id, utc_now_iso()),
    )
    return dict(row)


def create_iteration(
    conn: sqlite3.Connection,
    *,
    iteration_id: str,
    name: str | None = None,
    expected_reports: int | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    iteration_id = str(iteration_id).strip()
    if not iteration_id:
        raise ValueError("iteration_id is blank")
    clean_name = str(name).strip() if name is not None else iteration_id
    if not clean_name:
        clean_name = iteration_id
    if expected_reports is not None and int(expected_reports) < 1:
        raise ValueError("expected_reports must be at least 1 when provided.")

    exists = conn.execute(
        "SELECT 1 FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if exists is not None:
        raise ValueError(f"Iteration {iteration_id} already exists.")

    conn.execute(
        """
        INSERT INTO iterations
        (iteration_id, name, status, expected_reports, created_at, closed_at)
        VALUES (?, ?, 'open', ?, ?, NULL)
        """,
        (iteration_id, clean_name, expected_reports, utc_now_iso()),
    )
    if activate:
        set_active_iteration(conn, iteration_id)

    row = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Iteration insert failed")
    result = dict(row)
    result["active"] = get_active_iteration_id(conn) == iteration_id
    return result


def active_iteration_state(conn: sqlite3.Connection) -> dict[str, Any]:
    iteration_id = get_active_iteration_id(conn)
    if iteration_id is None:
        return {
            "active_iteration_id": None,
            "status": None,
            "expected_reports": None,
        }
    row = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if row is None:
        # Defensive cleanup if an old/corrupt setting points nowhere.
        set_active_iteration(conn, None)
        return {
            "active_iteration_id": None,
            "status": None,
            "expected_reports": None,
        }
    return {
        "active_iteration_id": iteration_id,
        "status": row["status"],
        "expected_reports": row["expected_reports"],
    }


def ensure_iteration(
    conn: sqlite3.Connection,
    iteration_id: str,
    *,
    expected_reports: int | None = None,
) -> sqlite3.Row:
    iteration_id = str(iteration_id).strip()
    if not iteration_id:
        raise ValueError("iteration_id is blank")

    conn.execute(
        """
        INSERT OR IGNORE INTO iterations
        (iteration_id, name, status, expected_reports, created_at)
        VALUES (?, ?, 'open', ?, ?)
        """,
        (iteration_id, iteration_id, expected_reports, utc_now_iso()),
    )
    row = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Could not create/read iteration")
    return row


def get_iteration(conn: sqlite3.Connection, iteration_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    return None if row is None else dict(row)


def set_iteration_expectations(
    conn: sqlite3.Connection,
    *,
    iteration_id: str,
    reports: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Replace the exact expected country/pathway set for an open iteration."""
    iteration = ensure_iteration(conn, iteration_id)
    if iteration["status"] != "open":
        raise ValueError(
            f"Iteration {iteration_id} must be open to configure expectations."
        )

    cleaned: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in reports:
        country_id = str(item["country_id"]).strip()
        pathway = str(item["pathway"]).strip()
        if not country_id or not pathway:
            raise ValueError("Expected report country_id/pathway cannot be blank.")
        key = (country_id, pathway)
        if key in seen:
            raise ValueError(f"Duplicate expected report: {country_id} / {pathway}")
        seen.add(key)
        cleaned.append(key)

    if not cleaned:
        raise ValueError("At least one expected report must be configured.")

    conn.execute(
        "DELETE FROM iteration_expected_reports WHERE iteration_id = ?",
        (iteration_id,),
    )
    conn.executemany(
        """
        INSERT INTO iteration_expected_reports (iteration_id, country_id, pathway)
        VALUES (?, ?, ?)
        """,
        [(iteration_id, country_id, pathway) for country_id, pathway in cleaned],
    )
    conn.execute(
        "UPDATE iterations SET expected_reports = ? WHERE iteration_id = ?",
        (len(cleaned), iteration_id),
    )
    return iteration_completeness(conn, iteration_id)


def get_iteration_expectations(
    conn: sqlite3.Connection,
    iteration_id: str,
) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT country_id, pathway
        FROM iteration_expected_reports
        WHERE iteration_id = ?
        ORDER BY pathway, country_id
        """,
        (iteration_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def iteration_completeness(
    conn: sqlite3.Connection,
    iteration_id: str,
) -> dict[str, Any]:
    iteration = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if iteration is None:
        raise KeyError(iteration_id)

    expected_rows = get_iteration_expectations(conn, iteration_id)
    latest = _latest_submissions(conn, iteration_id)

    expected = {(r["country_id"], r["pathway"]) for r in expected_rows}
    submitted = {(r["country_id"], r["pathway"]) for r in latest}

    missing = sorted(expected - submitted, key=lambda x: (x[1], x[0]))
    unexpected = sorted(submitted - expected, key=lambda x: (x[1], x[0]))

    return {
        "iteration_id": iteration_id,
        "status": iteration["status"],
        "expectations_configured": bool(expected_rows),
        "expected_reports": len(expected_rows) if expected_rows else iteration["expected_reports"],
        "current_reports": len(latest),
        "missing_count": len(missing),
        "unexpected_count": len(unexpected),
        "complete": bool(expected_rows) and not missing and not unexpected,
        "missing": [
            {"country_id": country_id, "pathway": pathway}
            for country_id, pathway in missing
        ],
        "unexpected": [
            {"country_id": country_id, "pathway": pathway}
            for country_id, pathway in unexpected
        ],
    }


def _new_submission_id() -> str:
    return "SUB-" + uuid.uuid4().hex[:12].upper()


def _new_run_id() -> str:
    return "RUN-" + uuid.uuid4().hex[:12].upper()


def save_submission(
    conn: sqlite3.Connection,
    *,
    iteration_id: str,
    normalized: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one immutable submission version; identical resubmits are idempotent."""

    country_id = str(normalized["country_id"]).strip()
    pathway = str(normalized["pathway"]).strip()
    report_hash = str(normalized["report_hash"])

    duplicate = conn.execute(
        """
        SELECT * FROM submissions
        WHERE iteration_id = ? AND country_id = ? AND pathway = ? AND report_hash = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (iteration_id, country_id, pathway, report_hash),
    ).fetchone()

    if duplicate is not None:
        result = dict(duplicate)
        result["duplicate"] = True
        return result

    max_version = conn.execute(
        """
        SELECT COALESCE(MAX(version), 0) AS max_version
        FROM submissions
        WHERE iteration_id = ? AND country_id = ? AND pathway = ?
        """,
        (iteration_id, country_id, pathway),
    ).fetchone()["max_version"]

    version = int(max_version) + 1
    submission_id = _new_submission_id()
    submitted_at = utc_now_iso()

    conn.execute(
        """
        INSERT INTO submissions
        (submission_id, iteration_id, version, username, country_id, location,
         pathway, row_count, report_hash, status, submitted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?)
        """,
        (
            submission_id,
            iteration_id,
            version,
            normalized["username"],
            country_id,
            normalized["location"],
            pathway,
            int(normalized["row_count"]),
            report_hash,
            submitted_at,
        ),
    )

    conn.executemany(
        """
        INSERT INTO trade_rows
        (submission_id, product_raw, product_key, year, import_quantity, export_quantity)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                submission_id,
                row["ProductRaw"],
                row["ProductKey"],
                int(row["Year"]),
                float(row["Import_quantity"]),
                float(row["Export_quantity"]),
            )
            for row in normalized["rows"]
        ],
    )

    result = conn.execute(
        "SELECT * FROM submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if result is None:
        raise RuntimeError("Submission insert failed")
    payload = dict(result)
    payload["duplicate"] = False
    return payload


def get_submission(conn: sqlite3.Connection, submission_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    return None if row is None else dict(row)


def get_submission_rows(conn: sqlite3.Connection, submission_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT product_raw, product_key, year, import_quantity, export_quantity
        FROM trade_rows
        WHERE submission_id = ?
        ORDER BY product_key, year
        """,
        (submission_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_submissions(conn: sqlite3.Connection, iteration_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest_versions AS (
            SELECT country_id, pathway, MAX(version) AS version
            FROM submissions
            WHERE iteration_id = ?
            GROUP BY country_id, pathway
        )
        SELECT s.*
        FROM submissions s
        JOIN latest_versions l
          ON s.country_id = l.country_id
         AND s.pathway = l.pathway
         AND s.version = l.version
        WHERE s.iteration_id = ?
        ORDER BY s.country_id, s.pathway
        """,
        (iteration_id, iteration_id),
    ).fetchall()


def dashboard_summary(conn: sqlite3.Connection, iteration_id: str) -> dict[str, Any]:
    iteration = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if iteration is None:
        raise KeyError(iteration_id)

    latest = _latest_submissions(conn, iteration_id)
    current = len(latest)
    countries = len({r["country_id"] for r in latest})
    latest_submitted_at = max((r["submitted_at"] for r in latest), default=None)

    by_pathway: dict[str, int] = {}
    for row in latest:
        by_pathway[row["pathway"]] = by_pathway.get(row["pathway"], 0) + 1

    completeness = iteration_completeness(conn, iteration_id)
    expected = completeness["expected_reports"]
    expected_int = None if expected is None else int(expected)
    pending = (
        completeness["missing_count"]
        if completeness["expectations_configured"]
        else (None if expected_int is None else max(expected_int - current, 0))
    )

    return {
        "iteration_id": iteration_id,
        "iteration_status": iteration["status"],
        "current_reports": current,
        "countries_submitted": countries,
        "expected_reports": expected_int,
        "pending_reports": pending,
        "by_pathway": dict(sorted(by_pathway.items())),
        "latest_submitted_at": latest_submitted_at,
    }


def close_iteration(
    conn: sqlite3.Connection,
    iteration_id: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    iteration = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if iteration is None:
        raise KeyError(iteration_id)

    summary = dashboard_summary(conn, iteration_id)
    status = iteration["status"]

    if status == "closed":
        if get_active_iteration_id(conn) == iteration_id:
            set_active_iteration(conn, None)
        return {
            "iteration_id": iteration_id,
            "status": status,
            "expected_reports": summary["expected_reports"],
            "current_reports": summary["current_reports"],
            "countries_submitted": summary["countries_submitted"],
            "closed_at": iteration["closed_at"],
        }

    if status != "open":
        raise ValueError(f"Iteration {iteration_id} cannot be closed from status {status}.")

    if summary["current_reports"] == 0:
        raise ValueError("Cannot close an iteration with no submitted reports.")

    expected = summary["expected_reports"]
    completeness = iteration_completeness(conn, iteration_id)
    if completeness["expectations_configured"] and not force:
        if completeness["missing_count"] or completeness["unexpected_count"]:
            raise ValueError(
                f"Iteration is not complete: {completeness['missing_count']} missing and "
                f"{completeness['unexpected_count']} unexpected report(s). "
                "Use the completeness endpoint to inspect the exact country/pathway pairs."
            )
    elif expected is not None and summary["current_reports"] < expected and not force:
        raise ValueError(
            f"Iteration has {summary['current_reports']} current report(s), "
            f"but {expected} are expected. Use force=true only for an intentional partial run."
        )

    closed_at = utc_now_iso()
    conn.execute(
        "UPDATE iterations SET status = 'closed', closed_at = ? WHERE iteration_id = ?",
        (closed_at, iteration_id),
    )
    if get_active_iteration_id(conn) == iteration_id:
        set_active_iteration(conn, None)
    return {
        "iteration_id": iteration_id,
        "status": "closed",
        "expected_reports": expected,
        "current_reports": summary["current_reports"],
        "countries_submitted": summary["countries_submitted"],
        "closed_at": closed_at,
    }


def reopen_iteration(conn: sqlite3.Connection, iteration_id: str) -> dict[str, Any]:
    iteration = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if iteration is None:
        raise KeyError(iteration_id)

    status = iteration["status"]
    if status == "open":
        summary = dashboard_summary(conn, iteration_id)
        return {
            "iteration_id": iteration_id,
            "status": "open",
            "expected_reports": summary["expected_reports"],
            "current_reports": summary["current_reports"],
            "countries_submitted": summary["countries_submitted"],
            "closed_at": None,
        }

    completed = conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE iteration_id = ? AND status = 'completed'",
        (iteration_id,),
    ).fetchone()["n"]
    if completed:
        raise ValueError(
            f"Iteration {iteration_id} has a completed run and cannot be reopened."
        )

    if status not in {"closed", "failed"}:
        raise ValueError(f"Iteration {iteration_id} cannot be reopened from status {status}.")

    conn.execute(
        "UPDATE iterations SET status = 'open', closed_at = NULL WHERE iteration_id = ?",
        (iteration_id,),
    )
    summary = dashboard_summary(conn, iteration_id)
    return {
        "iteration_id": iteration_id,
        "status": "open",
        "expected_reports": summary["expected_reports"],
        "current_reports": summary["current_reports"],
        "countries_submitted": summary["countries_submitted"],
        "closed_at": None,
    }


def create_run_snapshot(
    conn: sqlite3.Connection,
    *,
    iteration_id: str,
    engine_version: str,
) -> dict[str, Any]:
    iteration = conn.execute(
        "SELECT * FROM iterations WHERE iteration_id = ?",
        (iteration_id,),
    ).fetchone()
    if iteration is None:
        raise KeyError(iteration_id)
    if iteration["status"] != "closed":
        raise ValueError(
            f"Iteration {iteration_id} must be closed before a run can start; "
            f"current status is {iteration['status']}."
        )

    latest = _latest_submissions(conn, iteration_id)
    if not latest:
        raise ValueError("No current submissions are available for this iteration.")

    run_id = _new_run_id()
    input_row_count = sum(int(r["row_count"]) for r in latest)
    created_at = utc_now_iso()

    conn.execute(
        """
        INSERT INTO runs
        (run_id, iteration_id, status, engine_version,
         input_submission_count, input_row_count, created_at)
        VALUES (?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            run_id,
            iteration_id,
            engine_version,
            len(latest),
            input_row_count,
            created_at,
        ),
    )

    conn.executemany(
        """
        INSERT INTO run_submissions
        (run_id, submission_id, country_id, pathway, version, report_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                row["submission_id"],
                row["country_id"],
                row["pathway"],
                int(row["version"]),
                row["report_hash"],
            )
            for row in latest
        ],
    )

    conn.execute(
        "UPDATE iterations SET status = 'processing' WHERE iteration_id = ?",
        (iteration_id,),
    )

    return get_run(conn, run_id)  # type: ignore[return-value]


def get_run_input_rows(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.country_id,
               s.location,
               s.pathway,
               tr.product_raw,
               tr.product_key,
               tr.year,
               tr.import_quantity,
               tr.export_quantity,
               s.iteration_id
        FROM run_submissions rs
        JOIN submissions s ON s.submission_id = rs.submission_id
        JOIN trade_rows tr ON tr.submission_id = s.submission_id
        WHERE rs.run_id = ?
        ORDER BY s.country_id, s.pathway, tr.product_key, tr.year
        """,
        (run_id,),
    ).fetchall()

    return [
        {
            "Country": row["location"],
            "CountryID": row["country_id"],
            "Pathway": row["pathway"],
            "ProductRaw": row["product_raw"],
            "ProductKey": row["product_key"],
            "Year": int(row["year"]),
            "Import_quantity": float(row["import_quantity"]),
            "Export_quantity": float(row["export_quantity"]),
            "RunID": run_id,
            "Iteration": row["iteration_id"],
        }
        for row in rows
    ]


def _save_global_qa(
    conn: sqlite3.Connection,
    run_id: str,
    global_qa: Sequence[Mapping[str, Any]],
) -> None:
    conn.executemany(
        """
        INSERT INTO run_global_qa
        (run_id, pathway, product_key, year, global_imports, global_exports_before,
         alpha_2020, adj_factor_export, global_exports_after, status,
         raw_product_variants_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                row["Pathway"],
                row["Product"],
                int(row["Year"]),
                float(row["GlobalImports"]),
                float(row["GlobalExportsBefore"]),
                None if row["Alpha2020"] is None else float(row["Alpha2020"]),
                None if row["AdjFactorExport"] is None else float(row["AdjFactorExport"]),
                None if row["GlobalExportsAfter"] is None else float(row["GlobalExportsAfter"]),
                row["Status"],
                json.dumps(row.get("RawProductVariants", []), ensure_ascii=False),
            )
            for row in global_qa
        ],
    )


def save_blocked_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    global_qa: Sequence[Mapping[str, Any]],
    blockers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    _save_global_qa(conn, run_id, global_qa)
    conn.executemany(
        """
        INSERT INTO run_blockers
        (run_id, pathway, product_key, year, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                row["Pathway"],
                row["Product"],
                int(row["Year"]),
                row["Reason"],
            )
            for row in blockers
        ],
    )
    completed_at = utc_now_iso()
    conn.execute(
        """
        UPDATE runs
        SET status = 'blocked', blocker_count = ?, completed_at = ?
        WHERE run_id = ?
        """,
        (len(blockers), completed_at, run_id),
    )
    run = get_run(conn, run_id)
    if run is None:
        raise RuntimeError("Run disappeared while saving blockers")
    conn.execute(
        "UPDATE iterations SET status = 'failed' WHERE iteration_id = ?",
        (run["iteration_id"],),
    )
    return get_run(conn, run_id)  # type: ignore[return-value]


def save_completed_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    adjusted_rows: Sequence[Mapping[str, Any]],
    global_qa: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if any(row.get("ExportsAdj") is None for row in adjusted_rows):
        raise ValueError("A completed run cannot contain blocked adjusted rows.")

    _save_global_qa(conn, run_id, global_qa)
    conn.executemany(
        """
        INSERT INTO adjusted_trade_rows
        (run_id, country_id, location, pathway, product_raw, product_key, year,
         imports_adj, exports_adj, import_original, export_original, adj_factor_export)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                row["CountryID"],
                row["Country"],
                row["Pathway"],
                row["ProductRaw"],
                row["Product"],
                int(row["Year"]),
                float(row["ImportsAdj"]),
                float(row["ExportsAdj"]),
                float(row["ImportOriginal"]),
                float(row["ExportOriginal"]),
                float(row["AdjFactorExport"]),
            )
            for row in adjusted_rows
        ],
    )

    completed_at = utc_now_iso()
    conn.execute(
        """
        UPDATE runs
        SET status = 'completed', adjusted_row_count = ?, blocker_count = 0,
            completed_at = ?, error_message = NULL
        WHERE run_id = ?
        """,
        (len(adjusted_rows), completed_at, run_id),
    )
    run = get_run(conn, run_id)
    if run is None:
        raise RuntimeError("Run disappeared while completing")
    conn.execute(
        "UPDATE iterations SET status = 'ready' WHERE iteration_id = ?",
        (run["iteration_id"],),
    )
    return get_run(conn, run_id)  # type: ignore[return-value]


def save_failed_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    error_message: str,
) -> dict[str, Any]:
    completed_at = utc_now_iso()
    conn.execute(
        """
        UPDATE runs
        SET status = 'failed', completed_at = ?, error_message = ?
        WHERE run_id = ?
        """,
        (completed_at, error_message[:4000], run_id),
    )
    run = get_run(conn, run_id)
    if run is None:
        raise RuntimeError("Run disappeared while failing")
    conn.execute(
        "UPDATE iterations SET status = 'failed' WHERE iteration_id = ?",
        (run["iteration_id"],),
    )
    return get_run(conn, run_id)  # type: ignore[return-value]


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return None if row is None else dict(row)


def get_run_blockers(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT pathway, product_key, year, reason
        FROM run_blockers
        WHERE run_id = ?
        ORDER BY pathway, product_key, year, reason
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "Pathway": row["pathway"],
            "Product": row["product_key"],
            "Year": int(row["year"]),
            "Reason": row["reason"],
        }
        for row in rows
    ]



def get_latest_completed_run_for_location(
    conn: sqlite3.Connection,
    *,
    location: str,
) -> dict[str, Any] | None:
    """Return the newest completed run containing exactly one country for a location."""
    wanted = str(location).strip()
    if not wanted:
        return None

    rows = conn.execute(
        """
        SELECT r.*, a.country_id AS matched_country_id
        FROM runs r
        JOIN adjusted_trade_rows a ON a.run_id = r.run_id
        WHERE r.status = 'completed'
          AND lower(trim(a.location)) = lower(trim(?))
        GROUP BY r.run_id, a.country_id
        ORDER BY COALESCE(r.completed_at, r.created_at) DESC, r.created_at DESC
        """,
        (wanted,),
    ).fetchall()

    if not rows:
        return None

    newest_run_id = rows[0]["run_id"]
    newest_rows = [row for row in rows if row["run_id"] == newest_run_id]
    country_ids = {str(row["matched_country_id"]) for row in newest_rows}
    if len(country_ids) != 1:
        raise ValueError(
            f"Location {wanted!r} maps to multiple country IDs in run {newest_run_id}: "
            f"{sorted(country_ids)}"
        )

    result = dict(newest_rows[0])
    result["country_id"] = next(iter(country_ids))
    result.pop("matched_country_id", None)
    return result

def get_adjusted_rows_for_country(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    country_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT country_id, location, pathway, product_raw, product_key, year,
               imports_adj, exports_adj, import_original, export_original,
               adj_factor_export
        FROM adjusted_trade_rows
        WHERE run_id = ? AND country_id = ?
        ORDER BY pathway, product_key, year
        """,
        (run_id, country_id),
    ).fetchall()
    return [
        {
            "Country": row["location"],
            "CountryID": row["country_id"],
            "Pathway": row["pathway"],
            "Product": row["product_key"],
            "ProductRaw": row["product_raw"],
            "Year": int(row["year"]),
            "ImportsAdj": float(row["imports_adj"]),
            "ExportsAdj": float(row["exports_adj"]),
            "ImportOriginal": float(row["import_original"]),
            "ExportOriginal": float(row["export_original"]),
            "AdjFactorExport": float(row["adj_factor_export"]),
        }
        for row in rows
    ]
