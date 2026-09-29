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
- Phase 11 expansion `care-ofsted-v2` is implemented as a separate 50-outcome benchmark (29
  additional authoritative outcomes) with `care-historical-research-v2`; v1 remains immutable.
  The bounded research pass retained all existing eligible evidence and added two strong,
  date-verifiable official planning links (Leicestershire and Sandwell). Provider-only,
  same-authority and undated findings remain excluded because redacted Ofsted geography cannot
  establish site identity. The replay now reports observed min/max, fixed lead-time bands,
  source-specific quartiles, exclusion counts, and an internal marketing-safe summary with up to
  eight representative cases.
- The v2 production replay attempted all 50 outcomes. At the canonical 365-day window, 5 cases were
  honestly reconstructable and all 5 were detected: median lead time 207 days, p25 97, p75 290,
  p90 324.8, and observed range 96–348 days. Planning first discovered 4 cases (median 248.5 days)
  and recruitment uniquely discovered 1 (97 days). The distribution was 0 under 90 days, 2 at
  90–179, 1 at 180–269, 2 at 270–364, and 0 at 365+.
- At both 450 and 540 days, 7 cases were reconstructable and detected: median 290 days, p25 152,
  p75 360, p90 378.8, and range 96–389 days. Planning uniquely discovered 6 (median 319 days;
  p25 227.75, p75 366), while recruitment uniquely discovered 1. Two cases were admitted solely by
  the wider window. No case had both source families. Forty-three outcomes remain excluded because
  the historic archive cannot distinguish no early signal from unavailable source history.
- Precision remains unavailable because there are no reliable negative labels. The replay produced
  no incorrect merge or review item, but the two Birch House recruitment adverts still produce one
  duplicate opportunity. Organisation resolution was correct for 4/7 wider-window cases. Site
  accuracy remains unmeasurable/zero against the two cases with independent site truth, confirming
  that source coverage and site identity are the main evidence gaps rather than classifier recall.
- An immediate unchanged rerun reused the same run IDs for all three windows, proving replay
  idempotency. The production API/database is healthy, every DLQ is empty, scheduled Planning and
  Recruitment collection is unchanged, and the post-deployment Terraform plan is clean. Marketing
  wording must remain bounded: “Across 7 reconstructable historical registrations, relevant public
  signals were available a median of 290 days before Ofsted registration.” Always retain the caveat
  that this is a small reconstructable sample, not an average for all openings or a future promise.
- Gate decision: the requested 25-case usable sample was not achievable honestly from current
  historic coverage. Continue prospective immutable retention and targeted official-source corpus
  expansion; do not tune matching or publish population-level claims from seven cases. The measured
  next engineering issues are historic/source coverage first and the known same-site recruitment
  duplicate/site-identity weakness second; neither yet justifies a broad architecture rewrite.

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
- Safe diagnostics established that SES sandbox authorization evaluates the verified recipient identity
  as well as the sender. The sender role is therefore allowed over SES identities only when the exact
  configured `alerts@careprospect.co.uk` From address is used; it cannot send from another identity.
  This replaces the sender-domain-only resource statement that rejected the controlled recipient.
- The corrected delivery completed successfully: SES accepted one branded weekly digest for the
  authorised pilot recipient, a repeat run reported one idempotent duplicate and queued no second
  email, and digest/provisioning/live ingestion queues plus every DLQ are empty. The weekly Monday
  08:00 UTC rule remains enabled; Planning and Recruitment remain enabled at `rate(1 day)`. Production
  API/database and branded HTTPS health checks pass, and a fresh workflow plan reported `No changes`
  with `0 added, 0 changed, 0 destroyed`.
- Remaining paid-pilot gates are external/user-visible: complete and confirm the invited customer's
  first-login password challenge and clean-session journey, inspect the delivered Gmail headers for
  SPF/DKIM/DMARC alignment, and obtain SES production access (still pending/sandboxed) before inviting
  unverified supplier recipients. Until those checks pass, status remains `NOT_READY_FOR_PAID_PILOT`.
- Pilot login validation exposed four Cognito invitation emails whose temporary passwords had been
  invalidated by identity rollback/recreation during three provisioning retries. Provisioning now
  retains a successfully created Cognito identity when downstream tenant persistence fails: the
  encrypted SQS retry reuses that identity and the transactional account write, preventing repeated
  invitations and temporary-password invalidation. The fix was deployed and one explicit replacement
  invitation was sent at 16:21 UTC; only that newest temporary password should be used for the
  first-login challenge. Customer provisioning, digest, ingestion and enrichment queues/DLQs are empty.
- A production opportunity recalculation reached the API Lambda's exact 15-second timeout after the
  multi-vertical evidence set grew. Recalculation now remains synchronous but runs as sequential,
  selected-vertical batches of at most 25 signals (100 total), performs global maintenance only in
  the first batch, aggregates accurate counters and refreshes the opportunity list on completion.
  Signal-detail Approve/Reject and deliberate decision corrections now execute immediately without a
  confirmation dialog; bulk review and destructive opportunity operations retain confirmation.
