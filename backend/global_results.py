

from __future__ import annotations

from typing import Any

import pandas as pd
import numpy as np


AGGREGATE_COLUMNS = [
    "Location",
    "Year",
    "kcal_feas",
    "kcal_mder",
    "CalcAllLandCO2e",
    "CalcAllAgriCO2e",
]


FOOD_SECURITY_YEARS = (
    2030,
    2050,
)

FOOD_SECURITY_MIN_PERCENT = 10.0

FOOD_SECURITY_MAX_PERCENT = 50.0

AGRICULTURE_GHG_TARGET_GT = 4.0

LAND_USE_GHG_TARGET_GT = -1.0



# ============================================================
# GENERAL CLEANING
# ============================================================

def clean_aggregate_dataframe(
    df: pd.DataFrame,
) -> pd.DataFrame:

    missing = [
        column
        for column
        in AGGREGATE_COLUMNS
        if column not in df.columns
    ]

    if missing:

        raise ValueError(
            "Missing aggregate columns: "
            + ", ".join(
                missing
            )
        )


    clean = (
        df[
            AGGREGATE_COLUMNS
        ]
        .copy()
    )


    clean[
        "Location"
    ] = (
        clean[
            "Location"
        ]
        .astype(str)
        .str.strip()
    )


    numeric_columns = [
        "Year",
        "kcal_feas",
        "kcal_mder",
        "CalcAllLandCO2e",
        "CalcAllAgriCO2e",
    ]


    for column in numeric_columns:

        clean[
            column
        ] = pd.to_numeric(
            clean[
                column
            ],
            errors="coerce",
        )


    clean = clean[
        clean[
            "Location"
        ].ne("")
    ].copy()


    clean = clean.dropna(
        subset=[
            "Year"
        ]
    )


    clean[
        "Year"
    ] = (
        clean[
            "Year"
        ]
        .round()
        .astype(int)
    )


    return clean.reset_index(
        drop=True
    )



# ============================================================
# SUBMISSION PAYLOAD → AGGREGATE DATASET
# ============================================================

def aggregate_rows_from_submissions(
    submissions: list[dict[str, Any]],
    *,
    pathway: str | None = None,
) -> pd.DataFrame:

    rows = []


    for submission in submissions:

        submission_pathway = str(
            submission.get(
                "pathway",
                "",
            )
        ).strip()


        if (
            pathway is not None
            and submission_pathway.casefold()
            != str(
                pathway
            ).strip().casefold()
        ):

            continue


        participant = str(
            submission.get(
                "participant",
                "",
            )
        ).strip()


        for row in submission.get(
            "aggregate_rows",
            [],
        ):

            record = dict(
                row
            )


            # Keep workbook Location as primary location.
            # Participant is retained separately for QA.
            record[
                "Participant"
            ] = participant


            record[
                "Pathway"
            ] = submission_pathway


            rows.append(
                record
            )


    if not rows:

        return pd.DataFrame(
            columns=(
                AGGREGATE_COLUMNS
                + [
                    "Participant",
                    "Pathway",
                ]
            )
        )


    raw_df = pd.DataFrame(
        rows
    )


    clean_df = (
        clean_aggregate_dataframe(
            raw_df
        )
    )


    # Recover metadata after cleaning by rebuilding
    # from original records using row order where possible.
    metadata_df = pd.DataFrame(
        rows
    )[
        [
            "Participant",
            "Pathway",
        ]
    ].reset_index(
        drop=True
    )


    if (
        len(metadata_df)
        == len(clean_df)
    ):

        clean_df[
            "Participant"
        ] = metadata_df[
            "Participant"
        ]

        clean_df[
            "Pathway"
        ] = metadata_df[
            "Pathway"
        ]


    return clean_df



# ============================================================
# DUPLICATE QA
# ============================================================

def aggregate_duplicate_rows(
    df: pd.DataFrame,
) -> pd.DataFrame:

    clean = (
        clean_aggregate_dataframe(
            df
        )
    )


    duplicate_mask = (
        clean.duplicated(
            subset=[
                "Location",
                "Year",
            ],
            keep=False,
        )
    )


    return (
        clean.loc[
            duplicate_mask
        ]
        .sort_values(
            [
                "Location",
                "Year",
            ]
        )
        .reset_index(
            drop=True
        )
    )



# ============================================================
# FOOD SECURITY
# ============================================================

