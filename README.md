# NurserySignal

NurserySignal is a UK sales-intelligence service for detecting nursery openings,
expansions and related commercial signals.

This repository contains the first deployable foundation:

- Terraform bootstrap and application infrastructure in `infra/`.
- A small Python 3.12 Lambda backend in `backend/`.
- SQL migrations and the initial relational model.
- A normalized signal ingestion contract.
- A fixture-driven authenticated signal ingestion and enrichment flow.
- A minimal static frontend served through CloudFront.
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
- `GET /admin/signals/{id}`
- `POST /admin/signals/{id}/approve`
- `POST /admin/signals/{id}/reject`

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