- Organisation resolution now reuses stored URN-specific Ofsted provider identity and registered-
  office evidence without treating that office as a children's-home site. Exact provider-office
  postcode agreement is explicitly ranked above name-only candidates, appears as an explainable
  admin reason, and can safely re-score an existing pending review without duplicating it; region-only
  agreement remains insufficient. The Companies House manual source can re-evaluate pending reviews
  against newer Ofsted evidence under the existing automatic-resolution threshold.
- Organisation Review now also supports a bounded official Companies House number lookup. Lookup is
  performed by the existing internet-facing Companies House collector under least-privilege Lambda
  invocation through a private Lambda VPC endpoint, persists the profile as a manual candidate, and
  requires a separate confirmation before changing organisation identity. Suggested selection,
  manual-number selection, automatic resolution
  and rejection have distinct audit provenance; aliases and prior review history remain intact.
- Customer-safe CareProspect projections remain unchanged and cannot expose Ofsted provider-office
  street addresses or internal match reasoning. Production deployment and a real pending-review
  verification are the remaining gates for this focused organisation-resolution improvement.
- The first deployment safely stopped after updating application packages when the GitHub OIDC role
  lacked permission to create the required private Lambda endpoint. The scoped deploy policy now
  permits VPC-endpoint create/modify/delete and their resource tags in addition to its existing EC2
  reads, and Terraform explicitly updates that self-managed policy before creating the endpoint.
- Final health checks found four Ofsted URN-enrichment messages in the ingestion DLQ. The first pass
  removed only PostgreSQL-incompatible NUL bytes from normalized database text while leaving immutable
  source evidence and content identity unchanged. A bounded redrive then showed that two malformed PDF
  fields also contained run-on report text too large for an indexed organisation alias. Extracted
  organisation names now have a conservative identity-boundary check: malformed run-on text is not
  admitted as organisation identity, while the original report evidence remains preserved. Two items
  completed on the first redrive and the final two completed after the corrected worker deployment;
  all production queues and DLQs are now empty and the post-deployment Terraform plan is clean.
- A live read-only check of URN-specific evidence then identified the malformed PDF delimiter behind
  those run-on names: some reports repeat `Registered provider:` at the end of the document. The
  source-aware parser now treats that repeated label as a boundary and is versioned `ofsted-urn-v3`;
  the Bright Path-style production case extracts the concise registered-provider identity and its
  provider-office postcode without exposing or inferring a children's-home address.
- A one-record production run for Ofsted URN `2806691` fetched and persisted the corrected v3
  enrichment with no errors; the ingestion queue drained and its DLQ remained empty. The official
  Companies House number lookup also returns the active legal entity with exact provider-office
  postcode agreement. The pending review remains admin-authoritative and was not silently resolved.
- Companies House candidate discovery now performs bounded searches across the observed provider,
  its normalized form, the URN-enriched Ofsted registered-provider name and stored organisation
  aliases, then deduplicates results by company number before applying the unchanged resolution
  thresholds. Discovery provenance is retained with immutable enrichment evidence; provider-office
  identity remains strictly separate from opportunity/site geography. Production refresh of the
  existing URN `2806691` review was the final validation gate for this refinement.
- The first bounded production refresh still returned the previous five weak candidates because the
  official Ofsted identity and Companies House legal name differ in token spacing. Discovery now also
  tries one generic, bounded leading-token spacing variant for the authoritative Ofsted provider name
  and requests at most ten results per query; no matching or automatic-resolution threshold changed.
- The final bounded URN `2806691` refresh expanded the review from five to ten unique candidates and
  automatically discovered company `13962842` at rank one. Exact provider-office postcode and locality
  agreement plus plausible incorporation timing raised it to `PROBABLE`, below the unchanged automatic
  resolution gate, so admin confirmation remains required. An immediate unchanged rerun reused the
  same content-addressed evidence, created no duplicate candidate/review state, and left queues/DLQs
  empty.
- Final organisation-resolution hardening canonicalizes only recognised UK corporate suffixes
  (`Ltd/Limited`, `PLC/Public Limited Company`, and `LLP/Limited Liability Partnership`) while
  retaining substantive name tokens and every raw observed name. Admin candidate evidence now
  distinguishes exact normalized legal identity, exact Ofsted provider-office postcode, compatible
  provider-office address/locality and incorporation timing, and marks a materially stronger candidate
  as best supported without changing the automatic-resolution thresholds. Missing URN-specific
  evidence can be requested from a pending review through a bounded, audited, deduplicated Ofsted run;
  the review remains usable if that enrichment fails. The first bounded Bedspace validation found the
  expected company as a strong match and exposed one final provenance omission: the stable evidence
  sanitizer retained human-readable reasons but not the new machine-readable match feature/best-match
  flag. Those fields are now retained as well. Production Bedspace/Bright Path verification is the
  final gate for this pass.
- The final hardening pass is deployed. A bounded URN `2813108` refresh added the missing Bedspace
  provider evidence, and the unchanged matcher selected company `04457083` as `STRONG` at `0.99` with
  exact normalized legal name, exact provider-office postcode, compatible address/locality and
  pre-registration incorporation evidence. The candidate is marked best supported and no suffix-
  mismatch wording remains. An unchanged repeat reused the same content-addressed result and created
  no additional evidence version. Bright Path company `13962842` remains rank one across ten unique
  candidates with provider-office postcode/locality/address and incorporation corroboration; its
  spacing difference remains honestly `PROBABLE`, with no threshold relaxation. Both named records
  had already been authoritatively resolved by an administrator, so validation preserved those
  decisions rather than recreating pending reviews. API/database health passes, the post-deployment
  Terraform plan is clean, and every production queue and DLQ is empty.