def calculate_food_security(
    df: pd.DataFrame,
) -> dict[str, Any]:

    clean = (
        clean_aggregate_dataframe(
            df
        )
    )


    target_df = (
        clean[
            clean[
                "Year"
            ].isin(
                FOOD_SECURITY_YEARS
            )
        ]
        .copy()
    )


    regions = sorted(
        clean[
            "Location"
        ]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )


    detail_rows = []


    for region in regions:

        region_df = (
            target_df[
                target_df[
                    "Location"
                ]
                == region
            ]
            .copy()
        )


        region_complete = True

        region_pass = True


        year_results = {}


        for year in FOOD_SECURITY_YEARS:

            year_df = (
                region_df[
                    region_df[
                        "Year"
                    ]
                    == year
                ]
            )


            # Exactly one valid row required.
            if len(
                year_df
            ) != 1:

                year_results[
                    year
                ] = {
                    "margin_percent": None,
                    "pass": None,
                    "complete": False,
                }

                region_complete = False

                region_pass = False

                continue


            row = (
                year_df.iloc[
                    0
                ]
            )


            kcal_feas = row[
                "kcal_feas"
            ]

            kcal_mder = row[
                "kcal_mder"
            ]


            if (
                pd.isna(
                    kcal_feas
                )
                or pd.isna(
                    kcal_mder
                )
                or float(
                    kcal_mder
                )
                <= 0
            ):

                year_results[
                    year
                ] = {
                    "margin_percent": None,
                    "pass": None,
                    "complete": False,
                }

                region_complete = False

                region_pass = False

                continue


            margin = (
                (
                    float(
                        kcal_feas
                    )
                    / float(
                        kcal_mder
                    )
                )
                - 1.0
            ) * 100.0


            year_pass = (
                FOOD_SECURITY_MIN_PERCENT
                <= margin
                <= FOOD_SECURITY_MAX_PERCENT
            )


            year_results[
                year
            ] = {
                "margin_percent": margin,
                "pass": bool(
                    year_pass
                ),
                "complete": True,
            }


            if not year_pass:

                region_pass = False


        detail_rows.append(
            {
                "Location": region,

                "Margin_2030_pct": (
                    year_results[
                        2030
                    ][
                        "margin_percent"
                    ]
                ),

                "Pass_2030": (
                    year_results[
                        2030
                    ][
                        "pass"
                    ]
                ),

                "Margin_2050_pct": (
                    year_results[
                        2050
                    ][
                        "margin_percent"
                    ]
                ),

                "Pass_2050": (
                    year_results[
                        2050
                    ][
                        "pass"
                    ]
                ),

                "Complete": (
                    region_complete
                ),

                "Pass_Both_Years": (
                    (
                        region_pass
                    )
                    if region_complete
                    else None
                ),
            }
        )


    detail_df = pd.DataFrame(
        detail_rows
    )


    if detail_df.empty:

        return {
            "status": "INCOMPLETE",
            "target_met": None,
            "regions_total": 0,
            "regions_complete": 0,
            "regions_passing": 0,
            "regions_failing": 0,
            "regions_incomplete": 0,
            "detail": detail_df,
        }


    regions_total = len(
        detail_df
    )


    regions_complete = int(
        detail_df[
            "Complete"
        ]
        .fillna(
            False
        )
        .sum()
    )


    regions_incomplete = (
        regions_total
        - regions_complete
    )


    regions_passing = int(
        (
            detail_df[
                "Pass_Both_Years"
            ]
            == True
        ).sum()
    )


    regions_failing = int(
        (
            detail_df[
                "Pass_Both_Years"
            ]
            == False
        ).sum()
    )


    if regions_incomplete > 0:

        status = (
            "INCOMPLETE"
        )

        target_met = (
            None
        )


    elif (
        regions_passing
        == regions_total
    ):

        status = (
            "YES"
        )

        target_met = (
            True
        )


    else:

        status = (
            "NO"
        )

        target_met = (
            False
        )


    return {
        "status": status,

        "target_met": (
            target_met
        ),

        "regions_total": (
            regions_total
        ),

        "regions_complete": (
            regions_complete
        ),

        "regions_passing": (
            regions_passing
        ),

        "regions_failing": (
            regions_failing
        ),

        "regions_incomplete": (
            regions_incomplete
        ),

        "detail": (
            detail_df
        ),
    }



