from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from trade_engine import (
    ENGINE_VERSION,
    SubmissionValidationError,
    build_injection_payload,
    calculate_trade_adjustment,
    canonical_pathway,
    normalize_submission,
)
from .db import (
    active_iteration_state,
    close_iteration,
    connect,
    create_iteration,
    create_run_snapshot,
    dashboard_summary,
    ensure_iteration,
    get_active_iteration_id,
    get_adjusted_rows_for_country,
    get_latest_completed_run_for_location,
    get_run,
    get_run_blockers,
    get_run_input_rows,
    get_submission,
    iteration_completeness,
    get_submission_rows,
    init_db,
    reopen_iteration,
    save_blocked_run,
    save_completed_run,
    save_failed_run,
    save_submission,
    set_active_iteration,
    set_iteration_expectations,
)
from .schemas import (
    ActiveIterationState,
    ConfigResponse,
    DashboardSummary,
    ExpectationsIn,
    HealthResponse,
    InjectionPayload,
    IterationAdminState,
    IterationCompleteness,
    IterationCreateIn,
    IterationState,
    RunBlockersResponse,
    RunMetadata,
    SubmissionIn,
    SubmissionMetadata,
    SubmissionResponse,
)


def _parse_cors(value: str | None) -> list[str]:
    if not value:
        return [
            "https://localhost:3000",
            "http://localhost:3000",
            "https://localhost:3001",
            "http://localhost:3001",
        ]
    return [x.strip() for x in value.split(",") if x.strip()]


