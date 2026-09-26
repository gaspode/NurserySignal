import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard, LoginPage, OpportunitiesPage, ReviewInboxPage, ReviewedSignalsPage, SignalDetail, SourcesPage } from "./App.jsx";

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

  it("shows only pending records in the review inbox", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult());
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Review Inbox" })).toBeInTheDocument();
    expect(screen.getByText(pendingItem.title)).toBeInTheDocument();
    expect(screen.queryByLabelText("Review status")).not.toBeInTheDocument();
    expect(apiClient).toHaveBeenCalledWith(expect.stringContaining("review_status=PENDING"));
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

  it("uses a custom confirmation modal and opens the next pending signal after approval", async () => {
    const nativeConfirm = vi.spyOn(window, "confirm");
    const onReviewed = vi.fn();
    const apiClient = vi.fn()
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce({ signal_id: "signal-1", review_status: "APPROVED" })
      .mockResolvedValueOnce(listResult([{ ...pendingItem, id: "signal-2" }]));
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} queueMode onReviewed={onReviewed} onBack={vi.fn()} />);
    expect(await screen.findByText("A new nursery is proposed.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("Approve this signal?");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(apiClient).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("dialog").querySelector(".button.approve"));
    await waitFor(() => expect(onReviewed).toHaveBeenCalledWith({ message: "Signal approved successfully.", nextId: "signal-2" }));
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
    expect(screen.getByRole("dialog")).toHaveTextContent("Current decision: APPROVED");
    await userEvent.click(screen.getByRole("dialog").querySelector(".button.reject"));
    expect(await screen.findByRole("status")).toHaveTextContent("Signal rejected successfully.");
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
    expect(apiClient).toHaveBeenCalledWith("/admin/sources/planning/run", { method: "POST" });
    expect(await screen.findByText("Run in progress…")).toBeInTheDocument();
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

  it("links dashboard metrics to the inbox and reviewed history", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ total: 2 })
      .mockResolvedValueOnce({ total: 3 })
      .mockResolvedValueOnce({ total: 4 })
      .mockResolvedValueOnce(listResult([pendingItem]));
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
      if (callCount === 5) return new Promise((resolve) => { resolveFirstRefresh = resolve; });
      if (callCount % 4 === 1) return Promise.resolve({ total: 2 });
      if (callCount % 4 === 2) return Promise.resolve({ total: 3 });
      if (callCount % 4 === 3) return Promise.resolve({ total: 4 });
      return Promise.resolve(listResult([pendingItem]));
    });
    const onNavigate = vi.fn();
    render(<Dashboard apiClient={apiClient} onNavigate={onNavigate} />);
    await screen.findByText("Pending review");
    await userEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    expect(screen.getByRole("button", { name: "Refreshing data" })).toBeDisabled();
    expect(onNavigate).not.toHaveBeenCalled();
    resolveFirstRefresh({ total: 2 });
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).not.toBeDisabled());
    expect(apiClient).toHaveBeenCalledTimes(8);
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
      .mockResolvedValueOnce({ items: [{ id: "opp-1", name: "Little Acorns Nursery", lifecycle_stage: "STAFFING", confidence: 0.93, signal_count: 2, stage_reason: "same postcode and compatible operator/nursery name", event_type: "opening" }], total: 1 })
      .mockResolvedValueOnce({ id: "opp-1", name: "Little Acorns Nursery", lifecycle_stage: "STAFFING", confidence: 0.93, signals: [{ id: "signal-1", source_type: "planning", title: "Change of use to day nursery", discovered_at: "2026-09-20T00:00:00Z", rule_confidence: 0.86, provenance: { reason: "same postcode" } }] });
    const onNavigate = vi.fn();
    render(<OpportunitiesPage apiClient={apiClient} onNavigate={onNavigate} />);
    expect(await screen.findByText("Little Acorns Nursery")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Little Acorns Nursery"));
    expect(onNavigate).toHaveBeenCalledWith("/opportunities/opp-1");
  });

  it("keeps stored planning reprocess behind the custom confirmation modal", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult([approvedItem]));
    render(<ReviewedSignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("button", { name: "Re-evaluate stored planning" });
    await userEvent.click(screen.getByRole("button", { name: "Re-evaluate stored planning" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("does not call Plota");
  });
});