# ============================================================
# GENERIC 2050 GLOBAL GHG
# ============================================================

def calculate_global_ghg(
    df: pd.DataFrame,
    *,
    source_column: str,
    target_gt: float,
    comparison: str,
) -> dict[str, Any]:

    clean = (
        clean_aggregate_dataframe(
            df
        )
    )


    year_df = (
        clean[
            clean[
                "Year"
            ]
            == 2050
        ]
        .copy()
    )


    all_regions = set(
        clean[
            "Location"
        ]
        .dropna()
        .astype(str)
        .tolist()
    )


    year_regions = set(
        year_df[
            "Location"
        ]
        .dropna()
        .astype(str)
        .tolist()
    )


    missing_regions = sorted(
        all_regions
        - year_regions
    )


    duplicates = (
        year_df[
            year_df.duplicated(
                subset=[
                    "Location"
                ],
                keep=False,
            )
        ]
    )


    invalid_rows = (
        year_df[
            year_df[
                source_column
            ].isna()
        ]
    )


    complete = (
        bool(
            all_regions
        )
        and not missing_regions
        and duplicates.empty
        and invalid_rows.empty
        and len(
            year_df
        )
        == len(
            all_regions
        )
    )


    contribution_df = (
        year_df[
            [
                "Location",
                source_column,
            ]
        ]
        .copy()
    )


    contribution_df[
        "Value_Gt_CO2e"
    ] = (
        contribution_df[
            source_column
        ]
        / 1000.0
    )


    global_total_mt = (
        float(
            year_df[
                source_column
            ].sum()
        )
        if not year_df.empty
        else float(
            "nan"
        )
    )


    global_total_gt = (
        global_total_mt
        / 1000.0
        if pd.notna(
            global_total_mt
        )
        else float(
            "nan"
        )
    )


    if not complete:

        status = (
            "INCOMPLETE"
        )

        target_met = (
            None
        )


    else:

        if comparison == "<=":

            target_met = (
                global_total_gt
                <= target_gt
            )

        elif comparison == ">=":

            target_met = (
                global_total_gt
                >= target_gt
            )

        else:

            raise ValueError(
                "Unsupported comparison."
            )


        status = (
            "YES"
            if target_met
            else "NO"
        )


    return {
        "status": status,

        "target_met": (
            target_met
        ),

        "year": 2050,

        "source_unit": (
            "Mt CO2e"
        ),

        "display_unit": (
            "Gt CO2e"
        ),

        "global_total_mt": (
            global_total_mt
        ),

        "global_total_gt": (
            global_total_gt
        ),

        "target_gt": (
            target_gt
        ),

        "regions_total": (
            len(
                all_regions
            )
        ),

        "regions_2050": (
            len(
                year_regions
            )
        ),

        "missing_regions": (
            missing_regions
        ),

        "duplicate_region_rows": (
            len(
                duplicates
            )
        ),

        "invalid_value_rows": (
            len(
                invalid_rows
            )
        ),

        "complete": (
            complete
        ),

        "contributions": (
            contribution_df
        ),
    }



# ============================================================
# LAND USE
# ============================================================

def calculate_land_use_ghg(
    df: pd.DataFrame,
) -> dict[str, Any]:

    return calculate_global_ghg(
        df,

        source_column=(
            "CalcAllLandCO2e"
        ),

        target_gt=(
            LAND_USE_GHG_TARGET_GT
        ),

        comparison="<=",
    )



# ============================================================
# AGRICULTURE
# ============================================================

def calculate_agriculture_ghg(
    df: pd.DataFrame,
) -> dict[str, Any]:

    return calculate_global_ghg(
        df,

        source_column=(
            "CalcAllAgriCO2e"
        ),

        target_gt=(
            AGRICULTURE_GHG_TARGET_GT
        ),

        comparison="<=",
    )



# ============================================================
# COMPLETE GLOBAL ASSESSMENT
# ============================================================