- The CareProspect commercial-validation milestone now includes a public, product-led website using
  the approved Planning Register visual system: editorial typography, restrained stage colours,
  realistic customer-safe opportunity records, benchmark caveats, indicative pilot pricing, FAQ,
  evidence commitments and responsive customer sign-in. The existing authenticated portal shares
  the same visual language without changing customer entitlements, tenant isolation or SignalHub.
  Public pilot requests are bounded, deduplicated and stored separately from customer accounts for
  authenticated SignalHub review; no self-service signup, billing or unauthenticated admin action was
  introduced. Commit `ef79251` is deployed through the normal GitHub Actions/OIDC workflow. Live
  desktop and iPhone-width checks confirm that the homepage, pricing, FAQ, request-access and customer
  sign-in views preserve the approved design without clipping or horizontal overflow; public metadata,
  HTTPS, API/database health and the idempotent request flow also pass. The post-deployment Terraform
  plan is clean, daily planning/recruitment and weekly digest schedules remain enabled, and all primary
  queues and DLQs are empty. Commercial pilot recruitment is now the primary milestone while
  procurement and larger matching architecture remain deferred.
- The CareProspect desktop hero now uses bounded, content-driven spacing instead of reserving the
  browser viewport. Wide-screen copy and opportunity preview remain balanced, the timing section
  follows the actual content, and the layout stacks at 900px to avoid a cramped tablet composition.
  Frontend regression coverage prevents viewport-height sizing from returning. Commit `71dca1a` is
  deployed through the normal workflow; live checks at 1280, 1440, 1920, 1024, 768 and 390 pixels
  confirm content-driven flow and no horizontal overflow, completing this focused correction.
- CareProspect customer branding now uses the approved simplified mark: one planning-ochre dot with
  the dark serif wordmark. A shared native CSS/text component covers public header/footer, customer
  portal and sign-in variants, while the favicon is reduced to the same dot on deep green. The former
  three-stage line/square/diamond artwork has been removed from customer-facing code; SignalHub
  branding is unchanged. Commit `ba52ccf` is deployed through the normal workflow; live desktop,
  tablet, iPhone-width and sign-in checks confirm crisp proportions, unwrapped navigation and no
  horizontal overflow, completing the brand update.
- The public timing section now presents the expanded Phase 11 evidence as a dark, Gantt-style
  opportunity timeline rather than four individual benchmark bars. Planning shows its 96–389 day
  observed range, 228–366 day middle 50% and 319-day median; Recruitment remains explicitly one
  discrete 97-day example; Ofsted registration is the zero-day endpoint. Main-page sample counts
  have been removed in favour of a restrained reconstructable-history qualification. Desktop uses a
  shared six-point axis, while mobile stacks the same rows so every marker and the registration
  endpoint remain visible without page or local horizontal overflow. No customer-product, benchmark,
  matching or portal behaviour changed.
- The timing timeline has received its focused production-polish pass. Ofsted registration now owns
  a reserved endpoint column and the one-month tick remains independently readable at desktop,
  compact desktop and stacked mobile widths. Planning annotations use endpoint labels, a primary
  median flag and a quieter middle-50% band label; Recruitment uses a ring, stem and labelled point
  without implying a range. Summary cards now separate planning range, planning median and the
  recruitment example, and the public qualification uses concise reconstructed-history wording.
- A separate, admin-only Planning historical-backfill path now accepts an explicit date range of at
  most 550 days and processes it as a serial chain of weekly Plota chunks. It reuses the live NurserySignal
  and CareProspect classifiers, ingestion, matching and revision semantics while leaving the scheduled
  two-day run and its normal 31-day query guard unchanged. Source application dates are retained as
  historical discovery dates, retrieval remains separately timestamped by ingestion, run/chunk provenance
  is durable, customer publication remains explicitly gated, and a bounded idempotent finalisation action
  reprocesses current Recruitment before opportunity recalculation. Production dry-run quality, the full
  18-month counts, Recruitment correlation impact and customer-curation candidates remain the operational
  gates before deciding whether any further data work is justified.
- Production dry-run validation stopped safely at the provider gate. Three 60-day attempts returned
  Plota `429` responses before fetching or queueing any records: the first exposed the former 60-second
  collector timeout, and subsequent retries confirmed that the current Plota account quota rejects even
  the proven 25-Nursery/25-Care request envelope. Historical chunks now use that low-volume envelope,
  wait 60 seconds between successful chunks, and stop after Plota's own bounded retry budget without
  repeatedly redriving a quota failure through SQS. The full 550-day run and Recruitment recalculation
  remain deliberately unstarted. The exact next step is to confirm/reset or raise the Plota historical
  API quota, then rerun the 60-day gate before authorising the full backfill; matching architecture does
  not need changing on the available evidence.
