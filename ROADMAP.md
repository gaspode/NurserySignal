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

NurserySignal planning uses `planning-shadow-v3`: genuine nursery provision inside a wider residential, mixed-use, commercial or school scheme remains commercially relevant even when nursery use is not the application's dominant component. Incidental, address-only, former-use and horticultural mentions remain excluded. Historical prompt results are preserved.

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

## Phase 7 — Recruitment signals and opportunity correlation — OPERATIONAL

- Selected the documented GOV.UK Find an Apprenticeship Display Advert API as the first recruitment provider. It supports bounded JSON vacancy queries, stable vacancy references and an independent subscription-key access path.
- Reed was not selected for this first implementation because it requires a separate API key and its public documentation is less explicit about downstream data-use terms for this product. Specialist job boards and operator careers pages were deferred because they would add brittle, terms-sensitive HTML collection.
- Recruitment records use the canonical raw-signal/evidence path with provider-prefixed stable identities, deterministic early-years role filtering and explicit new-setting/expansion facts.
- Recruitment classification now separates role evidence from setting evidence, preserves school-based nursery recruitment as relevant routine evidence, captures GOV.UK `addresses` postcodes, and distinguishes routine versus strong commercial-change evidence.
- Opportunities and signal links now support conservative v1 correlation using exact postcode plus compatible operator/nursery names, with explainable provenance and independent evidence scoring. Recruitment alone remains a weak clue and cannot create a high-confidence opening opportunity.
- Bedrock recruitment shadow assessments use versioned `shadow-v3` output with separate recruitment-relevance and commercial-change assessments; routine relevant recruitment is approved as a signal while growth evidence remains independent. AI remains advisory-only.
- The recruitment Lambda, private evidence flow and daily `rate(1 day)` schedule are deployed through Terraform. The schedule is enabled with the bounded payload `source=scheduled`, `posted_since_days=7`, `max_records=50`, `page_size=25`.
- A bounded admin-only recruitment reprocess operation is available for stored evidence and preserves human review history without creating ingestion, evidence or queue artefacts.
- Final bounded validation of the deployed provider window fetched 100 records, matched 10 relevant routine vacancies, found no explicit change signals or provider errors, and retained postcodes for all 100 records. The three stored recruitment records were reprocessed from preserved evidence and received shadow-v2 assessments; all remained relevant routine evidence with no human review-state changes.
- GOV.UK provider requests use a stable identifying User-Agent, trim secret-key whitespace, and expose only bounded/redacted provider error diagnostics for safe operational troubleshooting.

Validation gate — COMPLETE:
- Display Advert API subscription key configured privately in Secrets Manager;
- bounded live sample manually inspected with no observed false positives or incorrect planning correlations;
- stored-signal reprocessing and shadow-v2 assessment verified without duplicate ingestion artefacts;
- daily recruitment collection enabled only after the gate passed.

Operational guardrail:
- Recruitment remains supporting evidence. Routine vacancies alone do not prove a new opening or expansion; explicit change evidence or independent corroboration is required for stronger opportunity progression.

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

Operational source visibility:
- planning and recruitment collector runs are persisted as bounded summaries and exposed to administrators through the Sources page;
- manual runs invoke only the configured collector Lambdas with server-controlled limits and do not change either daily schedule.

