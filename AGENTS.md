# AGENTS.md

## Purpose

NurserySignal is a UK sales-intelligence product for detecting commercially useful nursery openings, expansions and related early-years signals. Changes should improve signal quality, operator confidence and safe automation without sacrificing traceability.

## Working principles

- Prefer precision over volume. A smaller set of high-confidence commercial signals is more valuable than noisy matching.
- Keep source ingestion, normalization, classification, enrichment and review concerns separate.
- Reuse shared classification logic rather than creating provider-specific copies of the same rules.
- Treat raw source evidence and review history as durable records. Do not rewrite history to make new logic appear retroactive.
- Make reprocessing explicit, bounded, idempotent and auditable.
- Preserve existing reviewed decisions unless a task explicitly defines safe migration semantics.
- Do not enable schedules, automatic background processing, destructive cleanup or irreversible production behavior unless explicitly requested.
- Do not add new external providers, paid services or infrastructure costs without making the change explicit.

## Architecture

Current foundations are described in README.md. Keep these boundaries:

- Python 3.12 Lambda backend under backend/.
- PostgreSQL schema changes via ordered SQL migrations in backend/app/sql/.
- Provider-independent planning interfaces under backend/app/planning.py.
- Shared deterministic relevance/classification logic under backend/app/classification.py.
- Raw evidence remains private in S3.
- SQS-backed enrichment remains idempotent.
- React/Vite admin UI lives under frontend/.
- Terraform is the source of truth for AWS infrastructure under infra/.

Avoid bypassing existing layers just because a direct implementation is quicker.

## Data and history

- Stable provider IDs must remain stable.
- Duplicate observations must not create duplicate canonical signals.
- Material source changes should use the existing revision/evidence model rather than overwrite provenance.
- Manual ACCEPTED/REJECTED or equivalent review decisions are historical facts and must not be silently reset.
- Reclassification of historical records must not masquerade as fresh ingestion.
- New admin mutations that materially affect stored state should be auditable.
- AI shadow assessments are advisory evidence only; they must never mutate deterministic fields or human review state.

## Classification rules

- Horticultural, garden, tree, plant and nursery-stock usage is not childcare.
- A nursery term appearing only in an address, property name or unrelated organisation name is insufficient.
- School-based nursery provision can be commercially useful; require evidence of new, expanded or newly accommodated provision, while excluding only incidental or stale school references.
- Incidental references to proposed/nearby childcare provision in a wider development are weak evidence.
- Non-material amendments, condition discharges and similar follow-up applications may describe genuine nursery projects but can be commercially stale; do not promote them merely because the underlying project is relevant.
- Explicit early-years/day-nursery/pre-school opening, conversion, expansion or capacity-change evidence should remain detectable.
- Add regression cases whenever a live false positive or false negative causes a rule change.

## Security

- Never print, log, commit or expose API keys, passwords, Cognito credentials, database credentials or secret values.
- Use Secrets Manager and existing IAM patterns.
- Keep raw evidence private; access should remain authenticated and time-limited.
- Do not weaken authentication/authorization to simplify testing.
- Admin-only operations must enforce authorization server-side.

## Database changes

- Add a new ordered migration; do not edit an already-deployed migration.
- Prefer additive, backwards-compatible changes where practical.
- Preserve auditability and provenance.
- Consider production data already present when adding constraints.
- Do not delete or rewrite production records merely to make a migration convenient.

## UI/UX

The admin UI is an operator tool, not a demo.

- Prefer clear workflows, obvious state and concise actions.
- Avoid exposing implementation details where an operator decision is what matters.
- Preserve useful planning metadata and provenance in the signal detail view.
- Destructive or history-affecting actions require clear intent and safe backend semantics.
- Do not add UI controls for operations that are unsafe to run unattended.
- Operational triage workflows should behave like queues/inboxes: handled items leave the active queue, history is separate, and review confirmations use accessible in-app dialogs rather than browser-native prompts.

## Validation before completion

Run the relevant checks for every change:

- backend tests
- frontend tests when frontend code changes
- Ruff/linting
- frontend build when frontend code changes
- terraform fmt -check
- terraform validate
- terraform plan for infrastructure-affecting work

For live/provider work, also verify queue/DLQ health, idempotency and that secrets were not exposed.

## Deployment

Use the repository's existing GitHub Actions/OIDC deployment path. Do not create parallel deployment mechanisms without a specific reason.

If a task includes a live validation, report:
- what was changed
- tests/checks
- commit hash
- deployment result
- production health
- observed signal-quality results
- remaining risks or false-positive classes
- whether the next automation gate is actually ready

## Roadmap discipline

Update `ROADMAP.md` at the end of every piece of work, including documentation-only, code-only, infrastructure, deployment and live-validation work. Record the resulting phase status, passed/failed/pending gates, remaining blockers and exact next step. A task is not complete until the roadmap reflects the new state. Keep it lightweight and strategic rather than turning it into a detailed ticket backlog.
