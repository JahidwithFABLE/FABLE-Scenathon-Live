from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TradeRowIn(BaseModel):
    """Minimal row sent by the Excel add-in from SCENATHON_report."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    Location: str = Field(min_length=1)
    Product: str = Field(min_length=1)
    Year: int
    Import_quantity: float = Field(ge=0)
    Export_quantity: float = Field(ge=0)


class SubmissionIn(BaseModel):
    """Payload for the 'Share your SCENATHON report' button."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=200)
    pathway: str = Field(min_length=1, max_length=100)
    country_id: str | None = Field(default=None, min_length=1, max_length=100)
    iteration_id: str | None = Field(default=None, min_length=1, max_length=100)
    rows: list[TradeRowIn] = Field(min_length=1)


class SubmissionResponse(BaseModel):
    submission_id: str
    iteration_id: str
    version: int
    username: str
    country_id: str
    location: str
    pathway: str
    row_count: int
    report_hash: str
    status: str
    submitted_at: str
    duplicate: bool = False


class SubmissionMetadata(BaseModel):
    submission_id: str
    iteration_id: str
    version: int
    username: str
    country_id: str
    location: str
    pathway: str
    row_count: int
    report_hash: str
    status: str
    submitted_at: str


class DashboardSummary(BaseModel):
    iteration_id: str
    iteration_status: str
    current_reports: int
    countries_submitted: int
    expected_reports: int | None = None
    pending_reports: int | None = None
    by_pathway: dict[str, int]
    latest_submitted_at: str | None = None


class ExpectedReportKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_id: str = Field(min_length=1, max_length=100)
    pathway: str = Field(min_length=1, max_length=100)


class ExpectationsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reports: list[ExpectedReportKey] = Field(min_length=1)


class IterationCompleteness(BaseModel):
    iteration_id: str
    status: str
    expectations_configured: bool
    expected_reports: int | None = None
    current_reports: int
    missing_count: int
    unexpected_count: int
    complete: bool
    missing: list[ExpectedReportKey]
    unexpected: list[ExpectedReportKey]


class IterationState(BaseModel):
    iteration_id: str
    status: str
    expected_reports: int | None = None
    current_reports: int
    countries_submitted: int
    closed_at: str | None = None


class RunMetadata(BaseModel):
    run_id: str
    iteration_id: str
    status: str
    engine_version: str
    input_submission_count: int
    input_row_count: int
    adjusted_row_count: int
    blocker_count: int
    created_at: str
    completed_at: str | None = None
    error_message: str | None = None


class InjectionRow(BaseModel):
    Pathway: str
    ProductKey: str
    Year: int
    ImportsAdj: float
    ExportsAdj: float


class InjectionPayload(BaseModel):
    engine_version: str
    run_id: str
    iteration_id: str
    country_id: str
    row_count: int
    rows: list[InjectionRow]


class BlockerRow(BaseModel):
    Pathway: str
    Product: str
    Year: int
    Reason: str


class RunBlockersResponse(BaseModel):
    run_id: str
    blocker_count: int
    blockers: list[BlockerRow]


class ConfigResponse(BaseModel):
    active_iteration_id: str | None = None
    active_iteration_status: str | None = None
    engine_version: str
    expected_reports: int | None = None


class IterationCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration_id: str = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=200)
    expected_reports: int | None = Field(default=None, ge=1)
    activate: bool = True


class IterationAdminState(BaseModel):
    iteration_id: str
    name: str
    status: str
    expected_reports: int | None = None
    active: bool
    created_at: str
    closed_at: str | None = None


class ActiveIterationState(BaseModel):
    active_iteration_id: str | None = None
    status: str | None = None
    expected_reports: int | None = None


class HealthResponse(BaseModel):
    status: str
    engine_version: str


class ErrorResponse(BaseModel):
    detail: str | list[dict[str, Any]]