Opportunity matching foundation:
- signals can now be grouped into generic, nursery-vertical opportunities without losing source evidence;
- deterministic postcode/name matches carry explainable outcome and confidence provenance, while uncertain matches enter an admin Match Review queue;
- administrators can link, unlink, merge and split relationships through audited, history-preserving operations;
- Opportunities, Unmatched Signals and Match Review are available in the internal admin UI;
- manual relationship corrections are authoritative, and rejected automatic links are not recreated by routine matching.
- opportunity creation is now gated separately from signal relevance: routine recruitment supports existing opportunities only, while strong material planning/change evidence can create opportunities;
- bounded administrator recalculation can demote routine-only system opportunities, promote strong unmatched planning evidence and preserve source/review history; concise change-type titles are shown in the admin UI.
- opportunity basis/creation reasons are now stored separately from per-signal match reasons, so single-signal opportunities do not present relational matching text as their reason for existence.
- bounded recalculation now performs conservative historical duplicate consolidation using stable vertical/postcode/operator or stored site identity, plus exact shared-signal identity, while preserving admin intent and relationship history.
- Match Review now resolves consolidation chains, excludes superseded opportunities and already-linked signals, and closes stale suggestions with bounded audited cleanup.

Current next step:

Continue measuring opportunity quality and admin corrections from live source evidence;
do not widen automatic matching thresholds without observed production evidence.

## Phase 8 — SignalHub shared internal engine — INCREMENTAL FOUNDATION

- Added a first-class vertical registry with active `NURSERY` and registered-but-disabled `CHILDRENS_HOME` and `DENTAL` verticals.
- Migrated SignalHub records and evidence metadata to explicit verticals, added database guards against cross-vertical signal/opportunity relationships, and scoped matching/list APIs by vertical.
- Refactored existing NurserySignal classification through the Nursery vertical policy boundary while preserving its current collectors and review semantics.
- Added the shared SignalHub admin shell/selector and an initial cross-vertical Organisations view; customer-facing CareSignal/DentalSignal products and collectors remain deferred.
- Routed bounded admin collector runs through private SQS command queues so the VPC-attached admin API can trigger collectors reliably without NAT or a fixed-cost Lambda interface endpoint.
- Made the selected SignalHub vertical an explicit, validated backend scope for review/history, opportunities, unmatched signals, Match Review, organisations, sources and overview counts; `ALL` remains an explicit admin aggregation mode and disabled verticals cannot become working contexts.
- Shared table row actions now use an accessible viewport-aware portal so operator menus remain usable inside horizontally scrollable admin tables.
- Cognito login and restoration now use the same tab-scoped storage, so ordinary page reloads preserve valid sessions without extending authentication beyond the browser tab.

## Phase 9 — CareSignal planning and recruitment — ACTIVE

- Activated `CHILDRENS_HOME` as SignalHub's second live vertical behind a dedicated CareSignal policy; DentalSignal remains registered and inactive.
- Shared Plota and GOV.UK collectors now classify each provider record independently for NurserySignal and CareSignal without cloning collector infrastructure or allowing cross-vertical matching.
- CareSignal planning requires explicit children's-home context and material opening/expansion evidence; adult/nursing care, generic C2, day nursery and unrelated residential uses remain excluded.
- CareSignal recruitment distinguishes routine supporting vacancies from explicit opening/pre-registration change evidence. Routine support-worker or manager recruitment cannot create an opportunity by itself.
- Exact residential locations are marked internal-only for future subscriber projections, while source provenance remains available to authenticated administrators.
- Evidence object identity is vertical-scoped, so one provider record can support independent NurserySignal and CareSignal records without sharing or losing document links.
- GOV.UK vacancies with malformed optional application URLs fall back to their stable provider-reference URL so one provider record cannot abort a bounded shared Recruitment run.
- Recruitment discovery now uses the Display Advert API's official vertical-specific route filters (`Education and early years` and `Care services`) because the API has no free-text search parameter. Cross-query vacancy IDs are deduplicated while discovery provenance is retained.
- Live Care-route inspection found four genuine residential-childcare apprenticeships in 76 recent Care-services vacancies; narrow `residential childcare worker` and `children's support worker` wording gaps were corrected without admitting generic/adult support work.
- Added source-specific advisory Bedrock prompt versions and a bounded stored-evidence backfill operation. AI remains advisory and cannot alter deterministic decisions or review state.

