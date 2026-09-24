# NurserySignal

NurserySignal is a UK sales-intelligence service for detecting nursery openings,
expansions and related commercial signals.

This repository contains the first deployable foundation:

- Terraform bootstrap and application infrastructure in `infra/`.
- A small Python 3.12 Lambda backend in `backend/`.
- SQL migrations and the initial relational model.
- A normalized signal ingestion contract.
- A provider-independent planning collector with deterministic nursery candidate filtering.
- A fixture-driven authenticated signal ingestion and enrichment flow.
- A React/Vite internal admin frontend served through private S3 and CloudFront.
- Local tests and GitHub Actions checks.

The workflows in `.github/workflows/` run checks on pull requests and provide a
manual OIDC-based deployment entry point once the repository-specific IAM role
has been created and stored as the `NURSERYSIGNAL_AWS_ROLE_ARN` repository
variable.

## Architecture choices

The initial database is a single-AZ encrypted RDS PostgreSQL `db.t4g.micro` in
the account's existing default VPC. It is private from the public internet and
accessible only from the backend Lambda security group. This is intentionally
less expensive than Aurora Serverless v2 at very low usage; Aurora's minimum
capacity would be an always-on cost floor. Multi-AZ, read replicas and NAT are
deferred until workload or connectivity needs justify them; the single Secrets
Manager interface endpoint is already required by the VPC-attached Lambdas.

The backend and enrichment worker Lambdas are VPC-attached for database access.
The existing single-AZ Secrets Manager interface endpoint lets them retrieve
runtime credentials without a NAT gateway. Future collector Lambdas should use
an explicitly chosen endpoint strategy or remain outside the database VPC.

## Ingestion vertical slice

`POST /signals` accepts the versioned normalized signal contract. The API writes
the original JSON body to the raw-evidence bucket, creates the `raw_signals`
record, and sends a small reference message to the enrichment queue. Duplicate
`source_type` plus `external_id` submissions return the original signal ID.

The enrichment Lambda currently applies deterministic fixture classification and
stores a `PENDING` candidate in `signal_enrichments`. Authenticated admins can
inspect and review candidates with:

- `GET /admin/signals`
- `GET /admin/signals?review_status=REVIEWED&q=...` for bounded reviewed-history search
- `GET /admin/signals/{id}`
- `POST /admin/signals/{id}/approve`
- `POST /admin/signals/{id}/reject`

The admin frontend uses `GET /admin/signals/{id}/evidence` to obtain a
five-minute presigned URL for the private raw JSON evidence object. The
evidence bucket is never made public.

## Planning collector

The first live provider adapter is Plota. It is isolated behind
`backend/app/planning.py`, so another licensed provider can implement the same
`PlanningProvider` interface later. The collector searches a bounded date
window, paginates with the provider cursor, applies local positive and
exclusion heuristics, and sends versioned messages to the existing ingestion
queue. The ingestion worker then uses the normal S3 -> database -> enrichment
path.

The Terraform stack stores the provider credential in a private Secrets Manager
secret. The existing EventBridge rule runs at `rate(1 day)` and invokes the
collector with a two-day overlapping window, a maximum of 100 records and a
page size of 25. EventBridge rate schedules are UTC cadence-based rather than a
fixed wall-clock time: the first run is scheduled relative to enablement and
subsequent runs are 24 hours apart. The two-day overlap relies on ingestion
idempotency to avoid duplicate canonical signals. Store a Plota demo or paid
key as either the raw secret value or `{"api_key":"..."}`. The API key is
never logged or committed. Invoke a bounded sample manually with the collector
Lambda when troubleshooting:

```bash
aws lambda invoke --profile nurserysignal --region eu-west-1 \
  --function-name nurserysignal-prod-planning-collector \
  --payload '{"from_date":"2026-09-22","to_date":"2026-09-24","max_records":25}' \
  /tmp/nurserysignal-planning-result.json
```

