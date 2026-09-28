# SignalHub historical backtesting

Phase 11 evaluates production classification and opportunity policies without changing live
signals, opportunities, lifecycle, review state or matching decisions. The first benchmark is
CareProspect-first because Ofsted URNs and registration dates provide authoritative outcome truth.

## Separation of truth and replay input

`benchmark_cases` stores versioned outcome truth. `backtest_runs` and
`backtest_case_results` store immutable run parameters, metrics and explainable timelines.
Benchmark truth is used only after replay to score generated opportunities; it is never supplied
to classifiers or matchers.

For `care-ofsted-v1` and `care-ofsted-v2`, Ofsted registration is outcome truth only. Annual-register rows, later URN
enrichment and later inspection reports are never pre-registration inputs. A case without
case-linked historic planning/recruitment evidence is excluded unless benchmark provenance
explicitly establishes complete source coverage. This avoids treating an incomplete archive as a
real miss.

Runs are bounded to 50 cases, a canonical 365-day lookback (with explicit 450/540-day sensitivity)
and 1,000 source records. The replay is in-memory and writes only isolated backtest
run/results tables. A fingerprint over engine version, benchmark version, corpus version and
parameters makes an identical run reproducible and idempotent.

## Curated historical research corpus

`care-historical-research-v1` records the bounded research of all 21 `care-ofsted-v1` cases. The
bundled, code-reviewed manifest records official sources searched, search strategy, accepted and
rejected candidates, case-link confidence, earliest defensible public availability and explicit
exclusion reasons. Only `VERIFIED_CASE_LINK` and `STRONG_CASE_LINK` evidence from an official,
date-verifiable source is replay eligible. Possible links remain visible research notes.

`care-historical-research-v2` expands this to 50 authoritative 2024–2025 outcomes while retaining
the v1 benchmark and corpus unchanged. It carries the same case-link and eligibility rules, records
the source/search strategy for each added case, and admits two additional official planning links.
Provider-only or same-authority evidence is not enough to link a redacted Ofsted home. The bundled
manifest can be rebuilt reproducibly from an admin-safe outcome export with
`scripts/build_care_v2_manifest.py`; the script never calls or mutates production.

The corpus lives in evaluation-only tables. Importing it cannot create a live signal, opportunity,
organisation resolution, review item or lifecycle transition. Benchmark outcome fields are not
copied into replay records. The Ofsted URN, registration outcome and later provider enrichment
remain in benchmark truth only.

The current archive already preserves prospective replay state without another storage copy:

- planning source documents and revisions retain immutable observed payloads;
- recruitment source documents retain the published vacancy payload even after upstream expiry;
- Companies House responses are content-versioned organisation evidence with retrieval dates;
- Ofsted register and URN/report enrichment evidence is content-versioned with retrieval dates.

This means the current retention gap is historic coverage from before SignalHub existed, not
ongoing destructive overwrite. Mutable Companies House fields remain excluded unless their dated
snapshot was actually retrieved before the replay cut-off.

## Historic availability rules

- **Planning:** first known publication/validation/received/application date, in that order. A
  submission/application date is used only when no stronger publication field is preserved; this
  limitation is visible in the source timeline. Present-day status, decision and update fields are
  masked when no dated historic snapshot proves they existed at the cut-off.
- **Recruitment:** the provider's vacancy publication timestamp.
- **Ofsted:** retrieval/publication provenance, never registration date. In the initial CareSignal
  benchmark Ofsted is excluded from replay and used only to label the outcome.
- **Companies House:** cached identity is available only after its recorded retrieval timestamp and
  incorporation date. Company number and legal name may be used. Current status, office, SIC data
  and other mutable present-day profile fields are excluded unless a dated historical snapshot is
  later added.
- **Admin decisions:** exported as labels for evaluation, never supplied as historical matcher
  inputs. Decisions made after a cut-off cannot affect replay.

## Metrics and limitations

The framework reports recall, measurable precision, lead-time percentiles and observed range,
fixed lead-time distribution bands, operator resolution,
site/project resolution, review burden, duplicate/merge errors and planning/recruitment source
contribution. Unmatched generated opportunities are labelled `unlabelled`, not false. Precision
remains unavailable until reliable negative outcomes or exhaustive benchmark coverage exist.

Match confidence, event confidence and lifecycle confidence remain separate. Current policies
produce event and relationship confidence; there is no independent lifecycle-confidence or
commercial-priority model, so those values are reported as unavailable.

Ofsted deliberately redacts children-home site addresses. Provider identity therefore cannot be
used as site truth. Site accuracy is not calculated for cases without independently verified site
identity. This is expected to show whether a future Site model is justified, without presupposing
that architectural decision.

The first production `care-ofsted-v2` replay attempted 50 outcomes. Five were reconstructable in
the canonical 365-day window (all detected; median lead time 207 days, observed range 96–348), and
seven were reconstructable at both 450 and 540 days (all detected; median 290 days, range 96–389).
Planning was the first and only source for six wider-window cases; Recruitment was the first and
only source for one. The remaining 43 cases are excluded rather than counted as misses because
historic source coverage is incomplete. These results describe the bounded reconstructable sample,
not all children-home registrations, and precision is not available without reliable negatives.

## Admin API

- `GET /admin/backtesting?vertical=CHILDRENS_HOME`
- `POST /admin/backtesting/seed` — bounded, idempotent Ofsted outcome seeding
- `POST /admin/backtesting/research/import` — import an allow-listed fixed, reviewed corpus
- `POST /admin/backtesting/run` — bounded point-in-time replay
- `GET /admin/backtesting/runs/{id}`
- `GET /admin/backtesting/compare?left={id}&right={id}`
- `GET /admin/backtesting/labels?vertical=CHILDRENS_HOME`

All endpoints are administrator-only. Seeding, corpus import and running are audited. No endpoint
calls an external source or mutates production matching state.