Validation gate:
- inspect a bounded recent Planning and Recruitment sample;
- manually review every CareSignal candidate and automatic correlation;
- confirm no NurserySignal/CareSignal cross-links;
- confirm queues, DLQs and existing NurserySignal schedules remain healthy;
- tune only from observed false positives or false negatives.

## Phase 10 — CareSignal regulatory and organisation evidence — VALIDATION

- Added a bounded, manual-only Ofsted collector using the official annual children’s social
  care provider register. Published redactions of home names and exact addresses are
  preserved as a safeguarding boundary.
- Ofsted URNs provide stable regulatory evidence identity. Because exact home locations are
  redacted, provider plus local-authority agreement enters Match Review rather than forcing
  a site match; an admin-confirmed link advances the opportunity to `REGISTRATION`, and a
  register row cannot create a standalone opportunity.
- Added official Companies House Public Data API enrichment to the shared organisation
  layer. Exact company number and strong unique legal-name matches enrich organisations;
  ambiguous results use a separate admin review queue.
- Companies House does not create opportunities. One company may own several separate sites,
  and organisation identity alone never merges those opportunities.
- Officer/director history is deliberately not collected because it is unnecessary for the
  current entity-resolution purpose.
- Both sources remain bounded and manual-only while production accuracy is assessed.
- Initial production validation ingested 10 current Ofsted children’s-home register records.
  No opportunities were auto-linked from redacted site data, queues drained cleanly, and an
  unchanged repeat now creates no additional evidence or enrichment work.
- Initial Companies House validation resolved 8 of 10 stored CareSignal organisation names
  through strong official legal-name matches and held 2 ambiguous results for admin review.
  Repeating those two candidates preserved 10 evidence objects and the existing review items.
- Organisation resolution review now compares cached official company profiles with the
  originating SignalHub signals, locations, aliases and opportunities. Rejected candidate sets
  remain historical and are not re-offered until the upstream candidate set materially changes.
- Ofsted regulatory evidence is explicitly outside the planning/recruitment AI shadow scope;
  SignalHub labels it not applicable and prevents cross-source prompt fallback.
- Bounded URN-specific enrichment now follows official Ofsted provider pages and latest public
  report metadata. It versions registered-provider identity separately from the annual register,
  strengthens Companies House resolution, and explicitly keeps provider registered offices
  separate from redacted home/site locations.

Initial validation gate passed:
- credentials, bounded source runs, idempotency, source health and queue/DLQ state are verified;
- two ambiguous Companies House candidates remain for explicit admin review;
- keep both sources manual-only while regulatory matching quality is assessed, then decide a
  cadence aligned with Ofsted's publication frequency and the 30-day company-profile cache.

## Phase 11 — Historical backtesting and architecture evidence — ACTIVE

Before adding more collectors or undertaking a substantial matching/data-model redesign, build a
historical backtesting framework that measures how well SignalHub could actually have discovered
and resolved known real-world openings using only information available at the time.

The backtest must explicitly prevent hindsight/data leakage. For each historical evaluation date,
SignalHub may use only source records and source-state that would genuinely have been available on
or before that date. Current Companies House state, later Ofsted registration data, later website
content, later recruitment adverts and other future evidence must not be allowed to improve an
earlier historical decision.

Start with a bounded benchmark set of known NurserySignal and CareSignal openings/registrations
from approximately 2025–2026, subject to source-history availability and reliable outcome labels.

For each known opening, capture:
- first date SignalHub could have discovered the opportunity;
- which source produced the first useful signal;
- exactly what evidence was available at that point;
- whether the operator could be identified correctly at that point;
- whether the physical site/project could be identified correctly at that point;
- when later independent evidence corroborated or contradicted the opportunity;
- how many false opportunities and Match/Organisation Review items were generated along the way;
- final outcome and whether SignalHub's opportunity lifecycle matched reality.

