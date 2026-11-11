# TradingBrain_Deriv

**Status: RESEARCH_ONLY — live trading is disabled.**

TradingBrain_Deriv is a Python research workspace for screening stock universes,
calculating technical indicators, assessing research opportunities, and checking
market-data quality and provenance. Results support investigation and validation;
they are not executable orders or evidence of investment performance.

## Main modules

| Module | Purpose |
| --- | --- |
| `options_intelligence/scanner.py`, `stage1.py` | Screen normalized CSV inputs and record rejection reasons. |
| `technical_indicators.py` | Calculate EMA, Wilder RSI and ATR with history and OHLCV validation. |
| `opportunity_engine.py` | Rank research candidates and reject missing, stale or inconsistent inputs. |
| `data_integrity.py`, `pipeline.py` | Enforce data contracts and assemble offline research reports. |
| `provenance_manager.py` | Audit local evidence without treating a file hash as vendor authentication. |
| `alpha_vantage_adapter.py`, `verified_ingestion.py` | Optional provider adapter and ingestion receipts; network acquisition requires a separately authorized operator. |
| `tests/` | Synthetic unit/security tests and seven explicitly marked local historical checks. |
| `samples/` | Invented demonstration CSV; never use it as real market evidence. |

`brokers/`, `core/`, and `dashboard/` are currently empty workspace directories.
Supabase integration is a design document, not a deployed service.

## Setup

Use Python 3.11 or later. Runtime and tests currently use only the Python standard
library. `requirements.txt` intentionally contains no third-party dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --no-index -r requirements.txt
```

On Linux/macOS activate with `source .venv/bin/activate`. No provider keys,
environment files, broker accounts, Supabase configuration or network access are
needed to run the tests. Python 3.11 is the configured CI target.

## Tests and publication checks

From the project root, run the complete local suite:

```powershell
python -m unittest discover -s tests -v
```

Seven historical integration checks are decorated with `local_data_test` and run
when their original local fixtures are available. A clean checkout reports them
as `LOCAL_DATA_ONLY` skips. Their existing assertions are preserved. All other
tests use generated values, the invented sample CSV, or mocked provider responses
and persistence. Hashing and previous-report preservation also have synthetic
coverage that runs in CI. Some fake responses intentionally claim `REAL_MARKET_DATA` to
exercise validation branches; their prices and credentials are still invented.

Run the same isolated checks configured for GitHub Actions:

```powershell
python scripts/check_publishable.py
python scripts/run_ci_tests.py
```

The CI runner skips only the seven named historical checks and rejects unexpected
skips. Its Python audit guard blocks socket traffic, DNS, subprocess execution,
secret-file opens, and all access to local data/log/report directories before
test discovery. The local suite also blocks networking, secret-file access and
writes to historical artifact directories. Provider calls remain mocked.
These guards protect against accidental test I/O; they are not an OS sandbox
against intentionally hostile code.

The workflow uses read-only repository permissions, pinned action revisions,
no configured repository secrets, no dependency installation, no artifact upload,
and no persisted checkout credential. GitHub runner provisioning and checkout
require GitHub infrastructure; the Python tests make no external connections.
The workflow has been prepared locally and has not been executed on GitHub.

## Research, analysis and execution

- **Research** uses synthetic or separately licensed local inputs to test ideas.
- **Analysis** computes indicators, scores and diagnostics. Scores are heuristics,
  not win probabilities, and a watchlist entry is not an order recommendation.
- **Live execution** would need independently reviewed broker integration,
  contract data, liquidity/fees validation, authorization and enforced risk gates.
  It is outside the current project capability. Existing strategy and risk rules
  remain unchanged; missing evidence continues to block execution.

Real provider acquisition is separate from offline analysis. Do not run the
adapter CLI or its environment loader during tests or repository preparation.
No setup command in this README enables trading or requests live prices.
Provider error messages omit arbitrary response text and suppress underlying
transport/parse exception details to reduce accidental credential disclosure.

## Data security and repository scope

Publish reviewed source, tests, documentation, synthetic samples,
`requirements.txt`, `.gitignore`, and `.github/workflows/` only.
Keep `.env` and every `.env.*` file (including `.env.example`), credentials,
tokens, private keys, virtual environments, raw market data, logs, local tool
caches, databases, generated reports and receipts outside Git. `data/`,
`outputs/`, `logs/`, and `reports/` are ignored. The readiness report in
`outputs/github_readiness_report.json` is a local operational artifact.

Never include account identifiers, authenticated URLs, private paths, provider
messages or licensed exports in source, fixtures, CI output or uploaded artifacts.
The offline publication scanner reports filenames and issue categories only;
its heuristic checks cannot prove that every secret is absent. It never opens
excluded files. Review any newly introduced file and staged content before a
future commit. Ignore rules do not remove already tracked files or old history.
If credentials were ever published, rotate them and separately review history.
A private repository still needs restricted membership and data licensing review.

No Git repository creation, commit, push, deployment, Supabase connection or
broker connection is included in this preparation. Earlier notes in
`docs/VERIFIED_INGESTION.md` describe historical CI planning; the workflow and
local-fixture separation documented here supersede those planning notes.

See [the Arabic project notes](README_AR.md),
[market pipeline details](options_intelligence/MARKET_PIPELINE.md),
[ingestion contract](docs/VERIFIED_INGESTION.md), and
[future Supabase design](docs/SUPABASE_INTEGRATION_PLAN.md).
