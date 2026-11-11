# Verified Data Ingestion V1

`alpha_vantage_adapter.fetch_daily` now creates one `IngestionAttempt` just
before the HTTP call. It captures UTC start and full-body receipt times at those
events, validates the normalized result, and persists the receipt before
returning the same adapter result. `normalize_response` alone remains a pure
conversion and creates no receipt. No credentials or URLs reach the recorder.

Future calls create unique, exclusively created files in
`outputs/verified_ingestion/`: `<ingestion_id>_prices.json` and
`<ingestion_id>_receipt.json`. The hash identifies the exact UTF-8 bytes of that
new whitelisted normalized snapshot, **not** an existing `*_daily.json` file or
the raw HTTP body. The receipt is separate and does not hash itself. Existing
files and historical receipts are never backfilled or changed by the recorder.

`VALIDATED_LOCAL_INGESTION` means observed adapter events and valid daily OHLCV;
it is not external provider authentication. `REAL_MARKET_DATA` on that receipt
is the adapter's ingestion assertion, not proof from a hash. Synthetic or invalid
data get `REJECTED`, no snapshot and no hash. Transport/normalization exceptions
get `FAILED`, fixed error codes and no exception text. No complete response means
`response_received_at_utc` is null. Persistence errors raise a fixed error and
prevent a successful adapter return; an interrupted write can leave an orphan
snapshot or incomplete receipt requiring manual inspection. Treat only complete,
parseable receipts with matching snapshots as candidates for future review.

The recorder never promotes old provenance reports, alters risk rules, or enables
trading. The provenance manager still requires its independent trusted-evidence
contract; these local receipts are not automatically accepted as that evidence.
Daily validation uses the existing conservative prior-UTC-day policy without an
exchange holiday calendar. Local operator/clock/file integrity remains an
assumption. Tests mock transport and persistence, so no actual ingestion occurs.

## Later move to a private GitHub repository

1. Manually create an empty **private** repository. No Git initialization or push
   has been performed by this change.
2. Review `.gitignore`: `.env` variants, keys, credential files, `data/`, `outputs/`,
   `logs/` and local Supabase files are excluded. Inspect `.env.example` manually
   for placeholders only before including it. Ignore rules do not untrack files
   already committed and are not a substitute for reviewing the staged diff.
3. Initialize Git manually only when ready; stage explicit code, tests, docs and
   `.gitignore` paths. Inspect staged names and content before the first commit.
   If any real credential was previously committed, remove it from history and
   rotate it before publishing. Do not commit local prices or receipts.
4. Push only after that review. Restrict repository membership and branch writes.
   Future CI should run `python -m unittest discover -s tests -v` without provider
   credentials or live network tests. The current suite needs local historical
   fixtures: provision approved sanitized equivalents in a private CI workspace
   later, or refactor those tests before enabling CI; do not upload market data
   simply to make tests pass. A private repository does not grant data licenses.
5. Any later deployment workflow needs separate approval, minimal permissions and
   secrets stored outside the repository. See `SUPABASE_INTEGRATION_PLAN.md`.