Aggregate at least:
- recall of genuine openings/material expansions;
- precision of generated opportunities;
- lead-time distribution, including median and useful percentiles;
- organisation-resolution accuracy;
- site/project-resolution accuracy;
- manual reviews required per genuine opportunity;
- incremental source value: discoveries/corroborations uniquely contributed by each source.

The framework should preserve per-source provenance and make benchmark runs reproducible so later
matching/policy changes can be compared against the same historical cases.

Architecture gate:
- do not begin a broad rewrite solely from theoretical design preferences;
- use backtest results to decide which weakness materially limits performance;
- specifically evaluate whether the next priority should be:
  - a first-class Site entity with UPRN/address/geospatial identity;
  - explicit Observation → Event → Opportunity separation;
  - separate organisation-resolution and opportunity-resolution scoring;
  - explainable probabilistic match weights calibrated from human decisions;
  - lifecycle derived from positive and negative/counter-evidence;
  - or another bottleneck exposed by the benchmark.

Manual Link/Reject/organisation-resolution decisions should increasingly be retained as labelled
relationship outcomes suitable for evaluation and future calibration, without making automated AI
decisions authoritative.

Until the benchmark exists:
- finish the current Ofsted/Companies House validation and enrichment refinements;
- keep CareSignal/NurserySignal production behaviour stable;
- avoid broad automatic matching-threshold changes;
- avoid adding additional sources unless they close an immediately demonstrated operational gap.

Revised sequencing:
1. Finish current Ofsted/Companies House enrichment validation.
2. Build and run historical backtesting.
3. Use measured results to prioritise data-model/matching architecture changes.
4. Only then add further collectors where the backtest demonstrates a coverage or lead-time gap.

Implementation status:
- Added isolated, versioned benchmark/run/result storage and a CareSignal-first replay engine that
  reuses production vertical classification, opportunity-creation and deterministic match logic.
- Ofsted registration is outcome truth only and is never exposed to pre-registration replay.
  Planning uses preserved publication/application provenance, Recruitment uses vacancy publication,
  and Companies House contributes only identity fields after recorded retrieval/incorporation.
- Cases without case-linked historical evidence are excluded unless complete source coverage is
  explicitly proven; unmatched generated opportunities remain unlabelled rather than being assumed
  false, and precision remains unavailable without reliable negative labels.
- Added bounded administrator seeding/runs, reproducible fingerprints, run comparison, labelled
  admin-decision export and a SignalHub Backtesting view with per-case/source metrics.
- First production run (`care-ofsted-v1`, 365-day lookback) attempted 21 authoritative 2025
  registrations. All 21 were excluded because the current archive has no case-linked,
  historically reconstructable planning/recruitment coverage; no case was incorrectly counted as
  a miss and recall, precision, lead time, organisation/site accuracy and review burden therefore
  remain unmeasurable.
- Researched all 21 labelled outcomes against preserved evidence, bounded historical Plota search
  and date-verifiable official planning/recruitment records. Corpus `care-historical-research-v1`
  retains four eligible planning items and two recruitment items across five cases; one planning
  item is deliberately outside the unchanged 365-day replay window. Two same-provider planning
  candidates were excluded because different site/local-authority evidence made the case link weak.
- The first corpus-backed production replay made four cases usable: three were detected (75%
  recall), with planning producing all three discoveries and a 290-day median lead time. The one
  missed case had two official new-home recruitment adverts whose generic Support Worker/Team
  Leader titles the current deterministic policy ignored. Precision remains unmeasurable because
  the benchmark has no reliable negative cases; no duplicate or incorrect merge was observed.
- CareSignal recruitment policy `care-deterministic-v2` now permits generic Support Worker/Team
  Leader roles when the preserved advert body independently establishes a children's-home setting,
  and treats only explicit new-home, opening, acquisition or registration-stage context as material
  change. Established-home, adult-care and weak generic “new opportunity” wording remain protected.