def calculate_global_targets(
    df: pd.DataFrame,
) -> dict[str, Any]:

    clean = (
        clean_aggregate_dataframe(
            df
        )
    )


    duplicates = (
        aggregate_duplicate_rows(
            clean
        )
    )


    food = (
        calculate_food_security(
            clean
        )
    )


    agriculture = (
        calculate_agriculture_ghg(
            clean
        )
    )


    land_use = (
        calculate_land_use_ghg(
            clean
        )
    )


    summary = pd.DataFrame(
        [
            {
                "Target": (
                    "Food security"
                ),

                "Status": (
                    food[
                        "status"
                    ]
                ),

                "Actual": (
                    f"{food['regions_passing']}/"
                    f"{food['regions_total']} regions"
                ),

                "Target threshold": (
                    "All regions: 10–50% above MDER "
                    "in 2030 and 2050"
                ),
            },

            {
                "Target": (
                    "Agriculture GHG"
                ),

                "Status": (
                    agriculture[
                        "status"
                    ]
                ),

                "Actual": (
                    (
                        f"{agriculture['global_total_gt']:.3f} "
                        "Gt CO2e"
                    )
                    if pd.notna(
                        agriculture[
                            "global_total_gt"
                        ]
                    )
                    else "Missing"
                ),

                "Target threshold": (
                    "<= 4 Gt CO2e in 2050"
                ),
            },

            {
                "Target": (
                    "Land-use-change GHG"
                ),

                "Status": (
                    land_use[
                        "status"
                    ]
                ),

                "Actual": (
                    (
                        f"{land_use['global_total_gt']:.3f} "
                        "Gt CO2e"
                    )
                    if pd.notna(
                        land_use[
                            "global_total_gt"
                        ]
                    )
                    else "Missing"
                ),

                "Target threshold": (
                    "<= -1 Gt CO2e in 2050"
                ),
            },
        ]
    )


    return {
        "summary": (
            summary
        ),

        "food_security": (
            food
        ),

        "agriculture_ghg": (
            agriculture
        ),

        "land_use_ghg": (
            land_use
        ),

        "duplicate_rows": (
            duplicates
        ),

        "aggregate_rows": (
            len(
                clean
            )
        ),

        "regions": (
            clean[
                "Location"
            ]
            .nunique()
        ),
    }


# ============================================================
# EXPECTED SUBMISSION COVERAGE
# ============================================================

def calculate_global_targets_with_coverage(
    df: pd.DataFrame,
    *,
    expected_regions=None,
    submitted_regions=None,
) -> dict[str, Any]:

    """
    Calculate global targets while also checking whether
    all configured participants have submitted data.

    This prevents a false-positive YES when one or more
    complete country submissions are missing entirely.

    expected_regions:
        Configured participants for the iteration.

    submitted_regions:
        Participants that actually submitted the selected
        pathway/stage.
    """

    result = (
        calculate_global_targets(
            df
        )
    )


    expected_regions = (
        expected_regions
        or []
    )

    submitted_regions = (
        submitted_regions
        or []
    )


    # --------------------------------------------------------
    # NORMALIZE PARTICIPANT NAMES
    # --------------------------------------------------------

    expected_lookup = {}

    for value in expected_regions:

        display = str(
            value
        ).strip()

        if not display:
            continue

        expected_lookup[
            display.casefold()
        ] = display


    submitted_lookup = {}

    for value in submitted_regions:

        display = str(
            value
        ).strip()

        if not display:
            continue

        submitted_lookup[
            display.casefold()
        ] = display


    expected_keys = set(
        expected_lookup
    )

    submitted_keys = set(
        submitted_lookup
    )


    missing_keys = sorted(
        expected_keys
        - submitted_keys
    )


    unexpected_keys = sorted(
        submitted_keys
        - expected_keys
    )


    missing_regions = [
        expected_lookup[
            key
        ]
        for key in missing_keys
    ]


    unexpected_regions = [
        submitted_lookup[
            key
        ]
        for key in unexpected_keys
    ]


    expected_count = len(
        expected_keys
    )

    submitted_expected_count = len(
        expected_keys
        & submitted_keys
    )


    coverage_complete = (
        expected_count > 0
        and submitted_expected_count
        == expected_count
    )


    coverage = {
        "expected_regions": (
            expected_count
        ),

        "submitted_expected_regions": (
            submitted_expected_count
        ),

        "coverage_complete": (
            coverage_complete
        ),

        "missing_regions": (
            missing_regions
        ),

        "unexpected_regions": (
            unexpected_regions
        ),
    }


    result[
        "coverage"
    ] = coverage


    # --------------------------------------------------------
    # NO FALSE POSITIVE WHEN SUBMISSIONS ARE MISSING
    # --------------------------------------------------------

    if not coverage_complete:

        for key in [
            "food_security",
            "agriculture_ghg",
            "land_use_ghg",
        ]:

            if key in result:

                result[
                    key
                ][
                    "status"
                ] = "INCOMPLETE"

                result[
                    key
                ][
                    "target_met"
                ] = None


        summary = (
            result[
                "summary"
            ].copy()
        )

        summary[
            "Status"
        ] = "INCOMPLETE"

        result[
            "summary"
        ] = summary


    return result