- A bounded provider-only diagnostic confirmed the gate is Plota's exhausted one-off Demo record
  allowance, not per-minute throttling or malformed historical querying. Both a seven-day historical
  request and the normal two-day live-query equivalent returned `429 rate_limit_error`, `Retry-After:
  86400`, and the explicit message that all 500 Demo records have been used; Plota's current official
  documentation confirms Demo is capped at 500 one-off records/requests and excludes pre-2026 history.
  The daily collector remains enabled but cannot receive previously unseen records with the present key.
  The single next action is to move the Plota account to an appropriate paid commercial plan before any
  further dry run; because CareProspect is a customer-facing multi-tenant product and the bounded seed can
  require more than 10,000 records, confirm the required commercial tier/allowance with Plota rather than
  assuming Starter is sufficient.
- Plota Starter access is now confirmed and the historical Planning gate passed: the fixed 60-day range
  returned 333 records and 213 vertical classifications on both runs, with unchanged database totals on
  the idempotency repeat. The full 29 March 2025–29 September 2026 run completed all 79 weekly chunks from
  its persisted checkpoint, fetching 3,268 records and producing 857 NurserySignal plus 1,320 CareProspect
  classifications with zero collector errors. A repeatable SQS event-source stall was contained by
  disabling the consumer, purging only the explicitly authorised Planning manual queue, and serially
  pumping chunks 33–79; chunks 1–32 were not re-queried and all other queues/DLQs were left untouched.
  The existing post-backfill recalculation then exposed a genuine 15-second backend timeout; commit
  `5661953` raises only the backend bounded-admin-operation timeout to 60 seconds, with no matching-policy
  change, and is deployed through the normal OIDC workflow. The retry completed: 100 current Recruitment
  signals were reprocessed (96 relevant, four excluded), 200 vertical-scoped calculations reused 119
  existing opportunities, and no new relationship, merge or Match Review item was produced. Unmatched
  Recruitment therefore remains 104; the backfill added historical context and inventory, but did not
  produce a defensible new Recruitment-to-Planning correlation under current rules.
- Production now contains 864 NurserySignal and 1,321 CareProspect Planning signals in the backfill
  window, with 621 and 1,135 opportunities respectively. The prior all-vertical baseline was 196
  opportunities, so the run added 1,560 internal opportunities while leaving the six explicitly published
  CareProspect records unchanged. The newest bounded sample of 100 CareProspect drafts contains no
  publication-ready record because its evidence is still unreviewed; curation, rather than more source
  ingestion, is the next data-quality step. One bounded Companies House batch attempted ten organisations,
  resolving eight exact/strong and sending two to review with no errors. Plota reports 3,269/10,000 monthly
  records used and 6,731 remaining. API/database health, HTTPS, daily Planning/Recruitment schedules, the
  restored Planning consumer and the zero-drift Terraform plan all pass. All operational queues are empty;
  five retained shared collector DLQ messages were deliberately not purged under the operator's explicit
  safety constraint and require separate scoped triage if cleanup is desired.
- The focused post-backfill review-hardening code is deployed at commit `d4f145e`. Explicit structured
  council decisions of Refused/Rejected/Permission Refused/Application Refused now move only pending
  Planning signals to rejected history through an idempotent audited policy; raw evidence, decision
  dates, source links and later independent applications remain intact. Withdrawn, invalid, lapsed and
  other ambiguous terminal states are deliberately not auto-rejected. SignalHub Review Inbox now shows
  vertical-scoped risk buckets for deterministic/AI agreement, disagreement, uncertainty and manual
  review. A bounded safe-agreement bulk action is present but execution remains disabled unless at least
  20 existing human decisions show zero errors at a conservative threshold; AI rejection remains
  advisory only and customer publication remains unchanged.
- A read-only bounded Recruitment-to-Planning diagnostic now categorises up to 50 unmatched CareProspect
  recruitment signals using concrete postcode, locality, operator/applicant and stale/non-material
  evidence, without creating links or changing matching thresholds. The matching diagnostic, production
  refusal cleanup, human-label evaluation and safe-approval preview were subsequently run through five
  explicitly authorised, narrowly scoped direct Lambda admin invocations (including before/after triage
  captures). The cleanup inspected 1,782
  pending Planning signals and audit-rejected all 205 explicit council refusals with no errors; 79
  Withdrawn records remained pending by policy. The active Planning queue fell to 1,577 while preserving
  205 automatic reviews in history. Of 403 prior human reviews, AI agreed with 97.1% of NurserySignal and
  99.0% of CareProspect decisions, but only five labelled records met the complete 0.95 deterministic-plus-AI
  approval policy, so no safe threshold is recommended. A preview identified 64 currently eligible records
  but correctly disabled execution; no bulk approvals were performed. The requested 40-record Recruitment
  diagnostic found only four current records satisfying every relevant/unmatched eligibility condition,
  and all four had no plausible Planning candidate. This supports continued manual review and prospective
  label collection; it does not yet justify threshold changes or a focused matching rewrite. Infrastructure
  and application health pass; Planning and Recruitment daily
  schedules and the Planning manual-run consumer are enabled, primary queues and four non-collector DLQs
  are empty. The five retained collector DLQ messages were inspected without deletion: two are obsolete
  early dry-run first chunks, two are obsolete completed-backfill continuation chunks (17 and 33), and one
  is an obsolete two-day manual run from the exhausted Demo-key period. Their safe cleanup recommendation
  is purge/delete only those five known obsolete messages after separate operator approval.
