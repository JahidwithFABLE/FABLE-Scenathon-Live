# FABLE Scenathon Trade Adjustment Backend

This folder is the cleaned production core extracted from the validation notebook.

## Files

- `trade_engine.py` — production calculation/validation module.
- `Scenathon_Trade_Backend_FINAL.ipynb` — executable reference notebook.
- `tests/test_trade_engine.py` — unit tests for the locked methodology and edge cases.
- `requirements.txt` — runtime dependencies (currently standard library only).
- `requirements-dev.txt` — test dependency (`pytest`).

## Production architecture

The backend does **not** open or modify Excel files.

The Excel Office.js add-in owns workbook interaction:

1. **Share your SCENATHON report**
   - reads only the minimal report fields;
   - sends JSON to the backend;
   - does not upload the full workbook.

2. **Inject adjusted trade**
   - receives the adjusted country payload;
   - matches `FullDataTradeAdj` by `Pathway + Product.strip().casefold() + Year`;
   - writes `ImportsAdj` / `ExportsAdj`;
   - sets the selected `PathwaysSelection[S12]` to `Yes`;
   - recalculates and verifies `FinalTradeAdj`.

The Python module owns only:
- payload validation;
- stable report hashing;
- global pathway/product/year aggregation;
- the validated export adjustment methodology;
- blockers and QA;
- minimal injection payload generation.

## Key production rules

- Product key: `strip().casefold()`.
- No automatic semantic mapping (`lentil` is not silently mapped to `lentils`).
- Imports are unchanged.
- Historical years 2000, 2005, 2010, 2015, 2020 are unchanged.
- Clara's excluded products are unchanged.
- 2020 `0/0` with zero future trade is `NO_TRADE` with factor 1.
- Unsafe/undefined future-trade cases block the iteration.
- The `alpha=-1`, zero-export case is handled before division and safely returns factor 1.
- Country/product schemas may differ; the engine uses the rows actually submitted.
- Country IDs are not restricted to ISO3, so regional identifiers such as `RMECAS` are valid.

## Run tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## API integration

The web API should import:

```python
from trade_engine import (
    normalize_submission,
    calculate_trade_adjustment,
    build_injection_payload,
)
```

Suggested server flow:

- `POST /submissions` -> validate + persist minimal report + return server `run_id`.
- Close an iteration after required submissions are present.
- Run `calculate_trade_adjustment(all_iteration_rows, strict=True)`.
- Persist adjusted rows and QA.
- Return country adjustment data to the add-in with `build_injection_payload(...)`.

`run_id`, authentication, timestamps, database storage, iteration lifecycle, and HTTP routing belong in the server layer rather than the calculation engine.