# ============================================================
# FLEXIBLE GLOBAL ANALYTICS LAYER
# ============================================================

GLOBAL_ANALYTIC_INDICATORS = {
    "Food security margin": (
        "Food security margin"
    ),

    "Agriculture GHG": (
        "Agriculture GHG"
    ),

    "Land use change GHG": (
        "Land use change GHG"
    ),
}



def normalize_submission_stage(
    value,
) -> str:

    """
    Normalize stored submission-stage values.

    Legacy / blank submissions are interpreted as Before.
    """

    text = str(
        value
        or ""
    ).strip().casefold()


    if text in {
        "after",
        "after trade adjustment",
    }:

        return "After trade adjustment"


    return "Before trade adjustment"



def normalize_global_indicator_rows(
    submissions: list[dict[str, Any]],
) -> pd.DataFrame:

    """
    Convert v2 aggregate submissions into a long-form
    analytics dataframe.

    Output grain:

        Iteration
        Pathway
        Stage
        Participant
        Location
        Year
        Indicator
        Value

    This is intentionally independent from whether Location
    represents a country or a regional calculator.

    Whatever Location exists in the uploaded calculator is
    retained directly.
    """

    output_rows = []


    for submission in submissions:

        iteration = str(
            submission.get(
                "iteration",
                "",
            )
        ).strip()


        pathway = str(
            submission.get(
                "pathway",
                "",
            )
        ).strip()


        participant = str(
            submission.get(
                "participant",
                "",
            )
        ).strip()


        stage = (
            normalize_submission_stage(
                submission.get(
                    "submission_stage",
                    "before",
                )
            )
        )


        aggregate_rows = (
            submission.get(
                "aggregate_rows",
                []
            )
            or []
        )


        for row in aggregate_rows:

            location = str(
                row.get(
                    "Location",
                    "",
                )
            ).strip()


            year = pd.to_numeric(
                row.get(
                    "Year"
                ),
                errors="coerce",
            )


            if (
                not location
                or pd.isna(
                    year
                )
            ):

                continue


            year = int(
                round(
                    float(
                        year
                    )
                )
            )


            # -----------------------------------------------
            # FOOD SECURITY MARGIN
            # -----------------------------------------------

            kcal_feas = pd.to_numeric(
                row.get(
                    "kcal_feas"
                ),
                errors="coerce",
            )


            kcal_mder = pd.to_numeric(
                row.get(
                    "kcal_mder"
                ),
                errors="coerce",
            )


            food_margin = float(
                "nan"
            )


            if (
                pd.notna(
                    kcal_feas
                )
                and pd.notna(
                    kcal_mder
                )
                and float(
                    kcal_mder
                )
                > 0
            ):

                food_margin = (
                    (
                        float(
                            kcal_feas
                        )
                        / float(
                            kcal_mder
                        )
                    )
                    - 1.0
                ) * 100.0


            output_rows.append(
                {
                    "Iteration": (
                        iteration
                    ),

                    "Pathway": (
                        pathway
                    ),

                    "Stage": (
                        stage
                    ),

                    "Participant": (
                        participant
                    ),

                    "Location": (
                        location
                    ),

                    "Year": (
                        year
                    ),

                    "Indicator": (
                        "Food security margin"
                    ),

                    "Value": (
                        food_margin
                    ),

                    "Unit": (
                        "%"
                    ),
                }
            )


            # -----------------------------------------------
            # AGRICULTURE GHG
            # -----------------------------------------------

            agri_value = pd.to_numeric(
                row.get(
                    "CalcAllAgriCO2e"
                ),
                errors="coerce",
            )


            output_rows.append(
                {
                    "Iteration": iteration,
                    "Pathway": pathway,
                    "Stage": stage,
                    "Participant": participant,
                    "Location": location,
                    "Year": year,
                    "Indicator": (
                        "Agriculture GHG"
                    ),
                    "Value": (
                        (
                            float(
                                agri_value
                            )
                            / 1000.0
                        )
                        if pd.notna(
                            agri_value
                        )
                        else float(
                            "nan"
                        )
                    ),
                    "Unit": (
                        "Gt CO2e"
                    ),
                }
            )


            # -----------------------------------------------
            # LAND USE CHANGE GHG
            # -----------------------------------------------

            land_value = pd.to_numeric(
                row.get(
                    "CalcAllLandCO2e"
                ),
                errors="coerce",
            )


            output_rows.append(
                {
                    "Iteration": iteration,
                    "Pathway": pathway,
                    "Stage": stage,
                    "Participant": participant,
                    "Location": location,
                    "Year": year,
                    "Indicator": (
                        "Land use change GHG"
                    ),
                    "Value": (
                        (
                            float(
                                land_value
                            )
                            / 1000.0
                        )
                        if pd.notna(
                            land_value
                        )
                        else float(
                            "nan"
                        )
                    ),
                    "Unit": (
                        "Gt CO2e"
                    ),
                }
            )


    columns = [
        "Iteration",
        "Pathway",
        "Stage",
        "Participant",
        "Location",
        "Year",
        "Indicator",
        "Value",
        "Unit",
    ]


    if not output_rows:

        return pd.DataFrame(
            columns=columns
        )


    result = pd.DataFrame(
        output_rows,
        columns=columns,
    )


    result[
        "Value"
    ] = pd.to_numeric(
        result[
            "Value"
        ],
        errors="coerce",
    )


    return result.reset_index(
        drop=True
    )



