import { readFileSync } from "node:fs";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import PublicSite, { PublicLegalPage } from "./PublicSite.jsx";

const styles = readFileSync("src/styles.css", "utf8");

describe("CareProspect public website", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("presents the approved evidence-led homepage structure", () => {
    render(<PublicSite />);
    expect(screen.getByRole("heading", { name: "Find new children’s homes before they appear on the register." })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Registration is the last signal, not the first" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Built for suppliers to children’s homes" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Signals in, one opportunity out" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "How we handle evidence" })).toBeInTheDocument();
    expect(screen.getByText("£149")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Sign in" }).length).toBeGreaterThan(0);
  });

  it("keeps the hero content-driven rather than viewport-height-driven", () => {
    const desktopHeroRule = styles.match(/\.cp-hero\s*\{([^}]*)\}/)?.[1] || "";
    expect(desktopHeroRule).not.toMatch(/(?:min-)?height\s*:[^;]*vh/);
    expect(desktopHeroRule).not.toMatch(/padding\s*:[^;]*vh/);
    expect(desktopHeroRule).toContain("padding: 80px 2rem");
    expect(styles).toContain("@media (max-width: 900px)");
  });

  it("submits a bounded access request and shows an in-page result", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => ({ status: "accepted" }),
    });
    render(<PublicSite />);
    await userEvent.type(screen.getByLabelText("Name"), "Alex Supplier");
    await userEvent.type(screen.getByLabelText("Company"), "Example Ltd");
    await userEvent.type(screen.getByLabelText("Work email"), "alex@example.test");
    await userEvent.selectOptions(screen.getByLabelText("Supplier category"), "SOFTWARE");
    await userEvent.click(screen.getByRole("button", { name: "Request access" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Your pilot request has been received");
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/public/access-requests"), expect.objectContaining({ method: "POST" })));
  });

  it("uses accessible native disclosure controls for the FAQ", async () => {
    render(<PublicSite />);
    const question = screen.getByText("What counts as an opportunity?");
    await userEvent.click(question);
    expect(question.closest("details")).toHaveAttribute("open");
  });

  it("provides honest public privacy and pilot terms pages", () => {
    const { rerender } = render(<PublicLegalPage page="privacy" />);
    expect(screen.getByRole("heading", { name: "Privacy" })).toBeInTheDocument();
    expect(screen.getByText(/does not reconstruct redacted/i)).toBeInTheDocument();
    rerender(<PublicLegalPage page="terms" />);
    expect(screen.getByRole("heading", { name: "Pilot terms" })).toBeInTheDocument();
    expect(screen.getByText(/not a guarantee/i)).toBeInTheDocument();
  });
});
