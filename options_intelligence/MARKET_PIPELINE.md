# Market Pipeline

The offline entry point is:

```powershell
python -B -m options_intelligence.pipeline
```

It creates `outputs/market_pipeline_report.json` exclusively. A second run
requires a different `--output outputs/<new-name>.json`; existing results are
never overwritten. No existing modules or risk thresholds are changed.

## Provenance contract

Every new input needs `source`, `snapshot_utc`, `data_date`, `fetched_at_utc`,
`data_type`, `is_stale`, and `validation_errors`. Timestamps require timezones.
`snapshot_utc` means the source snapshot/availability time; `data_date` means
the latest completed trading date. File creation is not a market timestamp.

`data_type` is `REAL_MARKET_DATA` or `SYNTHETIC_TEST_DATA`. Missing legacy type
remains null in validation output and is rejected. Synthetic data remain
synthetic even with plausible dates. An aggregate of unverified/mixed inputs
also has null type/date; inspect the per-symbol records.

Real price ingestion requires the Alpha Vantage daily source and
`provenance.kind = ALPHA_VANTAGE_ADAPTER`. This records how data entered the
system, not a cryptographic certificate of vendor authenticity. Existing
NVDA data lack this contract and are not reclassified automatically.

Real eligibility requires `source = AUTHORIZED_FINVIZ_EXPORT`, a matching
provenance kind, and an explicitly authorized original CSV under `data/`.
`provenance.evidence_file` identifies the CSV and `provenance.sha256` must
match its bytes. Each CSV row needs `ticker`, `market_cap`, `average_volume`,
`optionable`, `snapshot_utc`, `data_date`, and `data_type`. The original
Stage 1 predicates are checked against that export. A hash checks content
consistency; authorization and authenticity still depend on the trusted
operator supplying the export. Sample CSVs are not authorized real exports.

## Optional acquisition after approval

The CLI is intentionally offline. `pipeline.run(..., approved=True,
fetcher=approved_client)` is the integration seam for an already authenticated
caller. `approved_client(symbol)` must call the existing
`alpha_vantage_adapter.fetch_daily` with credentials managed by that caller.
Do not call the adapter CLI, `load_local_env`, or print keys. This task does
not configure or read credentials. Tests use mocked acquisition only.

A validated, fresh local cache of at least 50 bars skips acquisition even
when EMA200 is missing. Successful fetches are saved separately as
`data/output/alpha_vantage/<SYMBOL>_daily_market_<unique-id>.json`.
The pipeline creates immutable `outputs/market_request_event_<id>.json`
records before requests. Across sequential invocations it allows at most
five attempts per UTC day, at most one attempt per symbol per day, and a
65-second cooldown. Cooldown returns a status rather than sleeping or
retrying. Any provider error stops all further requests for the UTC day;
exception messages and API keys are not logged. Invalid journals block
acquisition. Run acquisition serially; external programs sharing the same
API account are outside this journal's scope.

These are conservative local limits, not verified current free-plan terms.
Confirm official provider terms with approved network access before live
use. No full-history endpoint, paid subscription, or automatic upgrade is
requested. If 200 bars are unavailable, import a properly licensed longer
historical export through an appropriate provider adapter, retaining its
provenance and consistent OHLC adjustments.

## Integrity and options limitations

Snapshots older than 36 hours and market dates older than five calendar
days are rejected. Dates must be weekdays strictly before the evaluation
UTC day; there is no exchange holiday calendar. Future availability and
inconsistent timestamps are rejected. Historical as-of runs stay offline.

Only validated prices reach the existing technical calculator. Raw OHLCV
are explicitly unadjusted, with corporate actions unverified. Never mix
adjusted close with raw open/high/low for ATR. Longer histories need verified
split/dividend handling; this pipeline does not synthesize adjustments.

Only documented real eligibility reaches the existing opportunity engine.
Its underlying assessment is retained separately. All final options records
remain `INSUFFICIENT_DATA` while contracts, contract liquidity, price, fees,
and risk validation are absent. No contract validator or order execution is
implemented, and prices never prove optionability. Scores retain their
original heuristic meaning and are not win probabilities.