# ============================================================
# FILTER NORMALIZED ANALYTICS
# ============================================================

def filter_global_indicator_rows(
    analytics_df: pd.DataFrame,
    *,
    iterations=None,
    pathways=None,
    stages=None,
    locations=None,
    years=None,
    indicators=None,
) -> pd.DataFrame:

    """
    Generic filtering helper for the future plotting UI.

    Every filter accepts either:

        None
        one string/value
        list / tuple / set
    """

    filtered = (
        analytics_df.copy()
    )


    def _as_list(
        value,
    ):

        if value is None:

            return None


        if isinstance(
            value,
            (
                list,
                tuple,
                set,
            ),
        ):

            return list(
                value
            )


        return [
            value
        ]


    filter_map = {
        "Iteration": (
            iterations
        ),

        "Pathway": (
            pathways
        ),

        "Stage": (
            stages
        ),

        "Location": (
            locations
        ),

        "Year": (
            years
        ),

        "Indicator": (
            indicators
        ),
    }


    for column, selection in (
        filter_map.items()
    ):

        selection = (
            _as_list(
                selection
            )
        )


        if selection is None:

            continue


        filtered = filtered[
            filtered[
                column
            ].isin(
                selection
            )
        ].copy()


    return filtered.reset_index(
        drop=True
    )



# ============================================================
# BUILD ONE GLOBAL SUMMARY PER SCOPE
# ============================================================