- Replaying the unchanged corpus and canonical 365-day window after that refinement made all four
  usable cases detectable (100% recall) and moved median lead time from 290 to 248.5 days. The
  Cumulus/Birch House case is now discovered from recruitment 97 days before registration. Its two
  same-home adverts create one duplicate replay opportunity, exposing a bounded grouping weakness;
  no incorrect merge or review item was generated.
- A read-only preview of all four current stored CareSignal recruitment records left all four as
  `RELEVANT_ROUTINE` and changed none, providing no evidence of broad routine-role inflation.
- Read-only lookback sensitivity produced 4 usable/4 detected at 365 days, and 5 usable/5 detected
  at both 450 and 540 days. The sole additional case is the known planning item 372 days before
  registration; the canonical window remains 365 days.
- Existing immutable evidence already preserves dated planning revisions, vacancy payloads,
  Companies House retrieval revisions and Ofsted register/report revisions. The historic gap is
  pre-SignalHub source coverage rather than destructive overwriting of current records.
- Current evidence still does not justify a Site/Event/probabilistic redesign: only four of 21
  outcomes are replayable at the canonical window (five at wider windows). The next priority remains
  stronger prospective source coverage and more labelled outcomes. Track the exact-workplace
  duplicate exposed by the two Birch House adverts, but do not widen automatic site matching from
  one case; rerun this fixed benchmark as coverage grows before extending it to NurserySignal.

Procurement source-value experiment:
- Added a bounded, manual-only CareSignal procurement family over the official Find a Tender and
  Contracts Finder OCDS feeds. Immutable release versions retain platform/OCID/notice identity,
  buyer and award organisations, dates, value/category metadata and source links while excluding
  contact-person details.
- Procurement is deliberately `SHADOW_ONLY`: it can be inspected in SignalHub and correlated for
  evaluation, but cannot create or advance a live opportunity. Find a Tender and Contracts Finder
  have no EventBridge schedule.
- The initial 548-day production run inspected 610 releases (309 Find a Tender, 301 Contracts
  Finder), excluded 598, and retained 12: 2 new-capacity engagement releases, 4 operator-procurement
  releases, 2 awards, 1 routine placement framework and 3 uncertain records. The eight strong
  releases represent four procurement processes because successive Hackney notices correctly
  remain versioned under one OCID.
- Representative strong records showed materially useful buyer-led intelligence: three proposed
  Camden homes, two Hackney council-owned homes with a later named operator, Hampshire's programme
  for new residential provision, and a Milton Keynes award naming an operator. A broad YPO
  placements framework was correctly kept routine, while unclear placement/Regulation 44 records
  remained uncertain.
- No strong process could be defensibly linked to the existing 21-case Ofsted benchmark or current
  stored planning/recruitment corpus, so the canonical benchmark remains unchanged. Procurement
  nevertheless demonstrated a modelling/usefulness advantage: it can identify commissioning
  authorities, home counts and intended contract timing before an operator or exact site is known.
- An unchanged production rerun retained 12 evidence objects, created no duplicate enrichment and
  created no opportunities. Queues and DLQs remained clear.
- Gate decision: `KEEP_AS_MANUAL/SHADOW_SOURCE`. The seeded evaluation proves potential value but
  does not yet provide an unbiased prevalence/noise estimate or benchmark lift sufficient to enable
  scheduling. Re-run bounded prospective samples and link outcomes as they emerge; do not redesign
  the opportunity model or automate procurement creation from this sample.
- This experiment does not block the next major product step. Proceed with a bounded customer-facing
  CareSignal MVP while procurement remains an internal evaluation source and the historical
  benchmark accumulates prospective coverage.

## Phase 12 — CareSignal commercial pilot MVP

Status: deployed to production on 2026-09-28; pilot curation and one real-customer smoke test are
the active gate.

- Added a separate CareSignal customer experience over a strict server-side projection. SignalHub
  remains admin-only; customers cannot access admin APIs, internal review/debug state, rejected
  opportunities, private evidence, benchmark/source tooling or procurement shadow records.
