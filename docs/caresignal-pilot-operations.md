# CareProspect paid-pilot operations

This checklist is for the manually operated first 5–10 supplier accounts. SignalHub remains the
administrative system; customers use the separate CareProspect role-routed portal.

Customer portal: `https://careprospect.co.uk`

Digest sender: `CareProspect <alerts@careprospect.co.uk>`. Replies are not advertised because no
monitored CareProspect mailbox is configured. SES domain identity, DKIM, custom MAIL FROM/SPF and
DMARC are managed by Terraform. Check SES production access before inviting an external recipient;
while the account is sandboxed, each test recipient must also be verified in SES.

## Publish an opportunity

1. In SignalHub, select **CareProspect** and open **Opportunities**.
2. Confirm the opportunity is current, non-duplicate and supported by approved Planning,
   Recruitment or Ofsted evidence. Procurement shadow evidence is not publication evidence.
3. Check the operator is genuinely known, geography is useful without reconstructing an
   Ofsted-redacted address, and every customer-visible source link is public.
4. Preview the generated customer title and summary. Use a concise override only when the generic
   wording is unclear; never invent an operator, site or opening date.
5. Select **Publish to CareProspect**. Use **Withdraw** if later evidence makes it unsuitable.

For bounded launch curation, an operator with production Lambda invoke permission may run the
IAM-only `customer_pilot_inventory` operation (maximum 100 current CHILDRENS_HOME opportunities) and
then `customer_pilot_publish` with an explicit list of at most 30 reviewed IDs. The operation is not
an HTTP route and each publication uses the normal audit event.

## Provision a customer

1. Open **SignalHub → Customers** and enter the organisation and named owner email.
2. Choose `STARTER` or `PRO`. A Starter account must have at least one allowed region or local
   authority; this restriction is enforced by customer API database queries.
3. Ask the user to complete the Cognito invitation and temporary-password flow.
4. Verify Opportunities, Saved, Alerts and Account from a clean browser session. Confirm logout,
   reload and a second login before treating the account as active.
5. For Starter, directly test list/search and an out-of-area opportunity ID. All must return only
   entitled records, with an out-of-area detail returning `404` and no metadata.

## Plans and account control

- `STARTER`: configured geography, opportunity feed, watchlist and weekly digest.
- `PRO`: nationwide feed, saved searches and weekly/daily/immediate preference options.
- Pricing is commercial configuration, not entitlement code; no checkout is enabled.
- Suspend/reactivate from SignalHub → Customers. Suspension must deny normal customer APIs while
  preserving watchlists, preferences and audit history.

## Email and digest test

1. Confirm the `careprospect.co.uk` SES domain identity and DKIM are verified in `eu-west-1`. If the SES account is in
   sandbox, the recipient must also be verified; request production access before inviting external
   pilot customers.
2. Confirm Terraform still configures `caresignal_email_from=alerts@careprospect.co.uk` and the
   customer-visible sender name `CareProspect`; these technical variable names are intentionally
   retained. Verify both the weekly EventBridge rule and digest SQS event-source mapping are enabled.
3. Set the authorised test user's preference to `WEEKLY` and invoke the bounded
   `customer_weekly_digest` backend operation once.
4. Confirm one digest run progresses `QUEUED → SENT`, the email is delivered, links open the
   CareProspect portal, and the same period cannot send twice.
5. Confirm the email contains only customer-safe opportunity projection data.

## Troubleshooting and health

- Backend/API: `GET /health` must report the database connected.
- Check the customer digest queue and `customer-digest-dlq`; failed sends record only a safe error
  class and retry up to the configured redrive limit.
- Check the sender Lambda log group for bounded error categories; never log recipients, payloads or
  credentials unnecessarily.
- Check SES account health, verified identity, sandbox/production status, quota and suppression
  list before retrying.
- Check ingestion/enrichment queues and all DLQs before and after pilot operations.
- Planning and Recruitment EventBridge schedules remain independent and must stay at `rate(1 day)`.

## Pilot evidence and privacy checks

- Customer APIs must never expose internal review notes, rule/AI confidence debugging, rejected
  evidence, benchmark data, procurement shadow records, organisation-resolution review data,
  private S3 keys or deliberately redacted Ofsted home addresses.
- Confirm another customer tenant cannot read this account's watchlist, saved searches or
  preferences.
- Check account-level usage events for login, opportunity views/saves, search/filter use and source
  link clicks. Use these events only to evaluate pilot engagement, not for invasive profiling.
