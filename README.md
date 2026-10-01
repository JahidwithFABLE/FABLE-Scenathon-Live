# FABLE Scenathon Trade Adjustment

Trade-adjustment system for FABLE-C Scenathon workflows.

## Components

### Excel Add-in
Located in `addin/`.

The Office.js add-in allows FABLE-C users to:

- Share the required SCENATHON report data
- Send only the required trade fields to the backend
- Retrieve adjusted trade results
- Inject adjusted imports and exports into `FullDataTradeAdj`
- Activate the selected pathway in `PathwaysSelection`
- Recalculate and verify `FinalTradeAdj`

### Python Backend
Located in `backend/`.

The backend provides:

- submission validation
- deterministic report hashing
- iteration and submission management
- global trade-adjustment calculation
- QA and blocker detection
- adjusted trade result generation
- API endpoints for submission and retrieval

The validated trade-adjustment methodology is implemented in:

`backend/trade_engine.py`

### Reference Notebook
Located in `notebooks/`.

`Scenathon_Trade_Backend_FINAL.ipynb` documents the production methodology, submission contract, adjustment logic, blocker behaviour and injection payload.

## Repository Structure

```text
FABLE-Scenathon-Trade/
├── addin/
├── backend/
├── notebooks/
├── scripts/
├── docs/
└── .github/
```

## Backend Tests

From the `backend` directory:

```bash
python -m pytest -q
```

Current validation:

```text
30 passed
```

## Add-in Build

From the `addin` directory:

```bash
npm ci
npm run build
```

## Development Status

Core trade-adjustment calculation, backend API and Excel add-in Share/Inject workflows are implemented and tested.

Production hosting and distribution are handled separately from the calculation methodology and source code.
