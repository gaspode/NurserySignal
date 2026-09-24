import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LoginPage, SignalDetail, SignalsPage } from "./App.jsx";

const item = {
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
};

function detail() {
  return {
    ...item,
    source_url: "https://example.test/planning-1",
    raw_text: "A new nursery is proposed.",
    metadata: { capacity: 42 },
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
      review_status: "PENDING",
    },
  };
}

describe("admin frontend", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.confirm = vi.fn(() => true);
  });

  it("presents the login boundary and submits credentials", async () => {
    const onLogin = vi.fn().mockResolvedValue(undefined);
    render(<LoginPage onLogin={onLogin} />);
    expect(screen.getByRole("heading", { name: "NurserySignal" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Email"), "staff@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "not-a-real-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(onLogin).toHaveBeenCalledWith("staff@example.com", "not-a-real-password"));
  });

  it("renders signals, filters, and paginates", async () => {
    const apiClient = vi.fn().mockResolvedValue({ items: [item], total: 11, limit: 10, offset: 0 });
    const onNavigate = vi.fn();
    render(<SignalsPage apiClient={apiClient} onNavigate={onNavigate} />);
    expect(await screen.findByText(item.title)).toBeInTheDocument();
    expect(screen.getByText("86%")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Review status"), "APPROVED");
    await waitFor(() => expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("review_status=APPROVED")));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(apiClient).toHaveBeenLastCalledWith(expect.stringContaining("offset=10"));
    fireEvent.click(screen.getByText(item.title));
    expect(onNavigate).toHaveBeenCalledWith("/signals/signal-1");
  });

  it("renders detail evidence and completes approve flow", async () => {
    const apiClient = vi.fn()
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce({ signal_id: "signal-1", review_status: "APPROVED" })
      .mockResolvedValueOnce({ ...detail(), enrichment: { ...detail().enrichment, review_status: "APPROVED" } });
    render(<SignalDetail signalId="signal-1" apiClient={apiClient} onBack={vi.fn()} />);
    expect(await screen.findByText("A new nursery is proposed.")).toBeInTheDocument();
    expect(screen.getByText("View preserved JSON")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(apiClient).toHaveBeenCalledWith("/admin/signals/signal-1/approve", { method: "POST" }));
    expect(await screen.findByRole("status")).toHaveTextContent("approved successfully");
  });

  it("shows API failures without losing the page shell", async () => {
    const apiClient = vi.fn().mockRejectedValue(new Error("API unavailable"));
    render(<SignalsPage apiClient={apiClient} onNavigate={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("API unavailable");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