Planning records use `plota:<application-id>` as their stable external ID.
Repeated unchanged observations are idempotent. A changed planning record is
stored as an immutable evidence object and a `raw_signal_revisions` row while
updating the existing canonical signal, rather than creating a duplicate.
The current enrichment candidate is intentionally left for human review; a
future phase can add explicit lifecycle updates for material decisions.

Planning and fixture enrichment share the deterministic relevance rules in
`backend/app/classification.py`. Strong horticultural evidence such as plant or
tree nurseries, garden centres, nursery stock, propagation, seedlings, saplings,
RHS or gardening is treated as a likely false positive unless meaningful
childcare evidence is also present. Such candidates remain `OTHER` at
`DISCOVERED` with low confidence and are never promoted by the fixture rules.
The regression corpus is in `fixtures/classification_regressions.json` and is
not automatically reprocessed against historical records.

An invited user in the Cognito `NurserySignalAdmins` group can explicitly
re-evaluate a bounded set of stored planning records with
`POST /admin/planning/reprocess`. This is reclassification of the provider
evidence already retained in the database and private S3; it does not call
Plota, write new evidence, enqueue enrichment, or create a new raw signal.
Requests are capped at 100 records and may be narrowed by date range or signal
IDs. Pending candidates are updated in place using the shared deterministic
rules. Approved and rejected records retain their candidate fields and review
history, with only a compact latest derived evaluation attached for audit.
Each operation writes one safe admin audit event containing counts and rule
version, never raw payloads or secrets.

## Internal admin frontend

The React/Vite app lives in `frontend/`. It uses the existing Cognito user pool
with invited users only; self-registration is disabled. Tokens are kept in
browser session storage, refreshed through the Cognito session, and attached
only to API requests. Expired sessions return the operator to the sign-in
screen.

Frontend configuration is supplied at build time with:

```bash
VITE_API_URL=https://... \
VITE_AWS_REGION=eu-west-1 \
VITE_COGNITO_USER_POOL_ID=... \
VITE_COGNITO_CLIENT_ID=... \
npm --prefix frontend run build
```

The deployment workflow obtains these values from Terraform outputs, uploads
hashed assets to the private frontend bucket, and invalidates the CloudFront
entry point. Run `npm --prefix frontend test` for frontend tests.

The initial invited admin account is managed with Cognito's admin API. Do not
commit passwords or add self-registration to the frontend.

The admin UI presents pending candidates as a Review Inbox. Approved and
rejected candidates are kept in a separate, paginated Reviewed Signals history
with source/date filters and bounded text search. Review actions use an
accessible application modal, remove handled items from the inbox immediately,
and can be deliberately corrected from history without changing evidence or
creating ingestion/enrichment events.

Sample signals are in `fixtures/signals.json`. With a Cognito ID token, submit
them using `COGNITO_ID_TOKEN=... SIGNALS_API_URL=... make ingest-fixtures`.

## Repository setup

The GitHub Actions OIDC deployment role is restricted to the immutable GitHub
subject for `gaspode/NurserySignal` (`gaspode@856445/NurserySignal@1385020285`)
and the workflow expects its ARN in the `NURSERYSIGNAL_AWS_ROLE_ARN` repository
variable.

## Local checks

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements-dev.txt
make test
make check
```

## AWS deployment

The AWS profile must be authenticated first:

```bash
aws sts get-caller-identity --profile nurserysignal --region eu-west-1
```

Bootstrap the remote state bucket once, then initialize and apply the main
stack. The commands below make the profile explicit in the shell session.

```bash
export AWS_PROFILE=nurserysignal
export AWS_REGION=eu-west-1

terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap plan
terraform -chdir=infra/bootstrap apply

make build-lambda
terraform -chdir=infra init
terraform -chdir=infra fmt -check
terraform -chdir=infra validate
terraform -chdir=infra plan -out=/tmp/nurserysignal.tfplan
terraform -chdir=infra apply /tmp/nurserysignal.tfplan
```

The migration Lambda runs the SQL migrations as part of the main Terraform
apply. The generated outputs include the API, frontend and Cognito details.