- Planning refusal policy v2 now recognises additional exact normalized structured decisions including
  Refuse Permission/Consent, Refuse Permission, Refuse Consent, Refusal of Permission/Consent and the
  equivalent reversed refusal wording, without inspecting free-text proposals or broadening treatment of
  Withdrawn/Invalid/appeal/follow-up states. The bounded production cleanup inspected 1,566 pending
  Planning signals and audit-rejected nine newly recognised refusals (five Refuse Permission/Consent and
  four Refuse Permission casing variants), with zero errors; the server-side refusal cohort is now empty
  and 79 Withdrawn records remain pending by policy. Review Inbox triage counts are now clickable and
  backed by a validated `triage_bucket` API filter using the same shared evaluator as the summary. The
  current SAFE_APPROVE_AGREEMENT cohort is exactly 64 pending Planning signals; admins can review it
  sequentially with filter/pagination state and a live remaining count preserved. No automatic or bulk
  approval was run or newly enabled, so the next gate remains manual validation of this exact cohort.
- Safe-approval automation remains blocked at its required production validation gate. The current
  read-only recomputation finds 69 qualifying reviewed Planning records rather than the reported 68;
  all 69 are NurserySignal, all were approved, none were rejected, and all have AI confidence exactly
  0.95. CareProspect/CHILDRENS_HOME has no qualifying deterministic cohort because its reviewed records
  do not carry the same deterministic recommendation fields. The 69 total is consistent with the earlier
  five-record labelled cohort plus the subsequently reviewed 64-record pending cohort, but current review
  rows do not snapshot all policy inputs at the instant of the human decision, so exact decision-time
  eligibility cannot be proven retrospectively. Per the safety gate, no safe-approval code, QA holdout,
  deployment or backlog mutation was performed. The exact next step is to reconcile whether the intended
  validated cohort is the latest 64, the reported 68, or all 69, and explicitly approve the authoritative
  cohort definition before implementing NurserySignal-only automation.
- NurserySignal `safe-approval-v1` is approved for rollout against the authoritative 69-record
  Planning validation cohort (69 approved, zero rejected; the earlier five plus the later 64).
  Historical review rows do not snapshot every policy input at decision time, so this is recorded as
  authoritative operator validation rather than a perfect policy-time reconstruction. The policy is
  deliberately limited to pending NurserySignal Planning records where deterministic relevance and a
  successful AI APPROVE agree at confidence >=0.95, the planning candidate matched, the opportunity
  action is create/support, and no refusal or false-positive condition applies. A stable UUID-derived
  one-in-ten QA holdout remains pending; each automatic approval/holdout is versioned and audited, AI
  rejection remains advisory, and customer publication is unchanged. CHILDRENS_HOME/CareProspect stays
  fully manual until it has its own qualifying labelled cohort. Commit `de892db` deployed successfully
  through the normal OIDC workflow. Production recomputation confirms the 69/69 NurserySignal cohort at
  the fixed 0.95 threshold and zero qualifying CareProspect records. The bounded NurserySignal backlog
  preview found zero currently eligible pending records (zero auto-approvals, zero QA holdouts; 608 remain
  manual), so no backlog mutation was needed or performed. Future eligible NurserySignal Planning signals
  now evaluate automatically after successful AI enrichment. Post-deployment Terraform reports zero
  drift; API/database and Lambda health pass, daily Planning/Recruitment schedules remain enabled, primary
  queues and all non-collector DLQs are empty, and the five previously classified obsolete collector-DLQ
  messages remain deliberately untouched. The next automation gate is prospective QA-holdout monitoring;
  CareProspect remains manual.
- Planning refusal policy v3 adds the exact normalized structured council decision `REFUSAL` to the
  existing conservative refusal set. It remains exact-field matching only: AI APPROVE cannot override
  the council refusal, while Withdrawn, Invalid, Returned, appeals, condition discharges and non-material
  amendments remain outside this automatic policy. Commit `a7b75b4` deployed successfully through the
  normal OIDC workflow. The bounded audited production cleanup inspected 1,444 pending Planning signals,
  found and auto-rejected 21 newly recognised explicit refusals, and completed with zero errors. A
  subsequent read-only triage evaluation found no explicit-refusal bucket remaining; 79 Withdrawn records
  remain pending by policy. Evidence and review history were preserved, customer publication and
  safe-approval thresholds were unchanged, API/database/Lambda health pass, primary ingestion/enrichment
  queues and their DLQs are empty, and the five previously classified collector-DLQ messages remain
  deliberately untouched. Post-deployment Terraform reports zero drift. This hardening gate is complete;
  the next automation gate remains prospective NurserySignal QA-holdout monitoring.