- Added tenant accounts and named users, `STARTER`/`PRO`/`BUSINESS` entitlements, server-enforced
  Starter geography, personal watchlists, simple Pro saved searches, alert preferences, product
  usage events and idempotent digest-run history. Pricing and self-service billing remain deferred.
- Customer opportunity publication is explicit and audited. An item must be CareSignal,
  non-rejected/current, explicitly `PUBLISHED`, and supported by approved non-procurement evidence.
  No existing opportunity is automatically exposed by migration.
- Delivered customer Opportunities, Saved, Alerts and Account views with concise titles, plain
  language stage/strength labels, first/latest dates, customer-safe geography, organisation facts,
  explainable evidence timelines and official public links. `INTERNAL_EXACT` locations expose only
  outward postcode/area and never reconstruct Ofsted-redacted home addresses.
- Added a bounded weekly digest path using the existing VPC backend for database projection, SQS,
  and one non-VPC SES sender Lambda. This avoids NAT or another paid VPC endpoint. As no SES sender
  identity is currently verified, the deployed schedule/consumer must remain disabled and the UI
  offers digest preview only until `caresignal_email_from` is configured.
- Added SignalHub Customers administration for manual account invitation, plan/geography assignment,
  suspension/reactivation and customer-readiness counts; customer publication is managed on the
  existing opportunity detail page.
- Procurement remains manual/shadow and is explicitly excluded from customer eligibility/timelines.
  Historical benchmarking continues prospectively and does not block pilot discovery interviews.
- Production migration `0022` and the customer/API/frontend infrastructure were deployed from
  commit `07b7d34a`; the API/database and CloudFront frontend are healthy, unauthenticated customer
  and admin requests return `401`, the post-deployment Terraform plan is clean, and ingestion,
  enrichment, customer-digest and collector DLQs are empty. Planning and Recruitment remain enabled
  at their existing daily cadence. The weekly customer digest rule and its queue consumer remain
  intentionally disabled because no SES sender identity is configured.
- No historical opportunity was auto-published. Customer-visible inventory therefore starts empty
  by design and must be explicitly curated in SignalHub before a supplier account is invited; this
  is the remaining content-quality gate rather than an ingestion or deployment failure.

Pilot gate:
- deploy migration/code and confirm production health;
- curate and explicitly publish the small set of CareSignal opportunities whose evidence is clear
  enough for supplier conversations;
- verify a real customer invitation, Starter geography isolation and account separation;
- verify an SES sender and exercise one weekly digest before representing email delivery as live;
- then invite 5–10 suppliers for paid-pilot conversations and measure viewing, saving, source-link
  and alert usage before adding CRM, billing, exports or larger matching architecture.

Paid-pilot activation check (2026-09-28):
- Reviewed all 13 current, non-rejected CareSignal opportunities through the bounded customer-safe
  curation projection. Published six explicit new-home Planning opportunities: Wolverhampton,
  Sandwell, Bolton and three distinct Liverpool postcode areas. All six are `PLANNING` / `OPENING`,
  have approved evidence and working official source links, and were first detected on 26 September.
- Deferred seven rather than padding the launch inventory: two new-home conversions are currently
  mislabeled as expansions, three are condition/follow-up records with potentially stale commercial
  timing, one lawfulness record has ambiguous existing/proposed wording, and one otherwise strong
  Milton Keynes record has a broken public TLS certificate from the authority portal.
- Customer-safe generated titles now add only the outward postcode, making same-authority records
  distinguishable without exposing an exact CareSignal residential address. Digest presentation now
  uses a CareSignal sender display name and includes a link to alert preferences.
- Added a bounded IAM-only curation inventory/explicit publication operation and the practical
  `docs/caresignal-pilot-operations.md` runbook. There is still no bulk customer/public publication
  API and every publication uses the existing audit trail.
