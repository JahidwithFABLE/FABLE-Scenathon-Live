"""Combine stored FABLE Scenathon submissions into two reproducible, flat CSV reports.

REPORTING_AGGREGATE remains one row per Location/Year/report identity.  Never
sum kcal_feas or kcal_mder across countries or repeat them per product.
"""
from __future__ import annotations
import pandas as pd

TRADE_FIELDS = ["Location", "Product", "Year", "Import_quantity", "Export_quantity"]
AGG_FIELDS = ["Location", "Year", "kcal_feas", "kcal_mder", "CalcAllLandCO2e", "CalcAllAgriCO2e"]
META_FIELDS = ["Scenathon_ID", "Scenathon_Date", "Iteration", "Pathway", "Stage", "Participant", "Username", "Submitted_At_UTC"]


def build_scenathon_reports(submissions, *, session_id, session_date=""):
    """Take (iteration_name, submission_json) tuples; return (aggregate_df, trade_df).

    The rows are consolidated, NOT numerically summed across different keys.
    Callers must supply each relevant iteration and report stage exactly once.
    """
    aggregate_records, trade_records = [], []
    for iteration, payload in submissions:
        if not isinstance(payload, dict):
            continue
        meta = {
            "Scenathon_ID": session_id,
            "Scenathon_Date": session_date,
            "Iteration": iteration,
            "Pathway": payload.get("pathway", ""),
            "Stage": payload.get("submission_stage", "before"),
            "Participant": payload.get("participant", ""),
            "Username": payload.get("username", ""),
            "Submitted_At_UTC": payload.get("submitted_at_utc", ""),
        }
        for row in payload.get("rows", []) or []:
            if isinstance(row, dict):
                trade_records.append({**meta, **{k: row.get(k) for k in TRADE_FIELDS}})
        for row in payload.get("aggregate_rows", []) or []:
            if isinstance(row, dict):
                aggregate_records.append({**meta, **{k: row.get(k) for k in AGG_FIELDS}})

    aggregate = pd.DataFrame(aggregate_records, columns=META_FIELDS+AGG_FIELDS)
    trade = pd.DataFrame(trade_records, columns=META_FIELDS+TRADE_FIELDS)
    return aggregate, trade


def csv_bytes(df):
    """UTF-8 BOM improves compatibility when a CSV is opened in Excel."""
    return df.to_csv(index=False).encode("utf-8-sig")