- CareProspect Planning review hardening introduces versioned, internal planning semantics for explicit
  new homes, proposed/existing lawfulness, condition variations/discharges, non-material amendments,
  other follow-ups, refusals, withdrawals and ambiguity. `care-planning-fastpath-v1` is deliberately
  limited to CHILDRENS_HOME Planning records with an explicit new-home subtype, a matched deterministic
  candidate, CREATE_OPPORTUNITY policy, and an exact positive/pending structured council state; it uses
  a stable one-in-ten QA holdout and does not depend on AI. `planning-withdrawal-v1` handles only exact
  structured withdrawal variants, preserving evidence, audit and independent resubmissions. Follow-up
  planning references are retained and may link to an already supported opportunity without relaxing
  global matching. Customer publication remains explicitly manual. Deployment, bounded pending-backlog
  reclassification, withdrawn cleanup and the preview-only existing fast-path report remain the rollout
  gates; no existing fast-path backlog approval is authorised by this implementation step.
- Production rollout (2026-09-29): 817 pending CareProspect Planning records were reclassified with zero
  errors. The distribution was 410 new-home change-of-use, 52 other explicit new homes, 139 proposed
  lawfulness, 14 existing-use lawfulness, 10 condition variations, 20 condition discharges, two
  non-material amendments, 42 other follow-ups, 59 withdrawals and 69 ambiguous records. Exact
  structured withdrawal cleanup audit-rejected all 59 withdrawals and left 758 pending. The existing
  backlog preview found 239 fast-path-eligible records: 212 would be auto-approved and 27 would remain
  as deterministic QA holdouts; 519 remain manual and 117 are support-only. The preview also identified
  95 draft opportunities supported only by negative/follow-up/existing-use evidence for later manual
  investigation; none were deleted or published. A production verification caught and fixed prose
  `SUBMITTED` being parsed as a prior application reference; references now require a numeric component.
  The six published customer opportunities remain unchanged. Next gate: explicit approval is required
  before executing any bounded existing-backlog fast-path approval; monitor QA holdouts before widening
  the policy, and keep proposed lawfulness/manual follow-up review conservative.
- The explicitly authorised `care-planning-fastpath-v1` existing-backlog rollout completed on 2026-09-29
  in three bounded batches (maximum 100): batch 1 approved 86 and marked 14 QA holdouts; batch 2
  approved 77 and marked nine new holdouts, idempotently skipping 14 already marked holdouts; batch 3
  approved the final 49 and marked four new holdouts, idempotently skipping 23 already marked holdouts.
  Total authoritative outcomes are 212 deterministic explicit-new-home approvals and 27 stable QA
  holdouts left pending. Final preview reports zero remaining auto-approval candidates, 27 holdouts and
  519 other manual records. All calls succeeded, normal audit markers/history were retained, and the six
  published customer opportunities were unchanged. Next step is manual review of the 27 QA holdouts and
  monitoring for any rejected holdout before considering a policy change; no broader CareProspect AI
  authority is approved.
- CareProspect Planning shadow review is deployed at commit `bc70060` as
  `care-planning-shadow-v2`. Exact structured council refusal/rejection and withdrawal outcomes are
  evaluated before subject relevance and are forced to REJECT with no commercial-change evidence,
  while still allowing relevant-follow-up classification. Proposed/existing lawfulness and planning
  follow-ups are distinguished explicitly; v1 assessments remain immutable alongside v2. Bounded
  production validation created versioned v2 evidence for 84 records without changing any review or
  publication state. All 25 selected v1 false-approve refusal/withdrawal regressions now reject, as do
  all four Certificate of Lawfulness refusal cases in the separate 59-record disagreement sample.
  Within that disagreement sample v2 changed five recommendations from REJECT to APPROVE, and only
  three records retain enough current data to reconstruct a human label under today's disagreement
  definition: v1 and v2 each agree on two, with zero v2 false approvals and one v2 false rejection.
  The operator-reported 60–70 record manual session cannot be reconstructed exactly because historical
  review rows do not snapshot every policy input/bucket at decision time; its result must not be
  overstated as a complete policy-time evaluation.
- The read-only future-policy preview currently identifies 304 of 456 pending CareProspect Planning
  records as potentially eligible at AI confidence >=0.95: 271 would fall into the approval bucket and
  33 into a stable ten-percent QA holdout, leaving 185 manual. The eligible subtype mix is 154 explicit
  change-of-use, 35 other explicit new homes, 67 proposed lawfulness, 38 other follow-ups and ten
  condition variations. Because the preview still admits follow-ups/variations and only three human
  labels are exactly reconstructable, CareProspect AI-assisted automation is not ready to enable. No
  auto-approval or AI-only rejection was introduced; deterministic refusal/withdrawal remains
  authoritative, customer publication remains at six, and a separate approval decision must follow a
  prospectively labelled v2 QA cohort and a narrower eligibility review. API/database/Lambda health
  passes, schedules are enabled, all active queues and non-collector DLQs are empty, the five previously
  classified obsolete collector-DLQ messages remain untouched, and the refresh-only Terraform plan
  shows only the expected moving RDS restore timestamp.
