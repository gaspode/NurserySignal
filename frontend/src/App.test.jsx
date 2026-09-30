import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AdminNavigation, BacktestingPage, CustomersPage, Dashboard, LoginPage, MatchReviewPage, OpportunitiesPage, OpportunityDetail, OpportunityHygienePage, OrganisationsPage, ProcurementEvaluationPage, ReviewInboxPage, ReviewedSignalsPage, SignalDetail, SourcesPage, UnmatchedSignalsPage, restoredVertical, verticalScopedPath } from "./App.jsx";

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

const hygieneItem = {
  opportunity_id: "care-opp-1",
  name: "New children's home — Liverpool L19",
  lifecycle_stage: "PLANNING",
  change_type: "OPENING",
  confidence: 0.95,
  town: "Liverpool",
  postcode: "L19",
  supporting_signal_count: 1,
  source_mix: "PLANNING",
  category: "NEEDS_INVESTIGATION",
  hygiene_reason: "Active evidence lacks decisive event semantics.",
  warning: "Active evidence lacks decisive event semantics.",
  publication_status: "PUBLISHED",
  admin_touch_types: ["manual_publication"],
};

function hygieneResult(overrides = {}) {
  return {
    total: 1143,
    filtered_total: 30,
    limit: 25,
    offset: 0,
    category_counts: {
      VALID_SUPPORTED: 754,
      UNSUPPORTED_ORPHAN_CANDIDATE: 345,
      NEEDS_INVESTIGATION: 30,
      MANUAL_OR_ADMIN_TOUCHED_PRESERVE: 14,
      DUPLICATE_CANDIDATE: 0,
      SUPERSEDED_CANDIDATE: 0,
    },
    orphan_root_causes: { SIGNAL_REJECTED: 295, PLANNING_REFUSED: 9 },
    change_type_counts: { OPENING: 1088, EXPANSION: 52 },
    publication_counts: { DRAFT: 1137, PUBLISHED: 6 },
    customer_readiness_total: 749,
    items: [hygieneItem],
    read_only: true,
    ...overrides,
  };
}

const triageResult = {
  vertical: "NURSERY",
  pending_buckets: {
    EXPLICIT_PLANNING_REFUSAL: 2,
    SAFE_APPROVE_AGREEMENT: 7,
    QA_HOLDOUT_CARE_AI_APPROVAL: 9,
    QA_HOLDOUT_CARE_LAWFULNESS: 1,
    DETERMINISTIC_AI_DISAGREE: 3,
    AI_UNCERTAIN: 4,
    MANUAL_REVIEW_REQUIRED: 5,
  },
  safe_bulk_approval_recommended: false,
  care_planning_ai_approval_monitoring: {
    policy_version: "care-planning-ai-approval-v1.1",
    future_qa_target_percent: 5,
    auto_approved: 142,
    qa_holdouts: 18,
    by_policy_version: {
      "care-planning-ai-approval-v1": { qa_approved: 18, qa_rejected: 0 },
    },
  },
  care_planning_lawfulness_monitoring: {
    policy_version: "care-planning-lawfulness-proposed-v1",
    auto_approved: 0,
    qa_holdouts: 1,
    qa_holdouts_rejected: 0,
  },
};

