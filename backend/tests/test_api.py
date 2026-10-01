from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


REPORT_ROWS = [
    {
        "Location": "Argentina",
        "Product": "barley",
        "Year": 2020,
        "Import_quantity": 100.0,
        "Export_quantity": 90.0,
    },
    {
        "Location": "Argentina",
        "Product": "barley",
        "Year": 2025,
        "Import_quantity": 110.0,
        "Export_quantity": 150.0,
    },
]


def make_client(tmp_path: Path, *, expected_reports: int | None = 3) -> TestClient:
    app = create_app(
        db_path=tmp_path / "test.db",
        active_iteration_id="TEST-001",
        expected_reports=expected_reports,
    )
    return TestClient(app)


def submit_payload(
    client: TestClient,
    *,
    country_id: str = "ARG",
    pathway: str = "CurrentTrends",
    rows=None,
):
    return client.post(
        "/api/v1/submissions",
        json={
            "username": "Jahid",
            "country_id": country_id,
            "pathway": pathway,
            "rows": rows or REPORT_ROWS,
        },
    )


def test_health(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_submit_and_duplicate_is_idempotent(tmp_path):
    client = make_client(tmp_path)
    first = submit_payload(client)
    assert first.status_code == 200
    first_json = first.json()
    assert first_json["version"] == 1
    assert first_json["duplicate"] is False
    assert first_json["row_count"] == 2

    second = submit_payload(client)
    assert second.status_code == 200
    second_json = second.json()
    assert second_json["submission_id"] == first_json["submission_id"]
    assert second_json["duplicate"] is True


def test_changed_resubmission_creates_version_2(tmp_path):
    client = make_client(tmp_path)
    r1 = submit_payload(client).json()

    changed_rows = [dict(x) for x in REPORT_ROWS]
    changed_rows[1]["Export_quantity"] = 151.0
    r2 = submit_payload(client, rows=changed_rows).json()

    assert r1["version"] == 1
    assert r2["version"] == 2
    assert r1["submission_id"] != r2["submission_id"]


def test_dashboard_counts_latest_report_only(tmp_path):
    client = make_client(tmp_path)
    submit_payload(client)

    changed_rows = [dict(x) for x in REPORT_ROWS]
    changed_rows[1]["Export_quantity"] = 151.0
    submit_payload(client, rows=changed_rows)

    summary = client.get("/api/v1/dashboard/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["current_reports"] == 1
    assert body["countries_submitted"] == 1
    assert body["expected_reports"] == 3
    assert body["pending_reports"] == 2
    assert body["by_pathway"] == {"CurrentTrends": 1}


def test_unknown_pathway_rejected(tmp_path):
    client = make_client(tmp_path)
    response = submit_payload(client, pathway="WrongPathway")
    assert response.status_code == 400


def test_close_rejects_incomplete_expected_reports_without_force(tmp_path):
    client = make_client(tmp_path, expected_reports=3)
    submit_payload(client)

    response = client.post("/api/v1/iterations/TEST-001/close")
    assert response.status_code == 409
    assert "3 are expected" in response.json()["detail"]


def test_close_force_then_submissions_are_locked(tmp_path):
    client = make_client(tmp_path, expected_reports=3)
    submit_payload(client)

    close = client.post("/api/v1/iterations/TEST-001/close?force=true")
    assert close.status_code == 200
    assert close.json()["status"] == "closed"

    later = submit_payload(client, pathway="GlobalSustainability")
    assert later.status_code == 409


def test_run_requires_closed_iteration(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    submit_payload(client)

    response = client.post("/api/v1/iterations/TEST-001/runs")
    assert response.status_code == 409
    assert "must be closed" in response.json()["detail"]


def test_completed_run_and_country_injection_result(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    submit_payload(client)

    close = client.post("/api/v1/iterations/TEST-001/close")
    assert close.status_code == 200

    run = client.post("/api/v1/iterations/TEST-001/runs")
    assert run.status_code == 200
    run_json = run.json()
    assert run_json["status"] == "completed"
    assert run_json["input_submission_count"] == 1
    assert run_json["input_row_count"] == 2
    assert run_json["adjusted_row_count"] == 2
    assert run_json["blocker_count"] == 0

    run_id = run_json["run_id"]
    result = client.get(f"/api/v1/runs/{run_id}/results/ARG")
    assert result.status_code == 200
    body = result.json()
    assert body["run_id"] == run_id
    assert body["iteration_id"] == "TEST-001"
    assert body["country_id"] == "ARG"
    assert body["row_count"] == 2

    row2025 = next(x for x in body["rows"] if x["Year"] == 2025)
    assert row2025["Pathway"] == "CurrentTrends"
    assert row2025["ProductKey"] == "barley"
    assert row2025["ImportsAdj"] == 110.0
    # alpha2020=-0.1, exports>=imports => cap at 1.1*imports = 121
    assert abs(row2025["ExportsAdj"] - 121.0) < 1e-9

    summary = client.get("/api/v1/dashboard/summary", params={"iteration_id": "TEST-001"}).json()
    assert summary["iteration_status"] == "ready"


def test_run_snapshot_uses_latest_submission_version_only(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    first = submit_payload(client).json()

    changed_rows = [dict(x) for x in REPORT_ROWS]
    changed_rows[1]["Export_quantity"] = 200.0
    second = submit_payload(client, rows=changed_rows).json()
    assert second["version"] == 2

    client.post("/api/v1/iterations/TEST-001/close")
    run = client.post("/api/v1/iterations/TEST-001/runs").json()

    assert run["input_submission_count"] == 1
    assert run["input_row_count"] == 2
    assert first["submission_id"] != second["submission_id"]

    result = client.get(f"/api/v1/runs/{run['run_id']}/results/ARG").json()
    row2025 = next(x for x in result["rows"] if x["Year"] == 2025)
    assert abs(row2025["ExportsAdj"] - 121.0) < 1e-9


def test_blocked_run_persists_blockers_and_no_result_is_injectable(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    blocked_rows = [
        {
            "Location": "Argentina",
            "Product": "barley",
            "Year": 2020,
            "Import_quantity": 0.0,
            "Export_quantity": 0.0,
        },
        {
            "Location": "Argentina",
            "Product": "barley",
            "Year": 2025,
            "Import_quantity": 10.0,
            "Export_quantity": 0.0,
        },
    ]
    submit_payload(client, rows=blocked_rows)
    client.post("/api/v1/iterations/TEST-001/close")

    run = client.post("/api/v1/iterations/TEST-001/runs")
    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "blocked"
    assert body["blocker_count"] == 1
    assert body["adjusted_row_count"] == 0

    run_id = body["run_id"]
    blockers = client.get(f"/api/v1/runs/{run_id}/blockers")
    assert blockers.status_code == 200
    blocker_rows = blockers.json()["blockers"]
    assert blocker_rows[0]["Reason"] == "UNDEFINED_2020_ALPHA_WITH_FUTURE_TRADE"

    result = client.get(f"/api/v1/runs/{run_id}/results/ARG")
    assert result.status_code == 409

    summary = client.get("/api/v1/dashboard/summary", params={"iteration_id": "TEST-001"}).json()
    assert summary["iteration_status"] == "failed"


def test_failed_or_blocked_iteration_can_reopen_before_completed_run(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    blocked_rows = [
        {
            "Location": "Argentina",
            "Product": "barley",
            "Year": 2020,
            "Import_quantity": 0.0,
            "Export_quantity": 0.0,
        },
        {
            "Location": "Argentina",
            "Product": "barley",
            "Year": 2025,
            "Import_quantity": 10.0,
            "Export_quantity": 0.0,
        },
    ]
    submit_payload(client, rows=blocked_rows)
    client.post("/api/v1/iterations/TEST-001/close")
    run = client.post("/api/v1/iterations/TEST-001/runs").json()
    assert run["status"] == "blocked"

    reopened = client.post("/api/v1/iterations/TEST-001/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"

    fixed_rows = [dict(x) for x in REPORT_ROWS]
    new_submission = submit_payload(client, rows=fixed_rows)
    assert new_submission.status_code == 200
    assert new_submission.json()["version"] == 2


def test_ready_iteration_cannot_reopen(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    submit_payload(client)
    client.post("/api/v1/iterations/TEST-001/close")
    run = client.post("/api/v1/iterations/TEST-001/runs").json()
    assert run["status"] == "completed"

    reopened = client.post("/api/v1/iterations/TEST-001/reopen")
    assert reopened.status_code == 409
    assert "completed run" in reopened.json()["detail"]


def test_exact_expectations_report_missing_and_unexpected_pairs(tmp_path):
    client = make_client(tmp_path, expected_reports=None)

    configured = client.put(
        "/api/v1/iterations/TEST-001/expectations",
        json={
            "reports": [
                {"country_id": "ARG", "pathway": "CurrentTrends"},
                {"country_id": "BRA", "pathway": "GlobalSustainability"},
            ]
        },
    )
    assert configured.status_code == 200
    body = configured.json()
    assert body["expected_reports"] == 2
    assert body["missing_count"] == 2
    assert body["unexpected_count"] == 0
    assert body["complete"] is False

    submit_payload(client, country_id="ARG", pathway="CurrentTrends")
    submit_payload(client, country_id="CHN", pathway="NationalCommitments")

    status = client.get("/api/v1/iterations/TEST-001/completeness")
    assert status.status_code == 200
    body = status.json()
    assert body["missing_count"] == 1
    assert body["unexpected_count"] == 1
    assert body["complete"] is False
    assert body["missing"] == [
        {"country_id": "BRA", "pathway": "GlobalSustainability"}
    ]
    assert body["unexpected"] == [
        {"country_id": "CHN", "pathway": "NationalCommitments"}
    ]

    close = client.post("/api/v1/iterations/TEST-001/close")
    assert close.status_code == 409
    assert "1 missing" in close.json()["detail"]
    assert "1 unexpected" in close.json()["detail"]


def test_exact_expectations_allow_close_only_when_complete(tmp_path):
    client = make_client(tmp_path, expected_reports=None)

    configured = client.put(
        "/api/v1/iterations/TEST-001/expectations",
        json={
            "reports": [
                {"country_id": "ARG", "pathway": "CurrentTrends"},
                {"country_id": "BRA", "pathway": "GlobalSustainability"},
            ]
        },
    )
    assert configured.status_code == 200

    submit_payload(client, country_id="ARG", pathway="CurrentTrends")
    submit_payload(client, country_id="BRA", pathway="GlobalSustainability")

    status = client.get("/api/v1/iterations/TEST-001/completeness")
    body = status.json()
    assert body["complete"] is True
    assert body["missing_count"] == 0
    assert body["unexpected_count"] == 0

    dashboard = client.get("/api/v1/dashboard/summary").json()
    assert dashboard["expected_reports"] == 2
    assert dashboard["pending_reports"] == 0

    close = client.post("/api/v1/iterations/TEST-001/close")
    assert close.status_code == 200
    assert close.json()["status"] == "closed"


def test_latest_injection_resolves_country_by_workbook_location(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    submit_payload(client, country_id="ARG")
    client.post("/api/v1/iterations/TEST-001/close")
    run = client.post("/api/v1/iterations/TEST-001/runs").json()
    assert run["status"] == "completed"

    result = client.get("/api/v1/injection/latest", params={"location": "Argentina"})
    assert result.status_code == 200
    body = result.json()
    assert body["run_id"] == run["run_id"]
    assert body["iteration_id"] == "TEST-001"
    assert body["country_id"] == "ARG"
    assert body["row_count"] == 2


def test_latest_injection_location_match_is_trimmed_and_case_insensitive(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    submit_payload(client, country_id="ARG")
    client.post("/api/v1/iterations/TEST-001/close")
    client.post("/api/v1/iterations/TEST-001/runs")

    result = client.get("/api/v1/injection/latest", params={"location": "  argentina  "})
    assert result.status_code == 200
    assert result.json()["country_id"] == "ARG"


def test_latest_injection_returns_404_when_no_completed_result_exists(tmp_path):
    client = make_client(tmp_path, expected_reports=None)
    result = client.get("/api/v1/injection/latest", params={"location": "Argentina"})
    assert result.status_code == 404


def test_active_iteration_is_persisted_and_used_for_submission(tmp_path):
    db_path = tmp_path / "persistent.db"
    client = TestClient(create_app(db_path=db_path))

    config = client.get("/api/v1/config")
    assert config.status_code == 200
    assert config.json()["active_iteration_id"] is None

    created = client.post(
        "/api/v1/iterations",
        json={"iteration_id": "SCENATHON-001", "activate": True},
    )
    assert created.status_code == 200
    assert created.json()["active"] is True

    submission = submit_payload(client)
    assert submission.status_code == 200
    assert submission.json()["iteration_id"] == "SCENATHON-001"

    # Restart against the same DB without any hard-coded active iteration.
    restarted = TestClient(create_app(db_path=db_path))
    config2 = restarted.get("/api/v1/config").json()
    assert config2["active_iteration_id"] == "SCENATHON-001"
    assert config2["active_iteration_status"] == "open"


def test_closing_active_iteration_clears_active_and_blocks_implicit_submission(tmp_path):
    db_path = tmp_path / "close-active.db"
    client = TestClient(create_app(db_path=db_path))
    created = client.post(
        "/api/v1/iterations",
        json={"iteration_id": "SCENATHON-001", "activate": True},
    )
    assert created.status_code == 200
    assert submit_payload(client).status_code == 200

    closed = client.post("/api/v1/iterations/SCENATHON-001/close")
    assert closed.status_code == 200

    active = client.get("/api/v1/iterations/active")
    assert active.status_code == 200
    assert active.json()["active_iteration_id"] is None

    later = submit_payload(client, pathway="GlobalSustainability")
    assert later.status_code == 409
    assert "No active open iteration" in later.json()["detail"]


def test_admin_can_create_new_active_iteration_after_previous_close(tmp_path):
    db_path = tmp_path / "rotate.db"
    client = TestClient(create_app(db_path=db_path))
    client.post(
        "/api/v1/iterations",
        json={"iteration_id": "ROUND-1", "activate": True},
    )
    submit_payload(client)
    assert client.post("/api/v1/iterations/ROUND-1/close").status_code == 200

    second = client.post(
        "/api/v1/iterations",
        json={"iteration_id": "ROUND-2", "name": "Scenathon Round 2", "activate": True},
    )
    assert second.status_code == 200
    assert second.json()["iteration_id"] == "ROUND-2"
    assert second.json()["active"] is True

    submission = submit_payload(client, pathway="GlobalSustainability")
    assert submission.status_code == 200
    assert submission.json()["iteration_id"] == "ROUND-2"


def test_cannot_activate_closed_iteration(tmp_path):
    db_path = tmp_path / "activate-closed.db"
    client = TestClient(create_app(db_path=db_path))
    client.post(
        "/api/v1/iterations",
        json={"iteration_id": "ROUND-1", "activate": True},
    )
    submit_payload(client)
    client.post("/api/v1/iterations/ROUND-1/close")

    response = client.post("/api/v1/iterations/ROUND-1/activate")
    assert response.status_code == 409
    assert "only an open iteration can be active" in response.json()["detail"]
