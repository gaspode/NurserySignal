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
- periodically sample rejected/excluded records for false negatives
- continue expanding the regression corpus from real-world observations

## Phase 4 — Commercial signal usefulness

Once planning collection is reliable, improve the usefulness of each signal rather than immediately adding more sources.

Likely areas:
- clearer commercial-strength classification
- distinction between genuinely new projects and stale follow-up applications
- stronger lifecycle/status context
- concise reason/explanation for why a signal matched
- operator workflow for prioritisation and follow-up
- useful search/filtering across location, authority, date, signal type and review state

Any scoring or prioritisation should remain explainable and testable.

## Phase 5 — Additional signal sources

Only add another source when it fills a demonstrated gap in planning coverage or timing.

Potential categories to investigate:
- Ofsted registration/change data
- Companies House events
- local authority procurement/tenders
- jobs/recruitment evidence
- commercial property signals
- nursery/operator website announcements

Each new source should first be validated with a bounded sample before scheduled ingestion is enabled.

## Phase 6 — Customer-facing product

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

- broad AI/LLM classification as a substitute for deterministic rules
- unbounded historical backfills
- automatic rewriting of reviewed decisions
- multiple providers collecting the same data without a clear benefit
- expensive/high-availability infrastructure before usage requires it
- public self-registration
- automated outreach to detected organisations
- enabling scheduled collectors before their validation gate is passed

## Current next step

Monitor the first unattended daily runs and sample accepted/rejected records for precision, false negatives and provider failures before considering another signal source.