- Production remains healthy and drift-free after deployment. Planning and Recruitment retain their
  daily schedules; ingestion, enrichment, digest and collector DLQs are empty.
- Public customer branding is now **CareProspect** while `CHILDRENS_HOME`, SignalHub, benchmark IDs,
  Cognito group names and historical technical identifiers remain stable. `careprospect.co.uk` was
  registered for one year under the existing business ownership, with auto-renew and supported
  contact privacy enabled. Route 53 delegation, an ACM-managed TLS certificate, CloudFront alias and
  customer API CORS are live; the customer portal and digest links use the branded HTTPS domain.
- SES `eu-west-1` now has a verified `careprospect.co.uk` domain identity, successful Easy DKIM,
  aligned `bounce.careprospect.co.uk` custom MAIL FROM/SPF, and cautious DMARC `p=none`. The sender is
  `CareProspect <alerts@careprospect.co.uk>` with no unmonitored reply-to. The weekly Monday 08:00 UTC
  digest rule and SQS consumer are enabled, idempotency is unchanged, and the queue/DLQ are empty.
  SES production access has been requested accurately but remains pending/sandboxed.
- The six explicitly published Planning/Opening opportunities remain customer-visible under the new
  brand (Wolverhampton 1, Sandwell 1, Liverpool 3, Bolton 1); all retain at least one customer-safe
  evidence item. Procurement remains manual/shadow and excluded.
- Gate remains `NOT_READY_FOR_PAID_PILOT`: no separate authorised pilot recipient/customer identity
  exists. The sole existing internal Cognito identity is already the SignalHub administrator and
  cannot be reused as an isolated customer tenant. Consequently no real invite/digest can be sent and
  the clean-session Starter geography, tenant-isolation, suspension/reactivation and delivery-header
  checks remain pending. Exact next step: supply one controlled, non-admin test recipient, verify it
  in SES while sandboxed, provision it as a Starter account, and complete the documented end-to-end
  gate. Commercial outreach starts only after that succeeds; benchmarking continues prospectively
  and larger architecture work remains deferred.
- The subsequently supplied controlled address was submitted for SES sandbox-recipient verification,
  but it resolves to that existing SignalHub administrator account. It was therefore not added to the
  customer group or tenant tables: doing so would make the isolation test meaningless. SES recipient
  verification remains pending; use a distinct non-admin mailbox (or a confirmed mailbox alias that
  Cognito can treat as a separate email username) for the pilot account.
- A distinct controlled recipient, `willypayne@gmail.com`, is now verified for SES sandbox delivery.
  The first bounded provisioning attempt exposed a real infrastructure defect: the transactional
  backend runs in private subnets and could not reach the Cognito control plane, so the request timed
  out before creating either an identity or tenant. Customer invitation now uses an encrypted,
  DLQ-backed bounded queue and a least-privilege non-VPC identity worker; the worker calls the backend
  synchronously for idempotent tenant/preferences/audit persistence and rolls back only identities it
  created when persistence fails. The admin UI reports the invitation as queued rather than delivered.
  The first production apply safely stopped when the GitHub OIDC role lacked `iam:TagRole` for the new
  provisioner role; the scoped deployment policy and dependency ordering were corrected before retry.
  The first queued live attempt then exposed Cognito's distinct create/read attribute keys; rollback
  removed the transient identity and no tenant state was written. Normalization now supports both
  official response shapes. Production completion and the real invite/digest/customer-session checks
  remain the immediate gate.
- The isolated Starter identity and tenant were then provisioned successfully with Liverpool-only
  access; provisioning and DLQ queues drained. The first digest generated and queued exactly once,
  but SES rejected the worker call despite verified domain/recipient and an allowed IAM simulation.
  Delivery retry was paused and bounded, email-redacting SES diagnostics were added to identify the
  provider rejection before retrying. A delivered digest and customer first-login remain launch gates.