- CareProspect pending Planning AI currency is now explicit and refreshable through a bounded,
  admin-only workflow deployed at commit `5a8903e`. Review Triage and individual inbox rows distinguish
  current v2, stale v1, missing and failed assessments; the confirmed action processes at most ten stale
  or missing records serially, appends immutable v2 evidence, skips current records, and stops after
  repeated provider failures. It does not invoke review decisions, CareProspect automation or customer
  publication. Before refresh, 387 pending records comprised 48 current v2 and 339 stale v1, with no
  missing or failed assessments; triage showed 113 deterministic/AI disagreements and 274 manual-review
  records.
- The production stale refresh completed all 339 candidates in 35 bounded invocations: 337 v2
  assessments succeeded and two were preserved as `AI_FAILED` after isolated malformed model responses;
  there was no Bedrock throttling and a final idempotency invocation selected zero records. No pending
  stale or missing assessment remains. The current cohort is 385 successful v2 plus two failed v2
  assessments. Deterministic post-refresh cleanup found zero CareProspect refusals and zero withdrawals,
  so no review rows were mutated; the stale pending cohort contained zero v1-APPROVE to v2-REJECT
  structured-negative corrections because those authoritative negative applications had already left
  the pending inbox under the refusal/withdrawal policies.
- The clean current cohort contains 282 AI APPROVE, 98 REJECT and five NEEDS_HUMAN assessments. Triage
  now shows 159 deterministic/AI disagreements, five AI-uncertain and 223 manual-review records. The
  pending subtype distribution is 183 new-home change-of-use, 40 other explicit new homes, seven
  proposed lawfulness, 14 existing-use lawfulness, ten condition variations, 20 condition discharges,
  two non-material amendments, 42 other follow-ups and 69 ambiguous. A refreshed policy preview is still
  read-only: 205 records are potentially eligible, 182 would approve, 23 would be QA holdouts and 205
  remain manual. CareProspect AI automation remains disabled; the next decision must use this current v2
  cohort and explicitly account for follow-up/variation subtypes and the two failed assessments. The six
  published customer opportunities remain unchanged. CI/deployment, API/database/HTTPS and queue health
  pass; all active queues and non-collector DLQs are empty, while the five previously classified
  collector-DLQ messages remain untouched. The post-deployment refresh-only plan shows only expected RDS
  restore-time and deployed frontend-object metadata movement.
- CareProspect Planning outcome hardening replaces the duplicated exact refusal/withdrawal lists with
  one versioned `planning-outcome-v1` canonical layer over Plota's authoritative `decision`,
  `planning_status` and nested provider fields. The production vocabulary showed qualified/coded values
  including `FULL-REF`, `Planning Permission - Refused`, `Refusal - Full`, `Decided: REFUSE`,
  `Refused LUC`, `Certificate Refused (Lawful Dev. Cert.)`, `Withdrawn (P)`, `Withdrawn - Applicant`
  and `Withdrawn after Registration`; these were the reason the earlier exact cleanup incorrectly found
  zero negatives after the v2 AI refresh. Canonical outcomes are REFUSED, WITHDRAWN, APPROVED, PENDING,
  REFUSED_UNDER_APPEAL, APPEAL_ALLOWED, APPEAL_DISMISSED and UNKNOWN. Matching is anchored to structured
  fields rather than proposal-text substrings. Final refusal/appeal dismissal and application withdrawal
  override proposal relevance, while an active appealed refusal is retained as follow-up lifecycle
  evidence and cannot create a new opening.
- The read-only production dry run inspected 350 pending CareProspect Planning records and found 24 final
  refusals, five withdrawals and one refused application under active appeal; 27 of those records still
  carried stale CREATE_OPPORTUNITY facts. Manual inspection of all 30 records found no false-positive
  outcome classifications and no link to a published customer opportunity. The CareProspect-scoped,
  audited cleanup rejected all 24 refusals under `planning-refusal-v4` and all five withdrawals under
  `planning-withdrawal-v2`, with zero errors. It identified 24 associated draft opportunities, of which
  20 now have no non-rejected support; they remain preserved for manual lifecycle/duplicate review and
  were not deleted or published. The sole active appeal was reclassified by exact signal ID to
  FOLLOW_UP_OTHER / SUPPORT_EXISTING_ONLY. Post-cleanup reporting shows 321 pending, zero final
  refusal/withdrawal outcomes, one retained active appeal, and no negative/appeal record still carrying
  CREATE_OPPORTUNITY. Hillingdon provider record `j78cun1u` (`Withdrawn (P)`, 2026-06-12) had already
  been rejected before cleanup and remains immutable; the new canonical path prevents this vocabulary
  from creating future live opportunities.
- Commits `62cf255`, `42fbbf0` and `372c558` deployed successfully through CI/OIDC. Backend tests pass
  (357), frontend tests pass (78), Ruff/build/Terraform validation pass, and the post-deployment plan is
  clean when built with production frontend configuration. API/database and CareProspect HTTPS health
  pass; Planning and Recruitment schedules and the Planning manual-run mapping are enabled; active
  queues and non-collector DLQs are empty. The five previously retained collector-DLQ messages remain
  untouched. AI v1/v2 assessments and customer publication were unchanged. Exact next step: manually
  assess the 20 unsupported draft opportunities before any lifecycle cleanup; do not broaden outcome
  automation or CareProspect AI authority.
