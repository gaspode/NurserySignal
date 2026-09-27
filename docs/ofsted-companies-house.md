# Ofsted and Companies House source notes

Checked against the official services on 27 September 2026.

## Ofsted children’s social care data

Ofsted publishes an annual OpenDocument register through GOV.UK’s
[children’s social care statistics collection](https://www.gov.uk/government/collections/childrens-social-care-statistics).
The current provider-level sheet enumerates active children’s homes and supplies a stable
URN, owning organisation, registration date/status, sector, places, broad region/local
authority, and latest inspection event fields. It does not provide a live API or a change
feed. Publication is annual; from January 2027 Ofsted plans to publish regulatory activity
separately.

Ofsted deliberately replaces children’s-home names, address lines, towns and postcodes
with `REDACTED`. SignalHub preserves that redaction and never attempts to reconstruct the
withheld address from the Ofsted source. With the current redacted register, provider and
local-authority agreement creates a Match Review suggestion rather than an automatic site
link. A confirmed current registration advances an existing opportunity to `REGISTRATION`,
not automatically to `OPEN`.

The Ofsted collector is manual-only and bounded. It downloads the official annual ODS,
extracts only published children’s-home rows, and retains each selected row as immutable
CareSignal evidence using the Ofsted URN as its stable identity.

### URN-specific public enrichment

The annual register is the base regulatory observation. For a bounded number of selected
records, SignalHub also follows the stable official provider page at
`https://reports.ofsted.gov.uk/provider/2/{URN}`. The page supplies provision type, local
authority, registration date and the public report timeline. When a latest report is
available, SignalHub reads only the tagged public metadata needed to identify the registered
provider and provider registered office. It does not retain the report body, use OCR, or
collect responsible-individual/manager details.

URN enrichment is immutable, content-versioned evidence separate from the annual-register
record. Retrieval timestamps do not affect content identity, so an unchanged rerun does not
create another evidence version. A page or report parsing failure is recorded safely and does
not prevent the annual-register signal from entering the normal pipeline.

`provider_registered_address`, locality, region and postcode describe the provider/company
office printed in the public report. They are never copied into a home/site address or an
opportunity location. Ofsted's deliberate suppression of children's-home names and addresses
remains intact.

## Companies House

SignalHub uses the official [Companies House Public Data API](https://developer.company-information.service.gov.uk/)
with HTTP Basic authentication (API key as username, blank password). The API supports
company search and company-profile lookup. The documented default rate limit is 600
requests in five minutes.

Companies House is organisation enrichment, not opportunity discovery. A company name,
incorporation, SIC code or company status never creates a CareSignal opportunity. Exact
company numbers and uniquely corroborated legal-name results may enrich a shared
organisation; ambiguous searches enter a separate Organisation Review queue. Company
identity strengthens later operator matching but never proves that two different sites
are one opportunity.

An exact or suffix-equivalent registered-provider name plus matching provider-office town or
postcode can strengthen a Companies House candidate. Region alone is insufficient. The
Organisation Review screen presents this Ofsted corroboration beside cached Companies House
candidates and labels the office as provider-level evidence, not a children's-home site.

No officer/director endpoint is called. Legal name, number, status, incorporation date,
company type, registered-office business fields and SIC codes are sufficient for the
current entity-resolution purpose, so collecting officer history or personal data would
not be proportionate.

The API key belongs in Secrets Manager secret
`nurserysignal-prod/companies-house` as:

```json
{"api_key":"YOUR_COMPANIES_HOUSE_API_KEY"}
```

The key must never be committed, logged or supplied to the browser.