def create_app(
    *,
    db_path: str | Path | None = None,
    active_iteration_id: str | None = None,
    expected_reports: int | None = None,
) -> FastAPI:
    resolved_db = Path(
        db_path
        or os.getenv("SCENATHON_DB_PATH")
        or "data/scenathon.db"
    )
    bootstrap_iteration = active_iteration_id or os.getenv("ACTIVE_ITERATION_ID")

    if expected_reports is None:
        raw_expected = os.getenv("EXPECTED_REPORTS")
        expected_reports = int(raw_expected) if raw_expected else None

    init_db(resolved_db)
    with connect(resolved_db) as conn:
        # Explicit constructor/env configuration is supported for tests/deployments.
        # Otherwise the active iteration is read from persistent DB state.
        if bootstrap_iteration:
            iteration = ensure_iteration(
                conn,
                bootstrap_iteration,
                expected_reports=expected_reports,
            )
            if iteration["status"] == "open":
                set_active_iteration(conn, bootstrap_iteration)
        conn.commit()

    app = FastAPI(
        title="FABLE Scenathon Trade Adjustment API",
        version="0.4.0",
        description=(
            "Receives minimal SCENATHON_report payloads from the Excel add-in, "
            "manages iteration/run lifecycle, and stores adjusted trade results. "
            "The whole workbook is never uploaded."
        ),
    )

    app.state.db_path = resolved_db

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_cors(os.getenv("CORS_ORIGINS")),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", engine_version=ENGINE_VERSION)

    @app.get("/api/v1/config", response_model=ConfigResponse)
    def config() -> ConfigResponse:
        with connect(app.state.db_path) as conn:
            state = active_iteration_state(conn)
            conn.commit()
        return ConfigResponse(
            active_iteration_id=state["active_iteration_id"],
            active_iteration_status=state["status"],
            engine_version=ENGINE_VERSION,
            expected_reports=state["expected_reports"],
        )

    @app.get(
        "/api/v1/iterations/active",
        response_model=ActiveIterationState,
    )
    def active_iteration_endpoint() -> ActiveIterationState:
        with connect(app.state.db_path) as conn:
            state = active_iteration_state(conn)
            conn.commit()
        return ActiveIterationState(**state)

    @app.post(
        "/api/v1/iterations",
        response_model=IterationAdminState,
    )
    def create_iteration_endpoint(request: IterationCreateIn) -> IterationAdminState:
        with connect(app.state.db_path) as conn:
            try:
                state = create_iteration(
                    conn,
                    iteration_id=request.iteration_id,
                    name=request.name,
                    expected_reports=request.expected_reports,
                    activate=request.activate,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            conn.commit()
        return IterationAdminState(**state)

    @app.post(
        "/api/v1/iterations/{iteration_id}/activate",
        response_model=ActiveIterationState,
    )
    def activate_iteration_endpoint(iteration_id: str) -> ActiveIterationState:
        with connect(app.state.db_path) as conn:
            try:
                set_active_iteration(conn, iteration_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Iteration not found.") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            state = active_iteration_state(conn)
            conn.commit()
        return ActiveIterationState(**state)

    @app.post("/api/v1/submissions", response_model=SubmissionResponse)
    def submit_report(request: SubmissionIn) -> SubmissionResponse:
        payload = request.model_dump(
            exclude={"iteration_id"},
            exclude_none=True,
        )
        payload["rows"] = [r.model_dump() for r in request.rows]

        try:
            normalized = normalize_submission(payload)
        except SubmissionValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        with connect(app.state.db_path) as conn:
            iteration_id = request.iteration_id or get_active_iteration_id(conn)
            if not iteration_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "No active open iteration is configured. "
                        "An administrator must create or activate an iteration before submissions."
                    ),
                )

            iteration = ensure_iteration(conn, iteration_id)
            if iteration["status"] != "open":
                raise HTTPException(
                    status_code=409,
                    detail=f"Iteration {iteration_id} is not open for submissions.",
                )

            saved = save_submission(
                conn,
                iteration_id=iteration_id,
                normalized=normalized,
            )
            conn.commit()

        return SubmissionResponse(**saved)

    @app.get(
        "/api/v1/submissions/{submission_id}",
        response_model=SubmissionMetadata,
    )
    def submission_metadata(submission_id: str) -> SubmissionMetadata:
        with connect(app.state.db_path) as conn:
            row = get_submission(conn, submission_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Submission not found.")
        return SubmissionMetadata(**row)

    @app.get("/api/v1/submissions/{submission_id}/rows")
    def submission_rows(submission_id: str) -> dict:
        with connect(app.state.db_path) as conn:
            meta = get_submission(conn, submission_id)
            if meta is None:
                raise HTTPException(status_code=404, detail="Submission not found.")
            rows = get_submission_rows(conn, submission_id)
        return {
            "submission_id": submission_id,
            "row_count": len(rows),
            "rows": rows,
        }

    @app.get(
        "/api/v1/dashboard/summary",
        response_model=DashboardSummary,
    )
    def dashboard(iteration_id: str | None = None) -> DashboardSummary:
        with connect(app.state.db_path) as conn:
            wanted = iteration_id or get_active_iteration_id(conn)
            if not wanted:
                raise HTTPException(
                    status_code=409,
                    detail="No active iteration is configured. Pass iteration_id explicitly or activate one.",
                )
            try:
                summary = dashboard_summary(conn, wanted)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Iteration not found.") from exc
        return DashboardSummary(**summary)

    @app.put(
        "/api/v1/iterations/{iteration_id}/expectations",
        response_model=IterationCompleteness,
    )
    def configure_iteration_expectations(
        iteration_id: str,
        request: ExpectationsIn,
    ) -> IterationCompleteness:
        reports = []
        for item in request.reports:
            try:
                pathway = canonical_pathway(item.pathway)
            except SubmissionValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            reports.append(
                {
                    "country_id": item.country_id.strip(),
                    "pathway": pathway,
                }
            )

        with connect(app.state.db_path) as conn:
            try:
                state = set_iteration_expectations(
                    conn,
                    iteration_id=iteration_id,
                    reports=reports,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            conn.commit()
        return IterationCompleteness(**state)

    @app.get(
        "/api/v1/iterations/{iteration_id}/completeness",
        response_model=IterationCompleteness,
    )
    def iteration_completeness_endpoint(
        iteration_id: str,
    ) -> IterationCompleteness:
        with connect(app.state.db_path) as conn:
            try:
                state = iteration_completeness(conn, iteration_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Iteration not found.") from exc
        return IterationCompleteness(**state)

    @app.post(
        "/api/v1/iterations/{iteration_id}/close",
        response_model=IterationState,
    )
    def close_iteration_endpoint(
        iteration_id: str,
        force: bool = False,
    ) -> IterationState:
        with connect(app.state.db_path) as conn:
            try:
                state = close_iteration(conn, iteration_id, force=force)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Iteration not found.") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            conn.commit()
        return IterationState(**state)

    @app.post(
        "/api/v1/iterations/{iteration_id}/reopen",
        response_model=IterationState,
    )
    def reopen_iteration_endpoint(iteration_id: str) -> IterationState:
        with connect(app.state.db_path) as conn:
            try:
                state = reopen_iteration(conn, iteration_id)
                # If closing/failed processing left no active iteration, reopening this
                # iteration makes it the natural submission target again. Never
                # override a different active open iteration.
                if get_active_iteration_id(conn) is None:
                    set_active_iteration(conn, iteration_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Iteration not found.") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            conn.commit()
        return IterationState(**state)

    @app.post(
        "/api/v1/iterations/{iteration_id}/runs",
        response_model=RunMetadata,
    )
    def run_trade_adjustment(iteration_id: str) -> RunMetadata:
        # First freeze the exact latest submissions used by this run.
        with connect(app.state.db_path) as conn:
            try:
                run = create_run_snapshot(
                    conn,
                    iteration_id=iteration_id,
                    engine_version=ENGINE_VERSION,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="Iteration not found.") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            conn.commit()

        run_id = run["run_id"]

        try:
            with connect(app.state.db_path) as conn:
                input_rows = get_run_input_rows(conn, run_id)

            # strict=False lets orchestration persist the blocker list and QA.
            # Blockers still hard-block the run below; no adjusted result becomes injectable.
            result = calculate_trade_adjustment(input_rows, strict=False)

            with connect(app.state.db_path) as conn:
                if result["blockers"]:
                    final = save_blocked_run(
                        conn,
                        run_id=run_id,
                        global_qa=result["global_qa"],
                        blockers=result["blockers"],
                    )
                else:
                    final = save_completed_run(
                        conn,
                        run_id=run_id,
                        adjusted_rows=result["adjusted_rows"],
                        global_qa=result["global_qa"],
                    )
                conn.commit()

        except Exception as exc:
            with connect(app.state.db_path) as conn:
                save_failed_run(
                    conn,
                    run_id=run_id,
                    error_message=f"{type(exc).__name__}: {exc}",
                )
                conn.commit()
            raise HTTPException(
                status_code=500,
                detail=f"Trade adjustment run failed: {exc}",
            ) from exc

        return RunMetadata(**final)


    @app.get(
        "/api/v1/injection/latest",
        response_model=InjectionPayload,
    )
    def latest_country_adjusted_result(location: str) -> InjectionPayload:
        wanted_location = location.strip()
        if not wanted_location:
            raise HTTPException(status_code=400, detail="location cannot be blank.")

        with connect(app.state.db_path) as conn:
            try:
                run = get_latest_completed_run_for_location(
                    conn,
                    location=wanted_location,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            if run is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "No completed adjusted trade result is available for "
                        f"location={wanted_location!r}."
                    ),
                )

            country_id = str(run["country_id"])
            rows = get_adjusted_rows_for_country(
                conn,
                run_id=run["run_id"],
                country_id=country_id,
            )

        if not rows:
            raise HTTPException(
                status_code=404,
                detail=(
                    "The latest completed run was found, but it contains no adjusted rows "
                    f"for location={wanted_location!r}."
                ),
            )

        payload = build_injection_payload(rows, country_id=country_id)
        payload["run_id"] = run["run_id"]
        payload["iteration_id"] = run["iteration_id"]
        return InjectionPayload(**payload)

    @app.get(
        "/api/v1/runs/{run_id}",
        response_model=RunMetadata,
    )
    def run_metadata(run_id: str) -> RunMetadata:
        with connect(app.state.db_path) as conn:
            run = get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return RunMetadata(**run)

    @app.get(
        "/api/v1/runs/{run_id}/blockers",
        response_model=RunBlockersResponse,
    )
    def run_blockers(run_id: str) -> RunBlockersResponse:
        with connect(app.state.db_path) as conn:
            run = get_run(conn, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found.")
            blockers = get_run_blockers(conn, run_id)
        return RunBlockersResponse(
            run_id=run_id,
            blocker_count=len(blockers),
            blockers=blockers,
        )

    @app.get(
        "/api/v1/runs/{run_id}/results/{country_id}",
        response_model=InjectionPayload,
    )
    def country_adjusted_result(run_id: str, country_id: str) -> InjectionPayload:
        with connect(app.state.db_path) as conn:
            run = get_run(conn, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found.")
            if run["status"] != "completed":
                raise HTTPException(
                    status_code=409,
                    detail=f"Run {run_id} is {run['status']}; adjusted results are not injectable.",
                )
            rows = get_adjusted_rows_for_country(
                conn,
                run_id=run_id,
                country_id=country_id,
            )

        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"No adjusted result found for country_id={country_id!r}.",
            )

        payload = build_injection_payload(rows, country_id=country_id)
        payload["run_id"] = run_id
        payload["iteration_id"] = run["iteration_id"]
        return InjectionPayload(**payload)

    return app


app = create_app()
