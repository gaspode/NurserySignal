# CareSignal procurement experiment

## Official interfaces

SignalHub uses only official, unauthenticated OCDS feeds:

- Find a Tender: `GET https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages`
  with `updatedFrom`, `updatedTo`, `limit` and the returned cursor URL. It publishes OCDS
  1.1 release packages. Release IDs identify notice versions within an `ocid` procurement
  process.
- Contracts Finder: `GET https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search`
  with `publishedFrom`, `publishedTo`, optional procurement stages, `limit` (1–100), and
  the returned cursor URL. Its record and release endpoints are keyed by OCID and notice ID.

Both feeds expose publication dates, process/release identifiers, buyer parties, tender
descriptions, stages, contract periods, values, award/supplier data where published, and
source documents. Find a Tender filtering is based on update time; Contracts Finder search
uses publication time (including the latest edit/current-stage publication semantics described
by its API documentation).

No authenticated or undocumented endpoint is used. Live probing returned an explicit limit of
12 requests with a 120-second retry window. The adapter therefore uses three stratified historical
windows per platform plus five official targeted evaluation records (11 calls total), a stable
user agent, short inter-page pacing, and at most
three bounded retries for HTTP 429 responses.

The data is published under the Open Government Licence v3.0. Relevant official documentation:

- https://www.gov.uk/government/publications/open-contracting
- https://www.contractsfinder.service.gov.uk/apidocumentation
- https://www.contractsfinder.service.gov.uk/apidocumentation/Notices/1/GET-Published-Notice-OCDS-Search

## Evaluation semantics

Procurement is a CareSignal shadow source. The collector stores official organisation-level
evidence in the existing private evidence store and classifies retained CareSignal candidates,
but its opportunity decision is always `REVIEW`. It cannot create, link, merge, advance, or
otherwise mutate live opportunities.

Find a Tender and Contracts Finder are treated as two platforms in one procurement family.
The release ID is the stable notice-version identity and the OCID links tender/award stages of
one procurement process. Content changes are retained through the existing immutable revision
mechanism. Contact-person fields are removed before persistence because organisation, buyer and
supplier identity is sufficient for this experiment.

Buyer identity is not operator identity. A council-led notice with no selected supplier remains
useful shadow evidence, but the buyer is never inferred to be the future home operator. Likewise,
one commissioning notice describing several homes exposes a current modelling limitation: it can
be evaluated as evidence, but is not forced into one or several production opportunities.

## Bounded operation

The initial manual bound is 548 days, at most 300 broad-sample releases per platform, and pages
of at most 100. Three time-stratified windows prevent the sample from representing only one end
of the period. Five versioned official-record examples supplement the broad noise sample; they
are evaluation coverage, not classifier rules, and are reported separately from prevalence.
There is no EventBridge schedule. Any promotion decision must be based on inspected strong
candidates, incremental discovery versus Planning/Recruitment, noise, lead time, and maintenance
cost—not raw record volume.
