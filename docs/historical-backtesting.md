# SignalHub historical backtesting

Phase 11 evaluates production classification and opportunity policies without changing live
signals, opportunities, lifecycle, review state or matching decisions. The first benchmark is
CareSignal-first because Ofsted URNs and registration dates provide authoritative outcome truth.

## Separation of truth and replay input

`benchmark_cases` stores versioned outcome truth. `backtest_runs` and
`backtest_case_results` store immutable run parameters, metrics and explainable timelines.
Benchmark truth is used only after replay to score generated opportunities; it is never supplied
to classifiers or matchers.

For `care-ofsted-v1`, Ofsted registration is outcome truth only. Annual-register rows, later URN
enrichment and later inspection reports are never pre-registration inputs. A case without
case-linked historic planning/recruitment evidence is excluded unless benchmark provenance
explicitly establishes complete source coverage. This avoids treating an incomplete archive as a
real miss.

Runs are bounded to 50 cases, a 180–365 day lookback and 1,000 source records. The admin defaults
are 30 cases, 365 days and 500 records. The replay is in-memory and writes only isolated backtest
run/results tables. A fingerprint over engine version, benchmark version and parameters makes an
identical run reproducible and idempotent.

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

The framework reports recall, measurable precision, lead-time percentiles, operator resolution,
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

## Admin API

- `GET /admin/backtesting?vertical=CHILDRENS_HOME`
- `POST /admin/backtesting/seed` — bounded, idempotent Ofsted outcome seeding
- `POST /admin/backtesting/run` — bounded point-in-time replay
- `GET /admin/backtesting/runs/{id}`
- `GET /admin/backtesting/compare?left={id}&right={id}`
- `GET /admin/backtesting/labels?vertical=CHILDRENS_HOME`

All endpoints are administrator-only. Seeding and running are audited. No endpoint calls an
external source or mutates production matching state.