def build_global_scope_summary(
    submissions: list[dict[str, Any]],
    *,
    expected_participants_by_iteration=None,
) -> pd.DataFrame:

    """
    Build one target-summary row for every available:

        Iteration
        Pathway
        Stage

    This is the main source for iteration-progress plots.

    expected_participants_by_iteration format:

        {
            "Iteration 1": [
                "Argentina",
                "Brazil",
                ...
            ],
            "Iteration 2": [
                ...
            ]
        }
    """

    expected_participants_by_iteration = (
        expected_participants_by_iteration
        or {}
    )


    scopes = {}


    for submission in submissions:

        iteration = str(
            submission.get(
                "iteration",
                "",
            )
        ).strip()


        pathway = str(
            submission.get(
                "pathway",
                "",
            )
        ).strip()


        stage = (
            normalize_submission_stage(
                submission.get(
                    "submission_stage",
                    "before",
                )
            )
        )


        if (
            not iteration
            or not pathway
        ):

            continue


        key = (
            iteration,
            pathway,
            stage,
        )


        scopes.setdefault(
            key,
            []
        ).append(
            submission
        )


    summary_rows = []


    for (
        iteration,
        pathway,
        stage,
    ), scope_submissions in sorted(
        scopes.items(),
        key=lambda item: (
            str(
                item[
                    0
                ][
                    0
                ]
            ).casefold(),
            str(
                item[
                    0
                ][
                    1
                ]
            ).casefold(),
            str(
                item[
                    0
                ][
                    2
                ]
            ).casefold(),
        ),
    ):

        aggregate_df = (
            aggregate_rows_from_submissions(
                scope_submissions,
                pathway=pathway,
            )
        )


        submitted_participants = [
            str(
                payload.get(
                    "participant",
                    "",
                )
            ).strip()

            for payload
            in scope_submissions

            if str(
                payload.get(
                    "participant",
                    "",
                )
            ).strip()
        ]


        expected_participants = (
            expected_participants_by_iteration.get(
                iteration,
                submitted_participants,
            )
        )


        result = (
            calculate_global_targets_with_coverage(
                aggregate_df,
                expected_regions=(
                    expected_participants
                ),
                submitted_regions=(
                    submitted_participants
                ),
            )
        )


        coverage = (
            result[
                "coverage"
            ]
        )

        food = (
            result[
                "food_security"
            ]
        )

        agriculture = (
            result[
                "agriculture_ghg"
            ]
        )

        land = (
            result[
                "land_use_ghg"
            ]
        )


        food_total = int(
            food.get(
                "regions_total",
                0,
            )
            or 0
        )


        food_passing = int(
            food.get(
                "regions_passing",
                0,
            )
            or 0
        )


        food_percent = (
            (
                food_passing
                / food_total
            )
            * 100.0
            if food_total
            else float(
                "nan"
            )
        )


        summary_rows.append(
            {
                "Iteration": iteration,

                "Pathway": pathway,

                "Stage": stage,

                "Expected participants": (
                    int(
                        coverage.get(
                            "expected_regions",
                            0,
                        )
                        or 0
                    )
                ),

                "Submitted participants": (
                    int(
                        coverage.get(
                            "submitted_expected_regions",
                            0,
                        )
                        or 0
                    )
                ),

                "Coverage complete": (
                    bool(
                        coverage.get(
                            "coverage_complete",
                            False,
                        )
                    )
                ),

                "Food security passing": (
                    food_passing
                ),

                "Food security total": (
                    food_total
                ),

                "Food security passing percent": (
                    food_percent
                ),

                "Food security status": (
                    food.get(
                        "status",
                        "INCOMPLETE",
                    )
                ),

                "Agriculture GHG": (
                    agriculture.get(
                        "global_total_gt",
                        float(
                            "nan"
                        ),
                    )
                ),

                "Agriculture GHG status": (
                    agriculture.get(
                        "status",
                        "INCOMPLETE",
                    )
                ),

                "Land use change GHG": (
                    land.get(
                        "global_total_gt",
                        float(
                            "nan"
                        ),
                    )
                ),

                "Land use change GHG status": (
                    land.get(
                        "status",
                        "INCOMPLETE",
                    )
                ),
            }
        )


    columns = [
        "Iteration",
        "Pathway",
        "Stage",
        "Expected participants",
        "Submitted participants",
        "Coverage complete",
        "Food security passing",
        "Food security total",
        "Food security passing percent",
        "Food security status",
        "Agriculture GHG",
        "Agriculture GHG status",
        "Land use change GHG",
        "Land use change GHG status",
    ]


    if not summary_rows:

        return pd.DataFrame(
            columns=columns
        )


    return pd.DataFrame(
        summary_rows,
        columns=columns,
    )



# ============================================================
# BUILD BEFORE / AFTER COMPARISON
# ============================================================

def build_before_after_comparison(
    summary_df: pd.DataFrame,
) -> pd.DataFrame:

    """
    Convert the scope summary into a comparison-friendly
    table with one row per Iteration + Pathway.

    Useful for charts such as:

        Iteration 1
            Before vs After

        Iteration 2
            Before vs After
    """

    if summary_df.empty:

        return pd.DataFrame()


    value_columns = [
        "Food security passing percent",
        "Agriculture GHG",
        "Land use change GHG",
    ]


    comparison = (
        summary_df[
            [
                "Iteration",
                "Pathway",
                "Stage",
            ]
            + value_columns
        ]
        .copy()
    )


    comparison = comparison.pivot_table(
        index=[
            "Iteration",
            "Pathway",
        ],
        columns="Stage",
        values=value_columns,
        aggfunc="first",
    )


    comparison.columns = [
        (
            f"{metric} - {stage}"
        )
        for metric, stage
        in comparison.columns
    ]


    comparison = (
        comparison
        .reset_index()
    )


    return comparison


