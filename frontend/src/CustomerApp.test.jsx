import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AlertsPage, CustomerOpportunityDetail, CustomerShell, OpportunityFeed } from "./CustomerApp.jsx";

const opportunity = {
  id: "opportunity-1",
  title: "Example Care — new children’s home, Coventry",
  summary: "A planning application explicitly proposes material children’s-home provision.",
  operator: "Example Care Ltd",
  local_authority: "Coventry",
  region: "West Midlands",
  postcode: "CV1",
  location_precision: "AREA_ONLY",
  change_type: "OPENING",
  change_label: "New opening",
  stage: "PLANNING",
  stage_label: "Planning",
  strength: "Early",
  first_detected: "2026-01-01T00:00:00Z",
  last_updated: "2026-02-01T00:00:00Z",
  source_types: ["planning"],
  why: "A planning application explicitly proposes material children’s-home provision.",
  saved: false,
};

describe("CareProspect customer portal", () => {
  afterEach(() => cleanup());

  it("uses the simplified CareProspect logo in the customer shell", () => {
    const { container } = render(<CustomerShell account={{ account_name: "Pilot supplier" }} path="/care/opportunities" onLogout={vi.fn()}><p>Portal</p></CustomerShell>);
    expect(screen.getByRole("button", { name: "CareProspect opportunities" })).toBeInTheDocument();
    expect(container.querySelector(".care-brand .cp-mark")).toBeInTheDocument();
    expect(container.querySelector(".care-brand svg")).not.toBeInTheDocument();
  });

  it("renders a customer-safe opportunity feed with commercial filters", async () => {
    const api = vi.fn().mockResolvedValue({ items: [opportunity], total: 1 });
    render(<OpportunityFeed api={api} />);
    expect(await screen.findByText(opportunity.title)).toBeInTheDocument();
    expect(screen.getByLabelText("Search opportunities")).toBeInTheDocument();
    expect(screen.queryByText("Area only", { exact: false })).not.toBeInTheDocument();
    expect(screen.queryByText("review_status")).not.toBeInTheDocument();
  });

  it("shows explainable evidence without internal payloads", async () => {
    const api = vi.fn().mockImplementation((path) => {
      if (path === "/customer/opportunities/opportunity-1") {
        return Promise.resolve({
          ...opportunity,
          monitoring_message: "CareProspect continues to monitor public evidence.",
          organisation: null,
          evidence_timeline: [{
            date: "2026-01-01T00:00:00Z",
            source_type: "planning",
            source_label: "Planning",
            description: "Planning evidence identified for a material development.",
            reference: "ABC/123",
            source_url: "https://example.test/planning/ABC-123",
          }],
        });
      }
      return Promise.resolve({ status: "recorded" });
    });
    render(<CustomerOpportunityDetail api={api} id="opportunity-1" />);
    expect(await screen.findByRole("heading", { name: opportunity.title })).toBeInTheDocument();
    expect(screen.getByText("Area only — exact site is not shown")).toBeInTheDocument();
    expect(screen.getByText("Planning evidence identified for a material development.")).toBeInTheDocument();
    expect(screen.queryByText("raw_text")).not.toBeInTheDocument();
  });

  it("saves bounded weekly alert preferences and previews a digest", async () => {
    const account = { entitlements: { alert_frequencies: ["OFF", "WEEKLY"] } };
    const api = vi.fn().mockImplementation((path, options = {}) => {
      if (path === "/customer/preferences" && options.method === "PUT") {
        return Promise.resolve(JSON.parse(options.body));
      }
      if (path === "/customer/preferences") {
        return Promise.resolve({ frequency: "WEEKLY", regions: [], local_authorities: [] });
      }
      if (path === "/customer/digest/preview") {
        return Promise.resolve({ subject: "CareProspect weekly update — 1 opportunity", opportunities: [opportunity], delivery_status: "PREVIEW_ONLY" });
      }
      return Promise.resolve({});
    });
    render(<AlertsPage api={api} account={account} />);
    await userEvent.click(await screen.findByRole("button", { name: "Save preferences" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/customer/preferences", expect.objectContaining({ method: "PUT" })));
    await userEvent.click(screen.getByRole("button", { name: "Generate preview" }));
    expect(await screen.findByText("CareProspect weekly update — 1 opportunity")).toBeInTheDocument();
    expect(screen.getByText(/verified sender/i)).toBeInTheDocument();
  });
});