describe("admin frontend", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("groups admin navigation around signals, opportunities and organisations", async () => {
    const onNavigate = vi.fn();
    render(<AdminNavigation currentPath="/opportunity-hygiene?view=publication_candidates" onNavigate={onNavigate} />);

    expect(screen.getByText("Signals")).toBeInTheDocument();
    expect(screen.getByText("Opportunities")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review queue" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All signals" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unmatched" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All opportunities" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Needs attention" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publication candidates" })).toHaveClass("active");
    expect(screen.getByRole("button", { name: "Match review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Organisations" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Source status" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Customers" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Procurement" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Procurement evaluation" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Review queue" }));
    await userEvent.click(screen.getByRole("button", { name: "Needs attention" }));
    expect(onNavigate).toHaveBeenCalledWith("/inbox");
    expect(onNavigate).toHaveBeenCalledWith("/opportunity-hygiene");
  });

  it("renders the grouped navigation as a stacked mobile menu", () => {
    render(<AdminNavigation currentPath="/history" onNavigate={vi.fn()} mobile />);
    const navigation = screen.getByRole("navigation", { name: "Mobile navigation" });
    expect(navigation).toHaveClass("mobile-navigation-list");
    expect(within(navigation).getByRole("button", { name: "All signals" })).toHaveClass("active");
    expect(within(navigation).getByRole("button", { name: "Publication candidates" })).toBeInTheDocument();
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
    expect(await screen.findByRole("heading", { name: "Review queue" })).toBeInTheDocument();
    expect(screen.getByText(pendingItem.title)).toBeInTheDocument();
    expect(screen.queryByLabelText("Review status")).not.toBeInTheDocument();
    expect(apiClient).toHaveBeenCalledWith(expect.stringContaining("review_status=PENDING"));
  });

  it("offers immediate row actions without a confirmation dialog", async () => {
    const nativeConfirm = vi.spyOn(window, "confirm");
    const apiClient = vi.fn()
      .mockResolvedValueOnce(listResult())
      .mockResolvedValueOnce(triageResult)
      .mockResolvedValueOnce({ signal_id: "signal-1", review_status: "APPROVED" })
      .mockResolvedValueOnce(listResult([], 0))
      .mockResolvedValueOnce(triageResult);
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByText(pendingItem.title);
    await userEvent.click(screen.getByRole("button", { name: `Actions for ${pendingItem.title}` }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Approve" }));
    expect(nativeConfirm).not.toHaveBeenCalled();
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith("/admin/signals/signal-1/approve", { method: "POST" }));
    expect(await screen.findByText("Queue clear")).toBeInTheDocument();
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
    await userEvent.click(screen.getByRole("heading", { name: "Review queue" }));
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
      .mockResolvedValueOnce(triageResult)
      .mockResolvedValueOnce({ updated: 2 })
      .mockResolvedValueOnce(listResult([], 0))
      .mockResolvedValueOnce(triageResult);
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

  it("supports all-signal reference search, status filtering and pagination", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult([approvedItem], 11));
    render(<ReviewedSignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "All signals" })).toBeInTheDocument();
    expect(screen.getByText(approvedItem.title)).toBeInTheDocument();
    expect(screen.getByLabelText("Review status")).toHaveValue("");
    await userEvent.type(screen.getByLabelText("Search all signals"), "24/03385/FUL");
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("q=24%2F03385%2FFUL")));
    await userEvent.selectOptions(screen.getByLabelText("Review status"), "PENDING");
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("review_status=PENDING")));
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
    expect(await screen.findByText("Queue clear")).toBeInTheDocument();
    expect(screen.getByText("There are no pending signals waiting for review.")).toBeInTheDocument();
  });

  it("shows risk-first review triage buckets without enabling AI rejection", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce(listResult())
      .mockResolvedValueOnce(triageResult);
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Review triage" })).toBeInTheDocument();
    expect(screen.getByText(/AI rejection remains advisory/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Rule\/AI disagree 3/ })).toHaveTextContent("3");
    expect(screen.getByRole("button", { name: /Care AI approval QA 9/ })).toHaveTextContent("9");
    expect(screen.getByRole("button", { name: /Care lawfulness QA 1/ })).toHaveTextContent("1");
    expect(screen.getByText(/care-planning-lawfulness-proposed-v1/)).toBeInTheDocument();
    expect(screen.getByText(/5% future QA target/)).toBeInTheDocument();
    expect(screen.getByText(/Historical v1 validation: 18 approved · 0 rejected/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve safe agreement set" })).not.toBeInTheDocument();
  });

  it("shows CareProspect AI currency and runs only a bounded stale refresh", async () => {
    let refreshed = false;
    const careTriage = {
      ...triageResult,
      care_planning_ai_currency: {
        prompt_version: "care-planning-shadow-v2",
        counts: { CURRENT_V2: refreshed ? 2 : 1, STALE_V1: refreshed ? 0 : 1, NO_AI_ASSESSMENT: 0, AI_FAILED: 0 },
      },
    };
    const apiClient = vi.fn(async (path, options = {}) => {
      if (path === "/admin/review-triage") {
        return {
          ...careTriage,
          care_planning_ai_currency: {
            ...careTriage.care_planning_ai_currency,
            counts: { CURRENT_V2: refreshed ? 2 : 1, STALE_V1: refreshed ? 0 : 1, NO_AI_ASSESSMENT: 0, AI_FAILED: 0 },
          },
        };
      }
      if (path === "/admin/review-triage/care-planning/ai-validation" && options.method === "POST") {
        refreshed = true;
        return { succeeded: 1, failed: 0, idempotent_skips: 0 };
      }
      return listResult([{ ...pendingItem, vertical: "CHILDRENS_HOME", ai_currency: refreshed ? "CURRENT_V2" : "STALE_V1" }]);
    });
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByText(/CareProspect AI: 1 current · 1 stale/)).toBeInTheDocument();
    expect(screen.getByText("AI stale")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh stale CareProspect AI" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Refresh 10" }));
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith(
      "/admin/review-triage/care-planning/ai-validation",
      {
        method: "POST",
        body: JSON.stringify({ operation: "refresh_stale", limit: 10, include_missing: true }),
      }
    ));
    expect(await screen.findByText(/CareProspect AI refresh: 1 updated/)).toBeInTheDocument();
  });

  it("filters CareProspect planning by the server-side subtype", async () => {
    const careItem = {
      ...pendingItem,
      vertical: "CHILDRENS_HOME",
      title: "Use of dwellinghouse as a children's care home",
      extracted_facts: {
        planning_subtype: "NEW_HOME_CHANGE_OF_USE",
      },
    };
    const apiClient = vi.fn(async (path) => {
      if (path === "/admin/review-triage") return triageResult;
      if (path.includes("planning_subtype=NEW_HOME_CHANGE_OF_USE")) {
        return listResult([careItem]);
      }
      return listResult();
    });
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("heading", { name: "Review triage" });
    expect(screen.getByRole("option", { name: "New home — mixed-use development" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Expansion / capacity change" })).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Cessation / change away from care" })
    ).toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByLabelText("Planning subtype"),
      "NEW_HOME_CHANGE_OF_USE"
    );
    await waitFor(() =>
      expect(apiClient).toHaveBeenCalledWith(
        expect.stringContaining("planning_subtype=NEW_HOME_CHANGE_OF_USE")
      )
    );
    expect(await screen.findByText(careItem.title)).toBeInTheDocument();
    expect(screen.getAllByText("Explicit new home — change of use").length).toBeGreaterThan(1);
  });

  it("filters to the safe-agreement cohort and keeps it active after manual review", async () => {
    let reviewed = false;
    const apiClient = vi.fn(async (path, options = {}) => {
      if (path === "/admin/signals/signal-1/approve" && options.method === "POST") {
        reviewed = true;
        return { signal_id: "signal-1", review_status: "APPROVED" };
      }
      if (path === "/admin/review-triage") return triageResult;
      if (path.includes("triage_bucket=SAFE_APPROVE_AGREEMENT")) {
        return reviewed ? listResult([], 6) : listResult([pendingItem], 7);
      }
      return listResult();
    });
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("heading", { name: "Review triage" });

    await userEvent.click(screen.getByRole("button", { name: /Safe approve agreement 7/ }));
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith(
      expect.stringContaining("triage_bucket=SAFE_APPROVE_AGREEMENT")
    ));
    expect(screen.getByLabelText("Triage bucket")).toHaveValue("SAFE_APPROVE_AGREEMENT");
    expect(screen.getByRole("status")).toHaveTextContent("Safe agreement candidates: 7 remaining");

    await userEvent.click(screen.getByRole("button", { name: `Actions for ${pendingItem.title}` }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Approve" }));
    await waitFor(() => expect(screen.getByText(/Safe agreement candidates:/).closest(".triage-remaining")).toHaveTextContent(
      "Safe agreement candidates: 6 remaining"
    ));
    expect(apiClient).not.toHaveBeenCalledWith(
      "/admin/review-triage/safe-approve",
      expect.objectContaining({ body: expect.stringContaining('"preview":false') })
    );
  });

  it("previews and confirms bounded NurserySignal safe approval with QA holdout", async () => {
    const evaluatedTriage = {
      ...triageResult,
      recommended_safe_threshold: 0.97,
      safe_bulk_approval_recommended: true,
    };
    const apiClient = vi.fn()
      .mockResolvedValueOnce(listResult())
      .mockResolvedValueOnce(evaluatedTriage)
      .mockResolvedValueOnce({ preview: true, batch_count: 12, eligible_total: 19, would_auto_approve: 11, qa_holdouts: 1 })
      .mockResolvedValueOnce({ preview: false, updated: 12, auto_approved: 11, qa_holdouts: 1 })
      .mockResolvedValueOnce(listResult([], 0))
      .mockResolvedValueOnce(evaluatedTriage);
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("heading", { name: "Review triage" });

    await userEvent.click(screen.getByRole("button", { name: "Preview NurserySignal backlog" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "19 NurserySignal records qualify: 11 would auto-approve and 1 would remain as QA holdouts."
    );
    await userEvent.click(screen.getByRole("button", { name: "Apply NurserySignal policy" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("Stable 10% QA holdouts remain pending");
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Apply NurserySignal policy",
      })
    );
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith(
      "/admin/review-triage/safe-approve",
      {
        method: "POST",
        body: JSON.stringify({ preview: false, limit: 100, threshold: 0.95, vertical: "NURSERY" }),
      }
    ));
  });

  it("reviews a signal directly from detail and opens the next pending signal", async () => {
    const nativeConfirm = vi.spyOn(window, "confirm");
    const onReviewed = vi.fn();
    const apiClient = vi.fn()
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce({ signal_id: "signal-1", review_status: "APPROVED" })
      .mockResolvedValueOnce(listResult([{ ...pendingItem, id: "signal-2" }]));
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} queueMode queueQuery="triage_bucket=SAFE_APPROVE_AGREEMENT&offset=10" onReviewed={onReviewed} onBack={vi.fn()} />);
    expect(await screen.findByText("A new nursery is proposed.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(onReviewed).toHaveBeenCalledWith({ message: "Signal approved successfully.", nextId: "signal-2" }));
    expect(apiClient).toHaveBeenCalledWith(
      "/admin/signals?triage_bucket=SAFE_APPROVE_AGREEMENT&offset=0&review_status=PENDING&limit=1"
    );
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
    const onNavigate = vi.fn();
    render(<SourcesPage apiClient={apiClient} onNavigate={onNavigate} />);
    expect(await screen.findByRole("heading", { name: "Sources" })).toBeInTheDocument();
    expect(screen.getByText("Planning applications")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open procurement evaluation" }));
    expect(onNavigate).toHaveBeenCalledWith("/procurement");
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

  it("shows the CareProspect lifecycle watcher as a read-only preview", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({
        watcher: {
          would_watch: 42,
          due: 0,
          checked_last_24h: 0,
          provider_errors: 0,
          estimated_requests_per_day: 14,
          estimated_requests_per_30_days: 420,
        },
      });
    render(<SourcesPage apiClient={apiClient} selectedVertical="CHILDRENS_HOME" />);
    expect(await screen.findByRole("heading", { name: "Lifecycle refresh preview" })).toBeInTheDocument();
    expect(screen.getByText("Preview only")).toBeInTheDocument();
    expect(screen.getByText(/Automatic refresh, lifecycle bootstrap, publication and withdrawal remain disabled/)).toBeInTheDocument();
    expect(apiClient).toHaveBeenCalledWith("/admin/opportunities/lifecycle-preview");
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

  it("starts a bounded weekly historical Planning backfill", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({
        run_id: "backfill-1",
        status: "RUNNING",
        parameters: { chunks_total: 79 },
      });
    render(<SourcesPage apiClient={apiClient} selectedVertical="ALL" />);
    await screen.findByRole("heading", { name: "Planning historical backfill" });
    expect(screen.getByText(/does not auto-publish customer opportunities/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start bounded backfill" }));
    expect(apiClient).toHaveBeenCalledWith(
      "/admin/sources/planning/backfill",
      expect.objectContaining({ method: "POST" }),
    );
    const request = JSON.parse(apiClient.mock.calls[1][1].body);
    expect(request.vertical).toBe("ALL");
    expect(request.chunk_days).toBe(7);
    expect(request.max_records).toBe(4000);
    expect(await screen.findByText(/started in 79 weekly chunks/i)).toBeInTheDocument();
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

  it("shows public authorities without Companies House resolution controls", async () => {
    const authority = { id: "operator-council", name: "Lancashire County Council", legal_name: "Lancashire County Council", vertical: "CHILDRENS_HOME", organisation_type: "PUBLIC_AUTHORITY", companies_house_number: null, signal_count: 3, opportunity_count: 1 };
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [authority] })
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ ...authority, aliases: [{ alias: "Lancashire CC" }], registered_office: {}, sic_codes: [], opportunities: [], ofsted_corroboration: [], public_authority_conflict: false });
    render(<OrganisationsPage apiClient={apiClient} />);
    expect(await screen.findByText("Public authority")).toBeInTheDocument();
    expect(screen.getByText("Companies House match not applicable")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Lancashire County Council"));
    expect(await screen.findByText("Not applicable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Return to company resolution" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use this company" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Companies House number")).not.toBeInTheDocument();
  });

  it("shows a warning instead of hiding a public-authority mapping conflict", async () => {
    const authority = { id: "operator-council", name: "Example City Council", vertical: "CHILDRENS_HOME", organisation_type: "PUBLIC_AUTHORITY", companies_house_number: "12345678", public_authority_conflict: true, signal_count: 1, opportunity_count: 1 };
    const apiClient = vi.fn()
      .mockResolvedValueOnce({ items: [authority] })
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ ...authority, aliases: [], registered_office: {}, sic_codes: [], opportunities: [], ofsted_corroboration: [] });
    render(<OrganisationsPage apiClient={apiClient} />);
    await userEvent.click(await screen.findByText("Example City Council"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "historical Companies House mapping"
    );
    expect(screen.getByText("12345678")).toBeInTheDocument();
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
    await screen.findByText("Signals awaiting review");
    await userEvent.click(screen.getByText("Signals awaiting review"));
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
    await screen.findByText("Signals awaiting review");
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
    expect(verticalScopedPath("/admin/review-triage", "CHILDRENS_HOME")).toContain("vertical=CHILDRENS_HOME");
  });

  it("refreshes the pending inbox without changing its pagination state or navigation", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult());
    const onNavigate = vi.fn();
    render(<ReviewInboxPage apiClient={apiClient} onNavigate={onNavigate} />);
    await screen.findByText(pendingItem.title);
    await userEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    await waitFor(() => expect(apiClient).toHaveBeenCalledTimes(4));
    expect(apiClient).toHaveBeenCalledWith(expect.stringContaining("review_status=PENDING"));
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("preserves reviewed history search state when refreshing", async () => {
    const apiClient = vi.fn().mockResolvedValue(listResult([approvedItem], 1));
    render(<ReviewedSignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByText(approvedItem.title);
    await userEvent.type(screen.getByLabelText("Search all signals"), "Bristol");
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

  it("renders read-only opportunity hygiene counts, filters and detail navigation", async () => {
    const apiClient = vi.fn().mockResolvedValue(hygieneResult());
    const onNavigate = vi.fn();
    render(<OpportunityHygienePage apiClient={apiClient} onNavigate={onNavigate} />);

    expect(await screen.findByRole("heading", { name: "Needs attention" })).toBeInTheDocument();
    expect(apiClient).toHaveBeenCalledWith(expect.stringContaining("view=needs_attention"));
    expect(screen.queryByRole("button", { name: /Valid supported/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Unsupported \/ orphan/i })).toHaveTextContent("345");
    expect(screen.getByRole("button", { name: /Needs investigation/i })).toHaveTextContent("30");
    expect(screen.getByText("Published warning")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Needs investigation/i }));
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("category=NEEDS_INVESTIGATION")));
    await userEvent.click(screen.getByRole("button", { name: "View opportunity" }));
    expect(onNavigate).toHaveBeenCalledWith(expect.stringMatching(/^\/opportunities\/care-opp-1\?from=opportunity-hygiene/));

    for (const action of ["Merge", "Split", "Unlink", "Retire", "Deactivate", "Delete", "Supersede", "Publish"]) {
      expect(screen.queryByRole("button", { name: action })).not.toBeInTheDocument();
    }
  });

  it("supports orphan causes, publication candidates and server pagination", async () => {
    const apiClient = vi.fn().mockResolvedValue(hygieneResult());
    render(<OpportunityHygienePage apiClient={apiClient} onNavigate={vi.fn()} />);
    await screen.findByRole("heading", { name: "Needs attention" });

    await userEvent.click(screen.getByRole("button", { name: /Unsupported \/ orphan/i }));
    const rootCause = await screen.findByLabelText("Unsupported root cause");
    await userEvent.selectOptions(rootCause, "SIGNAL_REJECTED");
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("root_cause=SIGNAL_REJECTED")));

    await userEvent.click(screen.getByRole("button", { name: "Publication candidates" }));
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("view=publication_candidates")));
    expect(screen.getByRole("heading", { name: "Publication candidates" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("offset=25")));
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

  it("links directly from opportunity evidence to safe external sources", async () => {
    window.location.hash = "#/opportunities/opp-1?from=opportunity-hygiene&page=2";
    const apiClient = vi.fn().mockResolvedValue({
      id: "opp-1",
      name: "New children's home — Coventry",
      lifecycle_stage: "PLANNING",
      change_type: "OPENING",
      confidence: 0.95,
      signals: [
        { id: "planning-1", source_type: "planning", title: "Change of use", source_url: "https://planning.example/ABC-1", relationship_status: "ACTIVE" },
        { id: "recruitment-1", source_type: "recruitment", title: "Registered manager", source_url: null, relationship_status: "ACTIVE" },
        { id: "ofsted-1", source_type: "ofsted", title: "Registration update", source_url: "javascript:alert(1)", relationship_status: "ACTIVE" },
        { id: "procurement-1", source_type: "procurement", title: "Commissioning notice", source_url: "http://procurement.example/notice/1", relationship_status: "ACTIVE" },
      ],
    });
    render(<OpportunityDetail opportunityId="opp-1" apiClient={apiClient} onBack={vi.fn()} />);

    expect(await screen.findAllByRole("button", { name: "Open signal" })).toHaveLength(4);
    const sourceLinks = screen.getAllByRole("link", { name: "Open source" });
    expect(sourceLinks).toHaveLength(2);
    expect(sourceLinks[0]).toHaveAttribute("href", "https://planning.example/ABC-1");
    expect(sourceLinks[1]).toHaveAttribute("href", "http://procurement.example/notice/1");
    for (const link of sourceLinks) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noreferrer");
    }
    expect(window.location.hash).toBe("#/opportunities/opp-1?from=opportunity-hygiene&page=2");
  });

  it("shows authoritative Planning semantics without cluttering non-Planning evidence", async () => {
    const apiClient = vi.fn().mockResolvedValue({
      id: "opp-planning-semantics",
      name: "New children's home — Exampletown",
      lifecycle_stage: "PLANNING",
      change_type: "OPENING",
      confidence: 0.95,
      signals: [
        { id: "approved", source_type: "planning", title: "C3 to C2 change", relationship_status: "ACTIVE", match_reason: "merged from 380399bb-example", source_url: "https://planning.example/approved", planning_outcome: "APPROVED", planning_decision_raw: "Grant Permission Subject To Conditions", planning_status_raw: "Decided", planning_subtype: "NEW_HOME_CHANGE_OF_USE", opportunity_creation_decision: "CREATE_OPPORTUNITY", planning_families: [{ id: "family-approved", relationship_type: "PRIMARY_APPLICATION", planning_authority: "Example Council", raw_reference: "24/001/FUL", origin_status: "FOUND" }] },
        { id: "followup", source_type: "planning", title: "Condition details", relationship_status: "ACTIVE", planning_outcome: "APPROVED", planning_subtype: "CONDITION_DISCHARGE", opportunity_creation_decision: "SUPPORT_EXISTING_ONLY", planning_families: [{ id: "family-followup", relationship_type: "REFERENCES_APPLICATION", planning_authority: "Example Council", raw_reference: "24/001/FUL", origin_status: "FOUND" }] },
        { id: "refused", source_type: "planning", title: "Refused opening", relationship_status: "ACTIVE", planning_outcome: "REFUSED", planning_decision_raw: "Refused LUC", planning_subtype: "NEW_HOME_OTHER_EXPLICIT", opportunity_creation_decision: "CREATE_OPPORTUNITY", planning_consistency_warning: true },
        { id: "withdrawn", source_type: "planning", title: "Withdrawn application", relationship_status: "ACTIVE", planning_outcome: "WITHDRAWN", planning_subtype: "AMBIGUOUS", opportunity_creation_decision: "REVIEW" },
        { id: "pending", source_type: "planning", title: "Pending lawfulness", relationship_status: "ACTIVE", planning_outcome: "PENDING", planning_subtype: "LAWFULNESS_PROPOSED", opportunity_creation_decision: "CREATE_OPPORTUNITY" },
        { id: "missing", source_type: "planning", title: "Historical planning record", relationship_status: "ACTIVE", planning_outcome: "UNKNOWN", planning_subtype: null, opportunity_creation_decision: null },
        { id: "recruitment", source_type: "recruitment", title: "Registered manager vacancy", relationship_status: "ACTIVE" },
      ],
    });

    render(<OpportunityDetail opportunityId="opp-planning-semantics" apiClient={apiClient} onBack={vi.fn()} />);

    const approved = (await screen.findByText("C3 to C2 change")).closest("article");
    expect(approved).toHaveTextContent("Decision: Approved");
    expect(approved).toHaveTextContent("Explicit new home — change of use");
    expect(approved).toHaveTextContent("Opportunity action: Create opportunity");
    expect(approved).toHaveTextContent("Foundational application");
    expect(approved).toHaveTextContent("Raw council decision: Grant Permission Subject To Conditions");
    expect(approved).toHaveTextContent("Raw council status: Decided");
    expect(within(approved).getByText("Relationship provenance")).toBeInTheDocument();
    expect(approved).toHaveTextContent("merged from 380399bb-example");
    expect(within(approved).getByRole("button", { name: "Open signal" })).toBeInTheDocument();
    expect(within(approved).getByRole("link", { name: "Open source" })).toBeInTheDocument();
    expect(within(approved).getByRole("button", { name: "Unlink" })).toBeInTheDocument();
    expect(within(approved).getByText("Approved")).toHaveClass("badge-approved");

    const followup = screen.getByText("Condition details").closest("article");
    expect(followup).toHaveTextContent("Supporting follow-up");
    expect(followup).toHaveTextContent("Condition discharge");
    expect(followup).toHaveTextContent("Support existing only");
    const refused = screen.getByText("Refused opening").closest("article");
    expect(within(refused).getByText("Refused")).toHaveClass("badge-rejected");
    expect(refused).toHaveTextContent("Raw council decision: Refused LUC");
    expect(refused).toHaveTextContent("Negative Planning outcome on active opportunity evidence");
    expect(within(screen.getByText("Withdrawn application").closest("article")).getByText("Withdrawn")).toHaveClass("badge-rejected");
    expect(within(screen.getByText("Pending lawfulness").closest("article")).getByText("Pending")).toHaveClass("badge-pending");
    expect(screen.getByText("Historical planning record").closest("article")).toHaveTextContent("Decision: Unknown");
    expect(screen.getByText("Registered manager vacancy").closest("article")).not.toHaveTextContent("Decision:");
  });

  it("preserves hygiene review context and exposes planning-family recovery", async () => {
    const opportunity = {
      id: "opp-2", name: "Care home evidence", lifecycle_stage: "PLANNING",
      change_type: "OPENING", confidence: 0.9,
      evidence_support: { foundational: 0, supporting_followups: 1, unresolved_origins: 1 },
      signals: [{
        id: "followup-1", source_type: "planning", title: "Discharge of conditions",
        relationship_status: "ACTIVE", planning_families: [{
          id: "family-1", planning_authority: "Example Council", raw_reference: "24/03385/FUL",
          relationship_type: "REFERENCES_APPLICATION", origin_status: "MISSING",
          latest_recovery_status: null,
        }],
      }],
    };
    const apiClient = vi.fn(async (path, options) => {
      if (path === "/admin/opportunities/opp-2") return opportunity;
      if (path.includes("offset=3")) return { items: [{ opportunity_id: "opp-prev" }] };
      if (path.includes("offset=5")) return { items: [{ opportunity_id: "opp-next" }] };
      if (path === "/admin/signals/followup-1/planning-origin" && options?.method === "POST") return { queued: 1 };
      return { items: [] };
    });
    const onBack = vi.fn(); const onNavigate = vi.fn();
    render(<OpportunityDetail opportunityId="opp-2" apiClient={apiClient} onBack={onBack} onNavigate={onNavigate} contextQuery="from=opportunity-hygiene&category=NEEDS_INVESTIGATION&hygiene_offset=4&hygiene_total=8" />);
    expect(await screen.findByText("0 foundational signals · 1 supporting follow-up signal")).toBeInTheDocument();
    expect(screen.getByText("Supporting follow-up")).toBeInTheDocument();
    expect(screen.getByText("Originating planning application has not yet been resolved.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Next →" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Next →" }));
    expect(onNavigate).toHaveBeenCalledWith(expect.stringContaining("/opportunities/opp-next?"));
    await userEvent.click(screen.getByRole("button", { name: "Find referenced application" }));
    expect(apiClient).toHaveBeenCalledWith("/admin/signals/followup-1/planning-origin", expect.objectContaining({ method: "POST" }));
  });

  it("preserves publication-candidate context through publish and advance", async () => {
    const opportunity = {
      id: "opp-publication", name: "New children's home — Coventry", vertical: "CHILDRENS_HOME",
      lifecycle_stage: "PLANNING", change_type: "OPENING", confidence: 0.95,
      publication_status: "DRAFT", signals: [],
    };
    const apiClient = vi.fn(async (path) => {
      if (path === "/admin/opportunities/opp-publication") return opportunity;
      if (path.startsWith("/admin/opportunities/hygiene-audit?")) return { items: [{ opportunity_id: "opp-next" }] };
      if (path === "/admin/opportunities/opp-publication/publication") return { publication_status: "PUBLISHED" };
      return {};
    });
    const onNavigate = vi.fn();
    render(<OpportunityDetail opportunityId="opp-publication" apiClient={apiClient} onBack={vi.fn()} onNavigate={onNavigate} contextQuery="from=opportunity-hygiene&view=publication_candidates&hygiene_path=%2Fopportunity-hygiene&hygiene_offset=0&hygiene_total=2" />);

    expect(await screen.findByRole("button", { name: "← Back to Publication candidates" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Next →" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Publish to CareProspect" }));
    await waitFor(() => expect(onNavigate).toHaveBeenCalledWith(expect.stringContaining("/opportunities/opp-next?")));
    expect(onNavigate).toHaveBeenCalledWith(expect.stringContaining("view=publication_candidates"));
  });

  it("shows the derived CareProspect lifecycle and Planning watch state", async () => {
    const apiClient = vi.fn().mockResolvedValue({
      id: "opp-lifecycle", name: "New children's home — Oxford", vertical: "CHILDRENS_HOME",
      lifecycle_stage: "PLANNING", change_type: "OPENING", confidence: 0.95,
      publication_status: "DRAFT", signals: [], evidence_support: { foundational: 1, supporting_followups: 0, unresolved_origins: 0 },
      derived_customer_lifecycle: "PLANNING_PENDING",
      derived_customer_lifecycle_reason: "A reviewed foundational Planning application is pending or not yet decided.",
      derived_customer_lifecycle_policy_version: "care-opportunity-lifecycle-v1",
      publication_automation_blocked: false,
      planning_lifecycle_watches: [{ planning_authority: "Oxford", planning_reference: "24/1234/FUL", latest_outcome: "PENDING", last_checked_at: null, next_eligible_refresh_at: "2026-10-03T00:00:00Z" }],
      lifecycle_history: [],
    });
    render(<OpportunityDetail opportunityId="opp-lifecycle" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Customer lifecycle" })).toBeInTheDocument();
    expect(screen.getByText(/planning pending/i)).toBeInTheDocument();
    expect(screen.getByText("24/1234/FUL")).toBeInTheDocument();
    expect(screen.getByText(/Preview only — not bootstrapped/)).toBeInTheDocument();
    expect(screen.getByText(/automatic publication is not enabled/i)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Publication automation block reason"), "Needs manual review");
    await userEvent.click(screen.getByRole("button", { name: "Block publication automation" }));
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith(
      "/admin/opportunities/opp-lifecycle/automation-block",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("shows ambiguous Planning origin candidates without choosing one", async () => {
    const opportunity = {
      id: "opp-ambiguous", name: "Ambiguous origin", lifecycle_stage: "PLANNING",
      change_type: "OPENING", confidence: 0.9,
      evidence_support: { foundational: 0, supporting_followups: 1, unresolved_origins: 1 },
      signals: [{
        id: "followup-ambiguous", source_type: "planning", title: "Condition discharge",
        relationship_status: "ACTIVE", planning_families: [{
          id: "family-ambiguous", planning_authority: "Croydon", raw_reference: "24/03385/FUL",
          relationship_type: "REFERENCES_APPLICATION", origin_status: "AMBIGUOUS",
          latest_recovery_status: "AMBIGUOUS",
          latest_recovery_details: { candidates: [
            { provider_id: "one", authority: "Sheffield", application_date: "2024-01-01", address: "1 Other Road", source_url: "https://planning.example/one" },
            { provider_id: "two", authority: "Enfield", application_date: "2024-01-02", address: "2 Other Road", source_url: "https://planning.example/two" },
          ] },
        }],
      }],
    };
    const apiClient = vi.fn(async () => opportunity);
    render(<OpportunityDetail opportunityId="opp-ambiguous" apiClient={apiClient} onBack={vi.fn()} onNavigate={vi.fn()} />);

    expect(await screen.findByText("Multiple referenced applications found (2)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry lookup" })).toBeEnabled();
    expect(screen.getByText((_, element) => element?.classList.contains("cell-subtitle") && element.textContent.includes("Lookup: AMBIGUOUS"))).toBeInTheDocument();
  });

  it("previews a privacy-safe generated CareProspect title when the override is blank", async () => {
    const apiClient = vi.fn().mockResolvedValue({
      id: "care-opp-1",
      name: "New children's home — 10 Kingswood Road Nottingham NG8 1LD",
      address: "10 Kingswood Road",
      postcode: "NG8 1LD",
      town: "Nottingham",
      vertical: "CHILDRENS_HOME",
      lifecycle_stage: "PLANNING",
      change_type: "OPENING",
      confidence: 0.95,
      publication_status: "DRAFT",
      customer_title: null,
      customer_summary: null,
      default_customer_title: "New children’s home — Nottingham, NG8",
      default_customer_summary: "A planning application explicitly proposes material children’s-home provision.",
      signals: [],
    });
    render(<OpportunityDetail opportunityId="care-opp-1" apiClient={apiClient} onBack={vi.fn()} />);
    const preview = await screen.findByText(/Default customer title:/);
    expect(preview).toHaveTextContent("New children’s home — Nottingham, NG8");
    expect(preview).not.toHaveTextContent("Kingswood Road");
    expect(preview).not.toHaveTextContent("NG8 1LD");
    expect(screen.getByText(/Default summary:/)).toHaveTextContent(
      "A planning application explicitly proposes material children’s-home provision."
    );
  });

  it("preserves an explicit CareProspect customer title override", async () => {
    const apiClient = vi.fn().mockResolvedValue({
      id: "care-opp-2",
      name: "Internal opportunity name",
      vertical: "CHILDRENS_HOME",
      lifecycle_stage: "PLANNING",
      change_type: "OPENING",
      confidence: 0.95,
      publication_status: "PUBLISHED",
      customer_title: "Manually curated customer title",
      customer_summary: "Manually curated customer summary",
      default_customer_title: "New children’s home — Liverpool, L5",
      signals: [],
    });
    render(<OpportunityDetail opportunityId="care-opp-2" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByLabelText("Customer title")).toHaveValue(
      "Manually curated customer title"
    );
    expect(screen.queryByText(/Default customer title:/)).not.toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Customer title"));
    expect(screen.getByText(/Default customer title:/)).toHaveTextContent(
      "New children’s home — Liverpool, L5"
    );
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
