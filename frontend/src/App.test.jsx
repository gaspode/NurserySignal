import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BacktestingPage, CustomersPage, Dashboard, LoginPage, MatchReviewPage, OpportunitiesPage, OpportunityDetail, OrganisationsPage, ProcurementEvaluationPage, ReviewInboxPage, ReviewedSignalsPage, SignalDetail, SourcesPage, UnmatchedSignalsPage, restoredVertical, verticalScopedPath } from "./App.jsx";

const pendingItem = {
  id: "signal-1",
  source_type: "planning",
  external_id: "planning-1",
  discovered_at: "2026-09-24T08:00:00Z",
  title: "Proposed new nursery at 12 High Street",
  location_hint: "Bristol BS1",
  organisation_hint: "Little Acorns",
  event_type: "opening",
  lifecycle_stage: "PLANNING",
  confidence: 0.86,
  review_status: "PENDING",
  extracted_facts: { classification: "planning-opening" },
  metadata: { council: "Bristol City Council" },
};

const approvedItem = { ...pendingItem, id: "signal-2", review_status: "APPROVED", title: "Approved nursery conversion" };

function detail(status = "PENDING") {
  return {
    ...pendingItem,
    source_url: "https://example.test/planning-1",
    raw_text: "A new nursery is proposed.",
    metadata: { capacity: 42, council: "Bristol City Council" },
    created_at: "2026-09-24T08:00:00Z",
    documents: [{ s3_key: "signals/planning/raw.json", sha256: "abc123", s3_bucket: "private" }],
    enrichment: {
      id: "enrichment-1",
      event_type: "opening",
      nursery_name: "Little Acorns",
      operator_name: "Little Acorns",
      address: "12 High Street",
      expected_opening_date: "2027-01-15",
      capacity: 42,
      lifecycle_stage: "PLANNING",
      confidence: 0.86,
      extracted_facts: { method: "fixture-v1" },
      evidence: { source_url: "https://example.test/planning-1" },
      review_status: status,
      reviewed_by: status === "PENDING" ? null : "reviewer-1",
      reviewed_at: status === "PENDING" ? null : "2026-09-24T09:00:00Z",
    },
  };
}

function listResult(items = [pendingItem], total = items.length) {
  return { items, total, limit: 10, offset: 0 };
}

