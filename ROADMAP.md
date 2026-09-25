# NurserySignal Roadmap

This is a lightweight product roadmap, not a ticket backlog. It records the current direction, validation gates and intentionally deferred work.

## Product goal

Build a UK sales-intelligence service that surfaces timely, commercially useful signals for nursery and early-years businesses, starting with planning data and expanding only when additional sources measurably improve coverage or timeliness.

The MVP succeeds by being trustworthy and actionable, not by maximizing raw signal count.

## Phase 1 — Foundation — COMPLETE

- Serverless AWS foundation deployed.
- Python Lambda backend and PostgreSQL persistence.
- Versioned normalized signal ingestion.
- Private S3 raw evidence.
- SQS enrichment path with idempotent persistence.
- Cognito-protected internal admin UI.
- Human review flow for candidate signals.
- Terraform and GitHub Actions/OIDC deployment path.
- Deterministic shared classification rules and regression fixtures.
- Admin review UX now uses a pending inbox, separate searchable decision history and deliberate review-decision correction without changing evidence.
- Admin overview and review views support in-app data refresh without a browser reload or Cognito session reset.

## Phase 2 — Planning signal validation — COMPLETE

Primary provider: Plota.

Completed:
- Provider adapter and bounded manual collector.
- Stable planning external IDs.
- Revision handling for changed source records.
- Planning metadata rendered through the existing admin workflow.
- Initial live validation.
- False-positive handling for horticultural nursery usage.
- False-positive handling for nursery terms present only in addresses/property names.
- School-context exclusions refined without suppressing explicit early-years proposals.
- School-based nursery provision is retained as a lower-confidence commercial signal when new or expanded accommodation is explicit; incidental school references remain excluded.
- Persistence-level idempotency verified on repeated live collection.
- Bounded administrator reprocessing of stored planning evidence implemented with audit records, review-state preservation and no new ingestion artefacts.
- Production Cognito authorization verified: API Gateway supplies `cognito:groups` as a bracketed string and the exact-group check now handles it safely.
- Historical reprocess run twice by an administrator: each run re-evaluated 9 pending signals and preserved 1 reviewed record, with no new evidence or queue artefacts.
- Cognito admin-group claim normalization hardened for API Gateway string, comma-separated and JSON-array representations while retaining exact-group authorization.
- Fresh bounded Plota validation completed: 14 records returned, 11 excluded, and 3 explicit childcare candidates matched; follow-up/context exclusions were added and deployed.
- Existing invited operator added to `NurserySignalAdmins` and membership verified server-side.
- Latest bounded repeat for 2026-09-18 through 2026-09-24 returned 15 records, matched 3 explicit childcare candidates, and drained without provider errors or DLQ messages; all three remain strong genuine signals.
- Daily EventBridge schedule enabled after validation; the first bounded scheduled-equivalent runs completed cleanly.

Current gate:
- Planning validation and explicit schedule enablement are complete; monitor the first unattended runs before adding another provider.

## Phase 3 — Safe unattended planning collection — OPERATIONAL

Phase 2 demonstrated adequate precision and operational safety. The existing daily EventBridge schedule is now enabled with a bounded, overlapping collection window.

Readiness gate:
- representative live samples show strong precision
- obvious recurring false-positive classes have regression coverage
- collector and downstream processing remain idempotent
- reviewed records cannot be silently disturbed by reprocessing
- queues and DLQs remain healthy
- provider/rate-limit failures are visible
- operational cost remains proportionate
- schedule enablement was an explicit decision and is now complete

Operational follow-up:
- monitor signal quality and provider failures
- use the single aggregate collector summary log to monitor fetched, excluded, matched and queued volumes
- periodically sample rejected/excluded records for false negatives
- continue expanding the regression corpus from real-world observations

## Phase 4 — AI shadow-review validation — ACTIVE

Bedrock assessments are stored separately from deterministic classification and human review. This phase is for measuring usefulness and disagreement, not automating decisions.

The Nova Lite shadow integration uses the EU Bedrock inference profile (`eu.amazon.nova-lite-v1:0`) because direct on-demand invocation is not supported in eu-west-1.

The deployed shadow path also supports a bounded, administrator-only re-evaluation of stored signals, including reviewed history. It is idempotent for a model/prompt version and cannot change deterministic classification or human review state.

Gate before any automated decision-making:
- collect a meaningful reviewed sample across genuine, ambiguous and false-positive cases
- measure agreement, high-confidence agreement, false-approve, false-reject and NEEDS_HUMAN rates
- keep AI advisory-only until those measurements support an explicit decision

## Phase 5 — Commercial signal usefulness

Once planning collection is reliable, improve the usefulness of each signal rather than immediately adding more sources.

Likely areas:
- clearer commercial-strength classification
- distinction between genuinely new projects and stale follow-up applications
- stronger lifecycle/status context
- concise reason/explanation for why a signal matched
- operator workflow for prioritisation and follow-up
- useful search/filtering across location, authority, date, signal type and review state

Any scoring or prioritisation should remain explainable and testable.

## Phase 6 — Additional signal sources

Only add another source when it fills a demonstrated gap in planning coverage or timing.

Potential categories to investigate:
- Ofsted registration/change data
- Companies House events
- local authority procurement/tenders
- jobs/recruitment evidence
- commercial property signals
- nursery/operator website announcements

Each new source should first be validated with a bounded sample before scheduled ingestion is enabled.

## Phase 7 — Recruitment signals and opportunity correlation — IMPLEMENTED / VALIDATION PENDING

- Selected the documented GOV.UK Find an Apprenticeship Display Advert API as the first recruitment provider. It supports bounded JSON vacancy queries, stable vacancy references and an independent subscription-key access path.
- Reed was not selected for this first implementation because it requires a separate API key and its public documentation is less explicit about downstream data-use terms for this product. Specialist job boards and operator careers pages were deferred because they would add brittle, terms-sensitive HTML collection.
- Recruitment records use the canonical raw-signal/evidence path with provider-prefixed stable identities, deterministic early-years role filtering and explicit new-setting/expansion facts.
- Opportunities and signal links now support conservative v1 correlation using exact postcode plus compatible operator/nursery names, with explainable provenance and independent evidence scoring. Recruitment alone remains a weak clue and cannot create a high-confidence opening opportunity.
- The recruitment Lambda, private evidence flow and disabled daily `rate(1 day)` schedule are deployed through Terraform. The schedule remains disabled until the GOV.UK API key is configured and a bounded live sample is manually reviewed.

Validation gate:
- configure the Display Advert API subscription key in the recruitment-provider secret;
- run a bounded sample and inspect every childcare candidate and automatic correlation;
- enable the schedule only after precision, idempotency and correlation safety are demonstrated.

## Phase 7 — Customer-facing product

Defer external/customer access until the signal pipeline is demonstrably useful.

Likely requirements:
- organisations/users and tenant isolation
- saved searches or territory/preferences
- notification/digest delivery
- signal lifecycle and follow-up state
- billing/subscription model
- product analytics and feedback loop

Do not build these before the underlying signal quality justifies them.

## Explicitly deferred

- automated AI approval/rejection before the shadow-review validation gate is passed
- unbounded historical backfills
- automatic rewriting of reviewed decisions
- multiple providers collecting the same data without a clear benefit
- expensive/high-availability infrastructure before usage requires it
- public self-registration
- automated outreach to detected organisations
- enabling scheduled collectors before their validation gate is passed

## Current next step

Configure the GOV.UK Display Advert API key, run the first bounded recruitment sample, and manually validate candidate quality and opportunity links before enabling recruitment collection.
