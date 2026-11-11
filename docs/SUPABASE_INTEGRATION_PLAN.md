# Future Supabase integration — design only

No Supabase or GitHub connection, resource creation, secret read, migration or
deployment is part of V1. Confirm current platform configuration and APIs during
implementation; this plan introduces no dependencies on their present versions.

## Execution and secrets

Keep the provider request on a trusted server. A future Supabase Edge Function
can own the request, capture its UTC event times and perform equivalent schema
validation. Edge Functions do not directly execute this Python module: either
port the small validator/event contract to the Edge runtime and share test
vectors, or retain a private Python worker and an authenticated ingestion API.
Do not claim browser-supplied start/response timestamps as server observations.

Store the provider credential and a narrowly scoped database credential in
**Supabase Edge Function Secrets**, configured manually by an authorized
administrator. Read them only at execution time on the server. Never return or
log credentials, authorization headers, provider request URLs, raw exceptions
or arbitrary provider messages. Return an ingestion ID and safe status only.
Disable request URL tracing for provider calls that carry credentials in a query.

Authenticate invocations and restrict them to an approved scheduler or operator.
Do not allow unauthenticated clients to trigger paid provider calls. Enforce a
server-side symbol allowlist, quotas, cooldowns, request timeouts and idempotency.
GitHub can hold reviewed code/deployment definitions; it must not hold provider
secrets, live receipts or market data. Keep deployment credentials limited to
the deployment workflow, protected environments and authorized maintainers.

## Postgres record contract

Use a private schema, for example `market_private.ingestion_receipts`, with:

| Column | Intended type / constraint |
| --- | --- |
| ingestion_id | UUID primary key; unique per observed attempt |
| symbol, provider | constrained text; server allowlist |
| data_type | nullable enum for real assertion / synthetic data |
| request_started_at_utc | timestamptz, required |
| response_received_at_utc | nullable timestamptz |
| attempt_finished_at_utc | timestamptz, required |
| latest_market_date | nullable date |
| candle_count | nullable integer, positive when validated |
| file_sha256 | nullable 64-character lowercase hex text |
| verification_status | VALIDATED_LOCAL_INGESTION / REJECTED / FAILED |
| validation_errors | constrained array of fixed codes |
| evidence_scope | LOCAL_ADAPTER_EVENT_AND_SCHEMA_VALIDATION |
| externally_authenticated | boolean, false for local V1 evidence |
| snapshot_object_id | nullable private storage reference |
| created_at | server insertion timestamp, separate from request event times |

Require ordered event timestamps, and a hash, count, market date and private
snapshot reference for validated rows. Failed attempts may have no response time
or snapshot. Do not infer missing event times from `created_at` or file metadata.
Accepting a submitted row alone must never promote it to externally verified.

Store normalized snapshot bytes in private storage under the ingestion ID before
inserting the receipt; hash the exact stored bytes. The hash remains an integrity
check, not proof of provider authenticity. Use uniqueness/idempotency to avoid
duplicate retry inserts. Reconcile orphan objects after interrupted writes; do
not invent successful receipts for them. Avoid raw response storage unless its
licensing and sensitivity have been reviewed separately.

## Limited access

Revoke receipt table access from public, anonymous and ordinary authenticated
client roles. Enable RLS as defense in depth. Give a dedicated server writer
only INSERT on receipts and the minimum private snapshot upload permission;
deny UPDATE/DELETE and broad table reads. Use a separate restricted reader/view
for operational summaries, with explicit user/tenant authorization if needed.
Keep UUID and timestamp values explicit so the writer needs no broad privileges.

Use a dedicated Postgres login with those limited grants, held in Edge Function
Secrets, rather than giving the ingestion routine a general administrative key.
RLS does not constrain owners or roles that bypass it. If a future implementation
uses a service-role credential, recognize its broader privileges, keep it only
server-side and review that tradeoff explicitly; never ship it to a browser or
mobile client. Narrow SQL grants still require separate configuration/testing.

## Before enabling any integration

Manually review platform APIs, data licensing, clock policy, receipt/snapshot
retention and access grants. Create/review migrations and secrets; test allowed
inserts and denied client reads/updates/deletes. Test replay handling, transport
failures, partial persistence and secret redaction. Decide how independent
ingestion evidence may be accepted by `provenance_manager` before changing that
gate. Obtain authorization before live provider calls, deployment or uploads.