describe("admin frontend", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("presents the login boundary and submits credentials", async () => {
    const onLogin = vi.fn().mockResolvedValue(undefined);
    render(<LoginPage onLogin={onLogin} />);
    await userEvent.type(screen.getByLabelText("Email"), "staff@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "not-a-real-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(onLogin).toHaveBeenCalledWith("staff@example.com", "not-a-real-password"));
  });

  it("shows customer invitations as queued rather than already delivered", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ customer_eligible: 6, draft_candidates: 0, email_delivery_configured: true })
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ status: "QUEUED", owner_email: "pilot@example.test", name: "Pilot supplier" })
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ customer_eligible: 6, draft_candidates: 0, email_delivery_configured: true })
      .mockResolvedValueOnce({ items: [] });
    render(<CustomersPage apiClient={apiClient} />);
    await screen.findByRole("heading", { name: "CareProspect customers" });
    await userEvent.type(screen.getByLabelText("Organisation"), "Pilot supplier");
    await userEvent.type(screen.getByLabelText("Owner email"), "pilot@example.test");
    await userEvent.type(screen.getByLabelText("Allowed local authorities"), "Liverpool");
    await userEvent.click(screen.getByRole("button", { name: "Create account and invite" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Invitation queued for pilot@example.test to join Pilot supplier"
    );
  });

  it("uses customer language on the CareProspect sign-in boundary", () => {
    render(<LoginPage onLogin={vi.fn()} customerBrand />);
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("CareProspect")).toBeInTheDocument();
    expect(screen.getByText("Sign in to your CareProspect account.")).toBeInTheDocument();
    expect(screen.queryByText(/staff accounts/i)).not.toBeInTheDocument();
  });

  it("shows only pending records in the review inbox", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult());
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Review Inbox" })).toBeInTheDocument();
    expect(screen.getByText(pendingItem.title)).toBeInTheDocument();
    expect(screen.queryByLabelText("Review status")).not.toBeInTheDocument();
    expect(apiClient).toHaveBeenCalledWith(expect.stringContaining("review_status=PENDING"));
  });

  it("offers immediate row actions without a confirmation dialog", async () => {
    const nativeConfirm = vi.spyOn(window, "confirm");
    const apiClient = vi.fn()
      .mockResolvedValueOnce(listResult())
      .mockResolvedValueOnce({ signal_id: "signal-1", review_status: "APPROVED" })
      .mockResolvedValueOnce(listResult([], 0));
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByText(pendingItem.title);
    await userEvent.click(screen.getByRole("button", { name: `Actions for ${pendingItem.title}` }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Approve" }));
    expect(nativeConfirm).not.toHaveBeenCalled();
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith("/admin/signals/signal-1/approve", { method: "POST" }));
    expect(await screen.findByText("Inbox clear")).toBeInTheDocument();
  });

  it("portals a bottom-row action menu outside the scrolling table and flips it above", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult());
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByText(pendingItem.title);
    const trigger = screen.getByRole("button", { name: `Actions for ${pendingItem.title}` });
    vi.spyOn(trigger, "getBoundingClientRect").mockReturnValue({
      top: 750, bottom: 780, left: 940, right: 980, width: 40, height: 30, x: 940, y: 750, toJSON: () => {},
    });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1000 });

    await userEvent.click(trigger);
    const menu = await screen.findByRole("menu");
    await waitFor(() => expect(menu).toHaveAttribute("data-placement", "top"));
    expect(menu.parentElement).toBe(document.body);
    expect(menu.closest(".table-wrap")).toBeNull();
    expect(menu).toHaveStyle({ position: "fixed" });
    expect(Number.parseFloat(menu.style.top)).toBeLessThan(750);
    expect(Number.parseFloat(menu.style.left)).toBeGreaterThanOrEqual(8);
  });

  it("dismisses the row action menu on outside click and Escape", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult());
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByText(pendingItem.title);
    const trigger = screen.getByRole("button", { name: `Actions for ${pendingItem.title}` });

    await userEvent.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("heading", { name: "Review Inbox" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    await userEvent.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("supports keyboard navigation within the portalled row action menu", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult());
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByText(pendingItem.title);
    const trigger = screen.getByRole("button", { name: `Actions for ${pendingItem.title}` });
    trigger.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Approve" })).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Reject" })).toHaveFocus();
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("menuitem", { name: "View" })).toHaveFocus();
  });

  it("supports bounded bulk decisions with confirmation", async () => {
    const second = { ...pendingItem, id: "signal-2", title: "Second pending signal" };
    const apiClient = vi.fn()
      .mockResolvedValueOnce(listResult([pendingItem, second], 2))
      .mockResolvedValueOnce({ updated: 2 })
      .mockResolvedValueOnce(listResult([], 0));
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByText(second.title);
    await userEvent.click(screen.getByRole("checkbox", { name: `Select ${pendingItem.title}` }));
    await userEvent.click(screen.getByRole("checkbox", { name: `Select ${second.title}` }));
    await userEvent.click(screen.getByRole("button", { name: "Reject selected" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("Reject selected signals?");
    await userEvent.click(screen.getByRole("dialog").querySelector(".button.reject"));
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith("/admin/signals/bulk-review", {
      method: "POST",
      body: JSON.stringify({ action: "reject", signal_ids: ["signal-1", "signal-2"] }),
    }));
  });

  it("supports reviewed history search, filtering and pagination", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult([approvedItem], 11));
    render(<ReviewedSignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Reviewed Signals" })).toBeInTheDocument();
    expect(screen.getByText(approvedItem.title)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search reviewed signals"), "Bristol");
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("q=Bristol")));
    await userEvent.selectOptions(screen.getByLabelText("Review status"), "REJECTED");
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("review_status=REJECTED")));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("offset=10"));
  });

  it("shows the advisory AI shadow figure in reviewed history rows", async () => {
    const item = { ...approvedItem, ai_recommendation: "APPROVE", ai_confidence: 0.91, ai_status: "SUCCEEDED" };
    const apiClient = vi.fn().mockResolvedValue(listResult([item]));
    render(<ReviewedSignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect((await screen.findAllByText("AI shadow")).length).toBeGreaterThan(0);
    expect(screen.getByText("APPROVE · 91%")).toBeInTheDocument();
  });

  it("renders an empty inbox state", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult([], 0));
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByText("Inbox clear")).toBeInTheDocument();
    expect(screen.getByText("There are no pending signals waiting for review.")).toBeInTheDocument();
  });

  it("reviews a signal directly from detail and opens the next pending signal", async () => {
    const nativeConfirm = vi.spyOn(window, "confirm");
    const onReviewed = vi.fn();
    const apiClient = vi.fn()
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce({ signal_id: "signal-1", review_status: "APPROVED" })
      .mockResolvedValueOnce(listResult([{ ...pendingItem, id: "signal-2" }]));
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} queueMode onReviewed={onReviewed} onBack={vi.fn()} />);
    expect(await screen.findByText("A new nursery is proposed.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(onReviewed).toHaveBeenCalledWith({ message: "Signal approved successfully.", nextId: "signal-2" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(nativeConfirm).not.toHaveBeenCalled();
  });

  it("supports correcting a reviewed decision with accurate feedback", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce(detail("APPROVED"))
      .mockResolvedValueOnce({ signal_id: "signal-1", review_status: "REJECTED" })
      .mockResolvedValueOnce(detail("REJECTED"));
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByText("Decision recorded")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Change to Rejected" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Signal rejected successfully.");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("rejeced")).not.toBeInTheDocument();
    expect(apiClient).toHaveBeenCalledWith("/admin/signals/signal-1/reject", { method: "POST" });
  });

  it("shows deterministic and advisory AI assessments separately", async () => {
    const value = detail();
    value.ai_reviews = [{
      status: "SUCCEEDED",
      recommendation: "APPROVE",
      confidence: 0.91,
      reason: "Explicit new childcare provision.",
      model_id: "amazon.nova-lite-v1:0",
      evaluated_at: "2026-09-25T09:00:00Z",
    }];
    const apiClient = vi.fn().mockResolvedValue(value);
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByText("Deterministic assessment")).toBeInTheDocument();
    expect(screen.getByText("Rule confidence")).toBeInTheDocument();
    expect(screen.getByText("AI shadow assessment")).toBeInTheDocument();
    expect(screen.getByText("AI confidence")).toBeInTheDocument();
    expect(screen.getByText("Explicit new childcare provision.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("renders planning AI relevance without recruitment fields", async () => {
    const value = detail();
    value.ai_reviews = [{
      status: "SUCCEEDED",
      recommendation: "APPROVE",
      confidence: 0.9,
      planning_relevance: "RELEVANT_CHANGE",
      commercial_change_evidence: "STRONG",
      reason: "New nursery buildings address excess demand.",
      model_id: "eu.amazon.nova-lite-v1:0",
    }];
    const apiClient = vi.fn().mockResolvedValue(value);
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByText("Planning relevance")).toBeInTheDocument();
    expect(screen.getByText("RELEVANT CHANGE")).toBeInTheDocument();
    expect(screen.queryByText("Recruitment relevance")).not.toBeInTheDocument();
  });

  it("shows collector status and starts a bounded manual source run", async () => {
    const planning = { key: "planning", display_name: "Planning applications", provider: "Plota", schedule_state: "ENABLED", schedule_expression: "rate(1 day)", last_status: "SUCCESS", last_summary: { records_fetched: 4, candidates_matched: 1, signals_queued: 1, excluded: 3, duplicates: 0, errors: 0 }, last_run: { id: "run-1", status: "SUCCESS", invocation_source: "scheduled", started_at: "2026-09-26T18:00:00Z" }, recent_runs: [] };
    const recruitment = { key: "recruitment", display_name: "Recruitment vacancies", provider: "GOV.UK Apprenticeships", schedule_state: "ENABLED", schedule_expression: "rate(1 day)", recent_runs: [] };
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [planning, recruitment] })
      .mockResolvedValueOnce({ run_id: "run-2", status: "RUNNING" })
      .mockResolvedValueOnce({ items: [{ ...planning, last_run: { id: "run-2", status: "SUCCESS", invocation_source: "manual", started_at: "2026-09-26T19:00:00Z" }, last_status: "SUCCESS" }, recruitment] });
    render(<SourcesPage apiClient={apiClient} />);
    expect(await screen.findByRole("heading", { name: "Sources" })).toBeInTheDocument();
    expect(screen.getByText("Planning applications")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Run now" })[0]);
    expect(apiClient).toHaveBeenCalledWith("/admin/sources/planning/run", {
      method: "POST",
      body: JSON.stringify({ vertical: "NURSERY" }),
    });
    expect(await screen.findByText("Run in progress…")).toBeInTheDocument();
  });

  it("shows source run failures in an alert dialog with an OK button", async () => {
    const planning = { key: "planning", display_name: "Planning applications", provider: "Plota", schedule_state: "ENABLED", schedule_expression: "rate(1 day)", recent_runs: [] };
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [planning] })
      .mockRejectedValueOnce(new Error("Internal Server Error"));
    render(<SourcesPage apiClient={apiClient} />);
    await screen.findByText("Planning applications");
    await userEvent.click(screen.getByRole("button", { name: "Run now" }));
    const alert = await screen.findByRole("alertdialog");
    expect(alert).toHaveTextContent("Source run could not be started");
    expect(alert).toHaveTextContent("Internal Server Error");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "OK" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("runs a bounded CareProspect backfill from stored evidence", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ evaluated: 25, planning_evaluated: 20, recruitment_evaluated: 5, relevant: 3, accepted: 3 });
    render(<SourcesPage apiClient={apiClient} />);
    await screen.findByRole("heading", { name: "Stored-evidence backfill" });
    await userEvent.click(screen.getByRole("button", { name: "Run CareProspect backfill" }));
    expect(apiClient).toHaveBeenCalledWith(
      "/admin/verticals/CHILDRENS_HOME/backfill",
      { method: "POST", body: JSON.stringify({ days: 60, limit: 25 }) },
    );
    expect(await screen.findByText(/20 planning, 5 recruitment/)).toBeInTheDocument();
    expect(screen.getByText(/3 relevant and 3 newly accepted/)).toBeInTheDocument();
  });

  it("shows Companies House enrichment and keeps ambiguous matches reviewable", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [{ id: "operator-1", name: "Acme Care", legal_name: "ACME CARE LIMITED", vertical: "CHILDRENS_HOME", companies_house_number: "12345678", company_status: "active", signal_count: 2, opportunity_count: 1 }] })
      .mockResolvedValueOnce({ items: [{ id: "review-1", operator_id: "operator-2", organisation_name: "Other Care", reason: "multiple candidates", source_context: { observed_name: "Other Care", verticals: ["CHILDRENS_HOME"], source_types: ["planning", "ofsted"], website: "https://other.example", aliases: [{ alias: "Other Care Ltd" }], signals: [{ id: "signal-1", title: "Change of use to children's home", source_type: "planning", vertical: "CHILDRENS_HOME", organisation_name: "Other Care", town: "Coventry", postcode: "CV1 2AB", source_url: "https://planning.example/1" }], opportunities: [{ id: "opportunity-1", name: "New children's home — Coventry", vertical: "CHILDRENS_HOME", town: "Coventry" }], ofsted_evidence: [{ urn: "2766766", registered_provider_name: "Other Care Limited", provider_registered_address: "1 Provider Office, Blackpool, FY4 2FF", provider_registered_locality: "Blackpool", provider_registered_region: "Lancashire", provider_registered_postcode: "FY4 2FF", latest_report_url: "https://files.ofsted.gov.uk/v1/file/50311430", provider_page_url: "https://reports.ofsted.gov.uk/provider/2/2766766" }] }, candidates: [{ company_name: "OTHER CARE LIMITED", company_number: "87654321", company_status: "active", date_of_creation: "2020-03-04", type: "ltd", registered_office_address: { locality: "Coventry", postal_code: "CV1 2AB" }, sic_descriptions: [{ code: "87900", description: "Other residential care activities not elsewhere classified" }], companies_house_url: "https://find-and-update.company-information.service.gov.uk/company/87654321", match_outcome: "STRONG", best_supported_match: true, match_reasons: ["Exact normalized legal-name match", "Same town/locality as source evidence"], match_cautions: [] }] }] })
      .mockResolvedValueOnce({ id: "review-1", status: "CONFIRMED" })
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [] });
    render(<OrganisationsPage apiClient={apiClient} />);
    expect(await screen.findByText("ACME CARE LIMITED")).toBeInTheDocument();
    expect(screen.getByText("12345678")).toBeInTheDocument();
    expect(screen.getByText("Organisation resolution review")).toBeInTheDocument();
    expect(screen.getByText("Change of use to children's home")).toBeInTheDocument();
    expect(screen.getByText("Coventry, CV1 2AB")).toBeInTheDocument();
    expect(screen.getByText("Other residential care activities not elsewhere classified", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Exact normalized legal-name match")).toBeInTheDocument();
    expect(screen.getByText("Best supported match")).toBeInTheDocument();
    expect(screen.getByText("Ofsted provider evidence")).toBeInTheDocument();
    expect(screen.getByText("URN 2766766 · Blackpool · Lancashire · FY4 2FF")).toBeInTheDocument();
    expect(screen.getByText(/Provider registered office: 1 Provider Office/)).toBeInTheDocument();
    expect(screen.getByText(/not the children’s-home location/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View latest Ofsted report" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "View Companies House" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "View signal" })).toHaveAttribute("href", "#/history/signal-1");
    await userEvent.click(screen.getByRole("button", { name: "Use this company" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Original names remain as aliases");
    await userEvent.click(within(dialog).getByRole("button", { name: "Use this company" }));
    expect(apiClient).toHaveBeenCalledWith(
      "/admin/organisation-match-review/review-1/confirm",
      { method: "POST", body: JSON.stringify({ company_number: "87654321" }) },
    );
  });

  it("looks up a manually entered company number before requiring confirmation", async () => {
    const review = { id: "review-1", operator_id: "operator-1", organisation_name: "Example Care", candidates: [] };
    const manualCandidate = { company_name: "EXAMPLE CARE LIMITED", company_number: "12345678", company_status: "dissolved", date_of_creation: "2020-03-04", registered_office_address: { locality: "Blackpool", postal_code: "FY4 2FF" }, match_outcome: "STRONG", match_reasons: ["Exact Ofsted provider-office postcode match"], selection_source: "MANUAL_LOOKUP" };
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [{ id: "operator-1", name: "Example Care", vertical: "CHILDRENS_HOME", signal_count: 1, opportunity_count: 0 }] })
      .mockResolvedValueOnce({ items: [review] })
      .mockResolvedValueOnce(manualCandidate)
      .mockResolvedValueOnce({ id: "review-1", status: "CONFIRMED" })
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [] });
    render(<OrganisationsPage apiClient={apiClient} />);
    await screen.findByText("Organisation resolution review");
    await userEvent.type(screen.getByLabelText("Companies House number"), "1234 5678");
    await userEvent.click(screen.getByRole("button", { name: "Look up" }));
    expect(apiClient).toHaveBeenCalledWith(
      "/admin/organisation-match-review/review-1/lookup",
      { method: "POST", body: JSON.stringify({ company_number: "1234 5678" }) },
    );
    expect(await screen.findByText("EXAMPLE CARE LIMITED")).toBeInTheDocument();
    expect(screen.getByText("Dissolved")).toBeInTheDocument();
    expect(screen.getByText("Exact Ofsted provider-office postcode match")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Use this company" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("Original names remain as aliases");
  });

  it("queues missing Ofsted provider evidence without blocking the review", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [{ id: "operator-1", name: "Example Care", vertical: "CHILDRENS_HOME", signal_count: 1, opportunity_count: 0 }] })
      .mockResolvedValueOnce({ items: [{ id: "review-1", operator_id: "operator-1", organisation_name: "Example Care", source_context: { missing_ofsted_urns: ["2813108"], signals: [], opportunities: [] }, candidates: [] }] })
      .mockResolvedValueOnce({ status: "QUEUED", queued: true, urn: "2813108" });
    render(<OrganisationsPage apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "Enrich Ofsted provider evidence" }));
    expect(apiClient).toHaveBeenCalledWith(
      "/admin/organisation-match-review/review-1/enrich-ofsted",
      { method: "POST", body: JSON.stringify({}) },
    );
    expect(await screen.findByRole("button", { name: "Enrichment queued" })).toBeDisabled();
  });

  it("rejects candidate companies without rejecting the observed organisation", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [{ id: "operator-1", name: "Other Care", vertical: "CHILDRENS_HOME", signal_count: 1, opportunity_count: 0 }] })
      .mockResolvedValueOnce({ items: [{ id: "review-1", operator_id: "operator-1", organisation_name: "Other Care", candidates: [{ company_name: "OTHER CARE LIMITED", company_number: "87654321" }] }] })
      .mockResolvedValueOnce({ id: "review-1", status: "REJECTED" })
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [] });
    render(<OrganisationsPage apiClient={apiClient} />);
    await screen.findByText("Organisation resolution review");
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "None of these companies" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("organisation and its CareProspect evidence will not be rejected");
    await userEvent.click(within(dialog).getByRole("button", { name: "None of these companies" }));
    expect(apiClient).toHaveBeenCalledWith(
      "/admin/organisation-match-review/review-1/reject",
      { method: "POST", body: JSON.stringify({}) },
    );
  });

  it("shows recruitment role, setting, routine relevance and change evidence", async () => {
    const value = detail();
    value.source_type = "recruitment";
    value.enrichment.extracted_facts = {
      recruitment_role_category: "teaching_assistant",
      recruitment_setting_category: "school_with_nursery",
      recruitment_relevance: "RELEVANT_ROUTINE",
      commercial_change_evidence: "NONE",
    };
    const apiClient = vi.fn().mockResolvedValue(value);
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByText("Teaching Assistant")).toBeInTheDocument();
    expect(screen.getByText("School With Nursery")).toBeInTheDocument();
    expect(screen.getByText("RELEVANT ROUTINE")).toBeInTheDocument();
    expect(screen.getByText("NONE")).toBeInTheDocument();
  });

  it("runs an advisory AI assessment without changing human review state", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce({
        status: "SUCCEEDED",
        recommendation: "APPROVE",
        confidence: 0.9,
        reason: "New childcare provision.",
        model_id: "amazon.nova-lite-v1:0",
        prompt_version: "shadow-v1",
        human_review_status: "PENDING",
        idempotent: false,
      })
      .mockResolvedValueOnce(detail());
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} onBack={vi.fn()} />);
    await screen.findByText("No AI shadow assessment available.");
    await userEvent.click(screen.getByRole("button", { name: "Run AI assessment" }));
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith("/admin/signals/signal-1/ai-review", { method: "POST" }));
    expect(await screen.findByRole("status")).toHaveTextContent("AI shadow assessment completed.");
    expect(apiClient).toHaveBeenCalledTimes(3);
  });

  it("allows shadow assessment from reviewed history without changing the human decision", async () => {
    const reviewed = detail("APPROVED");
    const apiClient = vi.fn()
      .mockResolvedValueOnce(reviewed)
      .mockResolvedValueOnce({
        status: "SUCCEEDED",
        recommendation: "NEEDS_HUMAN",
        confidence: 0.64,
        reason: "The stored planning evidence is commercially ambiguous.",
        model_id: "amazon.nova-lite-v1:0",
        prompt_version: "shadow-v1",
        human_review_status: "APPROVED",
        idempotent: false,
      })
      .mockResolvedValueOnce({ ...reviewed, ai_reviews: [{ status: "SUCCEEDED", recommendation: "NEEDS_HUMAN", confidence: 0.64, reason: "The stored planning evidence is commercially ambiguous.", model_id: "amazon.nova-lite-v1:0" }] });
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByText("Decision recorded")).toBeInTheDocument();
    expect(screen.getAllByText("APPROVED").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Run AI shadow assessment" }));
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith("/admin/signals/signal-1/ai-review", { method: "POST" }));
    expect(await screen.findByRole("status")).toHaveTextContent("AI shadow assessment completed.");
    expect(screen.getAllByText("APPROVED").length).toBeGreaterThan(0);
    expect(screen.getByText("The stored planning evidence is commercially ambiguous.")).toBeInTheDocument();
  });

  it("marks Ofsted regulatory evidence as outside AI shadow scope", async () => {
    const ofsted = detail("APPROVED");
    ofsted.source_type = "ofsted";
    ofsted.vertical = "CHILDRENS_HOME";
    ofsted.title = "Ofsted children's home registration";
    ofsted.ai_reviews = [];
    ofsted.ofsted_enrichment = {
      urn: "2766766",
      status: "SUCCEEDED",
      registered_provider_name: "Oaktree Childcare Limited",
      provision_type: "Children's Home",
      registration_date: "2024-10-04",
      local_authority: "Lancashire",
      provider_registered_address: "Ground Floor, Seneca House, Blackpool, Lancashire FY4 2FF",
      provider_registered_locality: "Blackpool",
      provider_registered_region: "Lancashire",
      provider_registered_postcode: "FY4 2FF",
      latest_report_date: "2026-07-28",
      latest_report_publication_date: "2026-09-08",
      latest_report_url: "https://files.ofsted.gov.uk/v1/file/50311430",
      provider_page_url: "https://reports.ofsted.gov.uk/provider/2/2766766",
    };
    const apiClient = vi.fn().mockResolvedValue(ofsted);
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByText(/Not applicable to Ofsted regulatory evidence/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run AI/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change to Rejected" })).toBeInTheDocument();
    expect(screen.getByText("Ofsted regulatory evidence")).toBeInTheDocument();
    expect(screen.getByText("Oaktree Childcare Limited")).toBeInTheDocument();
    expect(screen.getByText("Provider registered office (not home/site)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View latest Ofsted report" })).toHaveAttribute("target", "_blank");
    expect(apiClient).toHaveBeenCalledTimes(1);
  });

  it("labels Ofsted list rows as not applicable rather than missing AI", async () => {
    const item = { ...approvedItem, source_type: "ofsted", vertical: "CHILDRENS_HOME" };
    const apiClient = vi.fn().mockResolvedValue(listResult([item]));
    render(<ReviewedSignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByText("Not applicable")).toBeInTheDocument();
  });

  it("links dashboard metrics to the inbox and reviewed history", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ total: 2 })
      .mockResolvedValueOnce({ total: 3 })
      .mockResolvedValueOnce({ total: 4 })
      .mockResolvedValueOnce(listResult([pendingItem]))
      .mockResolvedValueOnce({ total: 5 })
      .mockResolvedValueOnce({ total: 6 })
      .mockResolvedValueOnce({ total: 7 });
    const onNavigate = vi.fn();
    render(<Dashboard apiClient={apiClient} onNavigate={onNavigate} />);
    await screen.findByText("Pending review");
    await userEvent.click(screen.getByText("Pending review"));
    await userEvent.click(screen.getByText("Approved"));
    expect(onNavigate).toHaveBeenCalledWith("/inbox");
    expect(onNavigate).toHaveBeenCalledWith("/history?review_status=APPROVED");
  });

  it("refreshes all overview data in place and disables the control while busy", async () => {
    let resolveFirstRefresh;
    let callCount = 0;
    const apiClient = vi.fn(() => {
      callCount += 1;
      if (callCount === 8) return new Promise((resolve) => { resolveFirstRefresh = resolve; });
      if (callCount % 7 === 4) return Promise.resolve(listResult([pendingItem]));
      return Promise.resolve({ total: callCount % 7 || 7 });
    });
    const onNavigate = vi.fn();
    render(<Dashboard apiClient={apiClient} onNavigate={onNavigate} />);
    await screen.findByText("Pending review");
    await userEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    expect(screen.getByRole("button", { name: "Refreshing data" })).toBeDisabled();
    expect(onNavigate).not.toHaveBeenCalled();
    resolveFirstRefresh({ total: 2 });
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).not.toBeDisabled());
    expect(apiClient).toHaveBeenCalledTimes(14);
  });

  it("reloads the review inbox when the vertical-scoped API client changes", async () => {
    const nursery = { ...pendingItem, vertical: "NURSERY" };
    const care = { ...pendingItem, id: "care-1", vertical: "CHILDRENS_HOME", title: "New children's home" };
    const nurseryClient = vi.fn().mockResolvedValue(listResult([nursery]));
    const careClient = vi.fn().mockResolvedValue(listResult([care]));
    const view = render(<ReviewInboxPage apiClient={nurseryClient} onNavigate={vi.fn()} />);
    expect(await screen.findByText(nursery.title)).toBeInTheDocument();
    view.rerender(<ReviewInboxPage apiClient={careClient} onNavigate={vi.fn()} />);
    expect(await screen.findByText(care.title)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(nursery.title)).not.toBeInTheDocument());
  });

  it("shows vertical labels for all-vertical signal queues", async () => {
    const nursery = { ...pendingItem, vertical: "NURSERY" };
    const care = { ...pendingItem, id: "care-1", vertical: "CHILDRENS_HOME", title: "New children's home" };
    render(<ReviewInboxPage apiClient={vi.fn().mockResolvedValue(listResult([nursery, care]))} onNavigate={vi.fn()} showVertical />);
    expect(await screen.findByText("NurserySignal")).toBeInTheDocument();
    expect(screen.getByText("CareProspect")).toBeInTheDocument();
  });

  it("preserves valid vertical selection and rejects disabled stored contexts", () => {
    expect(restoredVertical({ getItem: () => "CHILDRENS_HOME" })).toBe("CHILDRENS_HOME");
    expect(restoredVertical({ getItem: () => "DENTAL" })).toBe("NURSERY");
    expect(verticalScopedPath("/admin/signals?review_status=PENDING", "NURSERY")).toContain("vertical=NURSERY");
    expect(verticalScopedPath("/admin/opportunities?limit=10", "ALL")).toContain("vertical=ALL");
    expect(verticalScopedPath("/admin/opportunities/recalculate", "CHILDRENS_HOME")).toContain("vertical=CHILDRENS_HOME");
  });

  it("refreshes the pending inbox without changing its pagination state or navigation", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult());
    const onNavigate = vi.fn();
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={onNavigate} />);
    await screen.findByText(pendingItem.title);
    await userEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    await waitFor(() => expect(apiClient).toHaveBeenCalledTimes(2));
    expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("review_status=PENDING"));
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("preserves reviewed history search state when refreshing", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult([approvedItem], 1));
    render(<ReviewedSignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByText(approvedItem.title);
    await userEvent.type(screen.getByLabelText("Search reviewed signals"), "Bristol");
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("q=Bristol")));
    await userEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("q=Bristol")));
  });

  it("shows API failures without losing the inbox shell", async () => {
    const apiClient = vi.fn().mockRejectedValue(new Error("API unavailable"));
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("API unavailable");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows opportunities and links their evidence", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [{ id: "opp-1", name: "Little Acorns Nursery", lifecycle_stage: "PLANNING", change_type: "EXPANSION", confidence: 0.93, signal_count: 2, creation_reason: "Planning evidence indicates expansion", stage_reason: "same postcode and compatible operator/nursery name", event_type: "expansion" }], total: 1 })
      .mockResolvedValueOnce({ id: "opp-1", name: "Little Acorns Nursery", lifecycle_stage: "STAFFING", confidence: 0.93, creation_reason: "Planning evidence indicates expansion", signals: [{ id: "signal-1", source_type: "planning", title: "Change of use to day nursery", discovered_at: "2026-09-20T00:00:00Z", rule_confidence: 0.86, relationship_created_by: "SYSTEM", match_reason: "same postcode and compatible operator/nursery name" }] });
    const onNavigate = vi.fn();
    render(<OpportunitiesPage apiClient={apiClient} onNavigate={onNavigate} />);
    expect(await screen.findByText("Little Acorns Nursery")).toBeInTheDocument();
    expect(screen.getByText("Expansion")).toBeInTheDocument();
    expect(screen.getByText("Planning evidence indicates expansion")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Little Acorns Nursery"));
    expect(onNavigate).toHaveBeenCalledWith("/opportunities/opp-1");
  });

  it("keeps recalculation errors inside the confirmation dialog", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [], total: 0 })
      .mockRejectedValueOnce(new Error("admin_request_failed"));
    render(<OpportunitiesPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("heading", { name: "Opportunities" });
    await userEvent.click(screen.getByRole("button", { name: "Recalculate opportunities" }));
    await userEvent.click(screen.getByRole("dialog").querySelector(".button.approve"));
    expect(await screen.findByRole("dialog")).toHaveTextContent("The recalculation could not be completed.");
    expect(screen.queryByText("admin_request_failed")).not.toBeInTheDocument();
  });

  it("separates opportunity basis from signal relationship reasons", async () => {
    const apiClient = vi.fn().mockResolvedValue({
      id: "opp-1",
      name: "Little Acorns Nursery",
      lifecycle_stage: "PLANNING",
      change_type: "EXPANSION",
      confidence: 0.93,
      creation_reason: "Planning evidence indicates expansion",
      stage_reason: "same postcode and compatible operator/nursery name",
      signals: [{
        id: "signal-1",
        source_type: "planning",
        title: "Capacity increase at nursery",
        discovered_at: "2026-09-20T00:00:00Z",
        rule_confidence: 0.86,
        relationship_status: "ACTIVE",
        relationship_created_by: "SYSTEM",
        match_reason: "same postcode and compatible operator/nursery name",
      }],
    });
    render(<OpportunityDetail opportunityId="opp-1" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByText("Planning evidence indicates expansion")).toBeInTheDocument();
    expect(screen.queryByText("Initial signal established this opportunity.")).not.toBeInTheDocument();
    expect(screen.getAllByText("same postcode and compatible operator/nursery name").length).toBeGreaterThan(0);
  });

  it("refreshes opportunity data after recalculation succeeds", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [], total: 0 })
      .mockResolvedValueOnce({ selected: 24, created: 5, routine_only_demoted: 15 })
      .mockResolvedValueOnce({ items: [{ id: "opp-2", name: "New nursery", lifecycle_stage: "PLANNING", change_type: "OPENING", confidence: 0.86, signal_count: 1 }], total: 1 });
    render(<OpportunitiesPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("heading", { name: "Opportunities" });
    await userEvent.click(screen.getByRole("button", { name: "Recalculate opportunities" }));
    await userEvent.click(screen.getByRole("dialog").querySelector(".button.approve"));
    expect(await screen.findByText("New nursery")).toBeInTheDocument();
    expect(apiClient).toHaveBeenLastCalledWith("/admin/opportunities?limit=10&offset=0");
  });

  it("recalculates opportunities in bounded sequential batches", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [], total: 0 })
      .mockResolvedValueOnce({ selected: 25, created: 1, reused: 2, merged: 1, routine_only_demoted: 0 })
      .mockResolvedValueOnce({ selected: 3, created: 0, reused: 1, merged: 0, routine_only_demoted: 1 })
      .mockResolvedValueOnce({ items: [], total: 0 });
    render(<OpportunitiesPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("heading", { name: "Opportunities" });
    await userEvent.click(screen.getByRole("button", { name: "Recalculate opportunities" }));
    await userEvent.click(screen.getByRole("dialog").querySelector(".button.approve"));
    expect(await screen.findByRole("status")).toHaveTextContent("Recalculated 28 signals");
    expect(apiClient).toHaveBeenCalledWith("/admin/opportunities/recalculate", {
      method: "POST", body: JSON.stringify({ limit: 25, offset: 0 }),
    });
    expect(apiClient).toHaveBeenCalledWith("/admin/opportunities/recalculate", {
      method: "POST", body: JSON.stringify({ limit: 25, offset: 25 }),
    });
  });

  it("shows unmatched signals and can create an opportunity from preserved evidence", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce(listResult([pendingItem]))
      .mockResolvedValue(listResult([pendingItem]));
    const onNavigate = vi.fn();
    render(<UnmatchedSignalsPage apiClient={apiClient} onNavigate={onNavigate} />);
    expect(await screen.findByText(pendingItem.title)).toBeInTheDocument();
    expect(apiClient).toHaveBeenCalledWith(expect.stringContaining("unmatched=true"));
    expect(apiClient).toHaveBeenCalledWith(expect.not.stringContaining("include_excluded=true"));
    await userEvent.click(screen.getByRole("checkbox", { name: "Include rejected/false positives" }));
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("include_excluded=true")));
  });

  it("renders uncertain match review actions", async () => {
    const apiClient = vi.fn().mockResolvedValue({ items: [{ id: "match-1", signal_id: "signal-1", opportunity_id: "opp-1", signal_title: "Nursery Manager", opportunity_name: "Little Acorns", outcome: "UNCERTAIN", confidence: 0.55, reason: "same postcode but names differ" }], total: 1 });
    render(<MatchReviewPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByText("Nursery Manager")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Link" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });

  it("keeps stored planning reprocess behind the custom confirmation modal", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult([approvedItem]));
    render(<ReviewedSignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("button", { name: "Re-evaluate stored planning" });
    await userEvent.click(screen.getByRole("button", { name: "Re-evaluate stored planning" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("does not call Plota");
  });

  it("renders CareProspect historical metrics and starts only a bounded replay", async () => {
    const summary = {
      benchmarks: [{ benchmark_version: "care-ofsted-v1", vertical: "CHILDRENS_HOME", case_count: 24, from_date: "2025-01-01", to_date: "2026-09-01" }],
      runs: [],
      engine_version: "historical-replay-v1",
    };
    const completed = {
      id: "run-1",
      status: "SUCCESS",
      benchmark_version: "care-ofsted-v1",
      engine_version: "historical-replay-v1",
      as_of: "2026-09-27T23:59:59Z",
      idempotent: false,
      metrics: { cases_usable: 3, cases_excluded: 21, detected_cases: 2, recall: 0.6667, precision: null, unlabelled_opportunities: 1, lead_time_days: { median: 120, p25: 90, p75: 150 }, organisation_accuracy: 0.5, site_accuracy: null, reviews_per_genuine_opportunity: 0.5 },
      source_contribution: { planning: { first_discoveries: 1, corroborations: 0, missed: 2 }, recruitment: { first_discoveries: 1, corroborations: 1, missed: 1 }, combined: { found_by_either: 2, neither_detected: 1 } },
      case_results: [{ benchmark_case_uuid: "case-1", outcome_type: "REGISTERED", outcome_date: "2026-09-01", known_regulatory_id: "2766766", known_operator: "Oaktree Childcare Ltd", known_location: "Lancashire", usable: true, detected: true, opportunity_created: true, first_source: "planning", lead_time_days: 120, organisation_resolution: "STRONG", site_resolution: "UNRESOLVED_NO_SITE_TRUTH", review_items: 0 }],
    };
    const apiClient = vi.fn()
      .mockResolvedValueOnce(summary)
      .mockResolvedValueOnce({ evaluated: 0, counts: {}, changed_count: 0, changed: [] })
      .mockResolvedValueOnce(completed)
      .mockResolvedValueOnce(summary)
      .mockResolvedValueOnce({ evaluated: 0, counts: {}, changed_count: 0, changed: [] });
    render(<BacktestingPage apiClient={apiClient} selectedVertical="CHILDRENS_HOME" />);
    expect(await screen.findByRole("heading", { name: "Historical Backtesting" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run benchmark" }));
    expect(apiClient).toHaveBeenCalledWith("/admin/backtesting/run", {
      method: "POST",
      body: expect.stringContaining('"max_cases":50'),
    });
    expect(await screen.findByText("67%")).toBeInTheDocument();
    expect(screen.getAllByText("Not measurable")).toHaveLength(2);
    expect(screen.getByText("Oaktree Childcare Ltd")).toBeInTheDocument();
    expect(screen.getByText("21 excluded honestly")).toBeInTheDocument();
  });

  it("shows historical corpus coverage and imports the reviewed bounded manifest", async () => {
    const summary = {
      benchmarks: [{ benchmark_version: "care-ofsted-v1", vertical: "CHILDRENS_HOME", case_count: 21 }],
      runs: [],
      historical_research: {
        corpus_version: "care-historical-research-v1",
        summary: { benchmark_cases: 21, cases_with_planning: 4, cases_with_recruitment: 1, cases_with_both: 0, cases_with_neither: 16, candidate_items_rejected: 2 },
        cases: [{ benchmark_case_id: "ofsted:2813382", known_operator: "KDB Care Ltd", outcome_date: "2025-01-14", eligible_planning: 1, eligible_recruitment: 0, records_accepted: 1 }],
      },
    };
    const apiClient = vi.fn()
      .mockResolvedValueOnce(summary)
      .mockResolvedValueOnce({ evaluated: 0, counts: {}, changed_count: 0, changed: [] })
      .mockResolvedValueOnce({ idempotent: true, summary: summary.historical_research.summary })
      .mockResolvedValueOnce(summary)
      .mockResolvedValueOnce({ evaluated: 0, counts: {}, changed_count: 0, changed: [] });
    render(<BacktestingPage apiClient={apiClient} selectedVertical="CHILDRENS_HOME" />);
    expect(await screen.findByText("care-historical-research-v1 · official, date-verifiable evidence only")).toBeInTheDocument();
    expect(screen.getByText("KDB Care Ltd")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Import researched corpus" }));
    expect(apiClient).toHaveBeenCalledWith("/admin/backtesting/research/import", {
      method: "POST",
      body: JSON.stringify({ manifest_name: "care_historical_research_v2.json" }),
    });
    expect(await screen.findByText("The reviewed historical corpus is already imported.")).toBeInTheDocument();
  });

  it("shows the read-only recruitment preview and bounded lookback sensitivity", async () => {
    const summary = { benchmarks: [], runs: [] };
    const preview = {
      evaluated: 25,
      counts: { RELEVANT_CHANGE: 2, RELEVANT_ROUTINE: 8, UNCERTAIN: 1, IRRELEVANT: 14 },
      changed_count: 1,
      changed: [{ signal_id: "signal-1", title: "Support Worker", employer: "Example Care", location: "Nuneaton", previous_relevance: "IRRELEVANT", new_relevance: "RELEVANT_CHANGE", role_category: "support_worker", change_terms: ["new_residential_home"] }],
    };
    const sensitivity = {
      runs: [365, 450, 540].map((lookback_days, index) => ({ lookback_days, metrics: { cases_usable: 4 + index, detected_cases: 4 + index, recall: 1, lead_time_days: { median: 193 + index } } })),
    };
    const apiClient = vi.fn()
      .mockResolvedValueOnce(summary)
      .mockResolvedValueOnce(preview)
      .mockResolvedValueOnce(sensitivity)
      .mockResolvedValueOnce(summary)
      .mockResolvedValueOnce(preview);
    render(<BacktestingPage apiClient={apiClient} selectedVertical="CHILDRENS_HOME" />);
    expect(await screen.findByText("Support Worker")).toBeInTheDocument();
    expect(screen.getByText("Read-only evaluation of the latest 25 stored CareProspect recruitment records.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Compare lookbacks" }));
    expect(apiClient).toHaveBeenCalledWith("/admin/backtesting/sensitivity", {
      method: "POST",
      body: expect.stringContaining('"max_signals":1000'),
    });
    expect(await screen.findByText("450 days")).toBeInTheDocument();
  });

  it("shows procurement shadow evidence without live opportunity controls", async () => {
    const apiClient = vi.fn().mockResolvedValue({
      total: 1,
      counts: { NEW_HOME_COMMISSIONING: 1 },
      items: [{
        id: "proc-1",
        title: "Commission three new children's homes",
        source_url: "https://www.find-tender.service.gov.uk/Notice/1",
        publication_date: "2025-06-01T09:00:00Z",
        buyer: "Example Council",
        location: "Coventry",
        confidence: 0.92,
        related_signal_count: 0,
        related_opportunity_count: 0,
        appears_incremental: true,
        operator_known: false,
        metadata: {
          procurement_platform: "find_a_tender",
          notice_stage: "planning",
          procurement_category: "NEW_HOME_COMMISSIONING",
          procurement_reason: "Notice explicitly commissions new children's-home provision",
          strong_candidate: true,
        },
      }],
    });
    render(<ProcurementEvaluationPage apiClient={apiClient} />);
    expect(await screen.findByText("Commission three new children's homes")).toBeInTheDocument();
    expect(screen.getByText(/Shadow-only/)).toBeInTheDocument();
    expect(screen.getByText("No exact buyer evidence found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create opportunity/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View official notice" })).toHaveAttribute("target", "_blank");
  });
});