- The post-cleanup CareProspect AI freshness follow-up confirms that all 321 pending Planning signals
  have a latest `care-planning-shadow-v2` attempt: 319 succeeded, zero remain on v1, zero are missing,
  and two v2 attempts remain explicitly failed with `MALFORMED_RESPONSE` for manual review. No Bedrock
  refresh calls or review/publication mutations were required. The current successful-v2 distribution
  is 271 APPROVE, 43 REJECT and five NEEDS_HUMAN; confidence is 304 at 0.95, ten at 0.90, two at 0.80
  and three at 0.70. Triage contains 93 rule/AI disagreements, five AI-uncertain and 223 manual-review
  records. The inbox list and detail projections both select the newest immutable assessment, so a newer
  v2 result supersedes v1 for display while retaining v1 history. Historical refresh evidence records
  47 v1 APPROVE to v2 REJECT changes; stored validation reasons attribute 25 of those directly to
  structured outcomes (three refusals and 22 withdrawals), while the remaining 22 are not assigned a
  cause without stronger stored evidence. Canonical outcomes remain authoritative: no final refusal or
  withdrawal is pending, and the one active appeal remains support-only. The 20 unsupported draft
  opportunities remain untouched. Exact next step: manually review the pending cohort and the two failed
  v2 assessments; do not broaden CareProspect AI automation.
- `care-planning-ai-approval-v1` is deployed as the first narrowly authoritative CareProspect AI-assisted
  policy. It requires a latest successful `care-planning-shadow-v2` APPROVE at confidence >=0.95,
  canonical non-negative Planning outcome, no ambiguity/false-positive or prior policy/human state, and
  exactly `NEW_HOME_CHANGE_OF_USE` or `NEW_HOME_OTHER_EXPLICIT`. All lawfulness, condition, amendment,
  follow-up and ambiguous subtypes remain manual. The stable UUID modulo-ten QA holdout is persisted in
  signal facts and exposed through the `Care AI approval QA` inbox bucket; rejected holdouts raise an
  admin warning. AI rejection remains advisory and customer publication remains manual.
- The production preview reconciled all 321 pending records: 160 eligible (130 change-of-use and 30 other
  explicit homes), comprising 142 auto-approvals and 18 QA holdouts. The 161 exclusions were 141 subtype,
  15 AI recommendation, four confidence and one canonical outcome; no stale/failed assessment entered the
  cohort. Two serial batches (100 and 60) applied all 160 markers with 142 approvals, 18 pending holdouts,
  zero failures and zero idempotent skips. Batch reporting found 87 then 34 distinct existing opportunities
  (not deduplicated across batches) and 122 existing active relationships; approval created no opportunities or relationships and
  published inventory remained six before/after. Final preview reports zero remaining eligible records.
  CareProspect Planning pending is now 179: 18 policy QA holdouts, 93 rule/AI disagreements, five AI
  uncertain and 63 other manual records. QA monitoring is 18 pending, zero approved, zero rejected and no
  warning. The prior deterministic fast-path history remains immutable; future Care auto-approval now
  waits for complete current-v2 AI evidence. Keep the holdout at 10% and review it prospectively before
  considering any policy expansion; do not broaden into lawfulness/follow-up subtypes.
- Prospective QA for `care-planning-ai-approval-v1` is complete: all 18 deterministic holdouts were
  manually approved and none rejected (18/18 agreement; observed QA error 0/18). Historical v1 markers,
  assignments and audit history remain immutable. Future explicit-new-home records now use
  `care-planning-ai-approval-v1.1`, retaining the same subtypes, v2/0.95 eligibility, canonical-outcome
  precedence and publication gate while moving to a stable UUID modulo-20 holdout (5%). Monitoring
  aggregates both versions and retains the original 142 auto-approved / 18 held-out validation cohort.
- The post-QA CareProspect Planning queue contains 161 manual records: 12 change-of-use, seven other
  explicit homes, seven proposed-lawfulness, 14 existing-lawfulness, eight condition variations, 17
  condition discharges, two non-material amendments, 41 other follow-ups and 53 ambiguous. Triage is 93
  deterministic/AI disagreements, five AI-uncertain and 63 manual-review; 159 have successful current v2
  assessments and the two known failed existing-lawfulness assessments remain manual. Published
  opportunities remain six. A broad current unsupported-draft query reports 96, but no draft was mutated
  and this is not treated as a like-for-like replacement for the earlier 20-record cleanup-specific set.
- `care-planning-lawfulness-proposed-v1` remains preview-only. All seven current proposed-lawfulness
  records are v2 APPROVE at 0.95, explicitly describe a new children’s home, are canonically approved,
  CREATE_OPPORTUNITY, and have no false-positive, ambiguity, prior-reference, appeal or malformed-AI
  marker. A hypothetical stable 10% holdout yields six approvals and one holdout. This is the cleanest
  remaining subtype cohort, but seven unlabelled records are insufficient to enable it; obtain manual
  decisions first. Follow-up, ambiguous and condition subtypes have materially mixed recommendation or
  deterministic semantics and are not cleaner automation candidates. No new subtype automation was
  enabled. Exact next step: manually validate the seven proposed-lawfulness records, then make a separate
  policy decision using those labels.
