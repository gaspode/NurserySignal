import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useApi } from "./api.js";
import { displayAttributeName, useAuth } from "./auth.js";
import CustomerApp from "./CustomerApp.jsx";
import PublicSite, { CareProspectMark, PublicLegalPage } from "./PublicSite.jsx";

const PAGE_SIZE = 10;
const ACTIVE_VERTICALS = new Set(["ALL", "NURSERY", "CHILDRENS_HOME"]);

export function restoredVertical(storage = window.sessionStorage) {
  const stored = storage.getItem("signalhub.vertical") || "NURSERY";
  return ACTIVE_VERTICALS.has(stored) ? stored : "NURSERY";
}

export function verticalScopedPath(requestPath, vertical) {
  const [pathname, queryString] = requestPath.split("?", 2);
  if (!["/admin/signals", "/admin/opportunities", "/admin/opportunities/recalculate", "/admin/match-review", "/admin/organisations", "/admin/sources", "/admin/backtesting", "/admin/procurement-evaluation"].includes(pathname)) return requestPath;
  const params = new URLSearchParams(queryString || "");
  params.set("vertical", vertical);
  return `${pathname}?${params.toString()}`;
}

function useHashLocation() {
  const [location, setLocation] = useState(() => window.location.hash.slice(1) || "/");
  useEffect(() => {
    const onHashChange = () => setLocation(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return location;
}

function navigate(path) {
  window.location.hash = path;
}

function formatDate(value, includeTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    ...(includeTime ? { timeStyle: "short" } : {}),
  }).format(date);
}

function titleCase(value) {
  return String(value || "—")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function verticalName(value) {
  return { NURSERY: "NurserySignal", CHILDRENS_HOME: "CareProspect", DENTAL: "DentalSignal" }[value] || titleCase(value);
}

function isFalsePositive(signal) {
  const facts = signal?.extracted_facts || {};
  return facts.likely_false_positive === true || ["irrelevant-or-unclear", "horticultural-nursery"].includes(facts.classification);
}

function ErrorState({ message, onRetry }) {
  return (
    <div className="state-card error-state" role="alert">
      <strong>We couldn’t load this view.</strong>
      <p>{message || "Please try again."}</p>
      {onRetry && <button className="button secondary" onClick={onRetry}>Try again</button>}
    </div>
  );
}

function LoadingState({ label = "Loading" }) {
  return <div className="state-card"><span className="spinner" /> {label}…</div>;
}

export function RefreshButton({ busy = false, onClick }) {
  return <button type="button" className="button secondary refresh-button" onClick={onClick} disabled={busy} aria-label={busy ? "Refreshing data" : "Refresh data"}>
    <span aria-hidden="true">↻</span> {busy ? "Refreshing…" : "Refresh"}
  </button>;
}

function Badge({ children, tone = "neutral" }) {
  return <span className={`badge badge-${tone}`}>{children || "—"}</span>;
}

function reviewTone(status) {
  return { PENDING: "pending", APPROVED: "approved", REJECTED: "rejected" }[status] || "neutral";
}

export function LoginPage({ onLogin, authError = "", configured = true, passwordChallenge, onCompleteNewPassword, onCancelPasswordChallenge, customerBrand = false }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [attributeValues, setAttributeValues] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!passwordChallenge) return;
    const initialValues = {};
    for (const attribute of passwordChallenge.requiredAttributes || []) {
      initialValues[attribute] = passwordChallenge.userAttributes?.[attribute] || "";
    }
    setAttributeValues(initialValues);
    setNewPassword("");
    setConfirmPassword("");
    setError("");
  }, [passwordChallenge]);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onLogin(email.trim(), password);
    } catch (loginError) {
      setError(loginError.message || "Sign in failed.");
    } finally {
      setBusy(false);
    }
  }

  async function submitNewPassword(event) {
    event.preventDefault();
    setError("");
    if (newPassword.length < 8 || !/[a-z]/.test(newPassword) || !/[A-Z]/.test(newPassword) || !/[0-9]/.test(newPassword) || !/[^A-Za-z0-9]/.test(newPassword)) {
      setError("Use at least 8 characters including uppercase, lowercase, a number and a symbol.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("The passwords do not match.");
      return;
    }
    const missingAttribute = (passwordChallenge.requiredAttributes || []).find((attribute) => !attributeValues[attribute]?.trim());
    if (missingAttribute) {
      setError(`${displayAttributeName(missingAttribute)} is required.`);
      return;
    }
    setBusy(true);
    try {
      await onCompleteNewPassword(newPassword, attributeValues);
    } catch (challengeError) {
      setError(challengeError.message || "Unable to set the new password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className={`login-page${customerBrand ? " care-customer-login" : ""}`}>
      <section className="login-card">
        <div className={customerBrand ? "care-login-mark" : "brand-mark"}>{customerBrand ? <CareProspectMark /> : "SH"}</div>
        <p className="eyebrow">{customerBrand ? "Early intelligence on new children’s homes" : "Secure intelligence workspace"}</p>
        <h1>{customerBrand ? "CareProspect" : "SignalHub"}</h1>
        <p className="muted">{customerBrand ? "Sign in to your CareProspect account." : "Sign in to your secure intelligence workspace."}</p>
        {!configured ? (
          <ErrorState message="This deployment has no Cognito configuration." />
        ) : passwordChallenge ? (
          <form onSubmit={submitNewPassword} className="login-form">
            <h2>Set a new password</h2>
            <p className="muted">Your temporary password must be replaced before you can continue.</p>
            {(passwordChallenge.requiredAttributes || []).map((attribute) => (
              <label key={attribute}>{displayAttributeName(attribute)}<input autoComplete="off" type={attribute === "email" ? "email" : "text"} value={attributeValues[attribute] || ""} onChange={(event) => setAttributeValues((current) => ({ ...current, [attribute]: event.target.value }))} required /></label>
            ))}
            <label>New password<input autoComplete="new-password" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required aria-describedby="password-requirements" /></label>
            <p id="password-requirements" className="muted small-text">At least 8 characters, with uppercase, lowercase, a number/symbol.</p>
            <label>Confirm new password<input autoComplete="new-password" type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} required /></label>
            {(error || authError) && <p className="form-error" role="alert">{error || authError}</p>}
            <button className="button primary full-width" disabled={busy}>{busy ? "Setting password…" : "Set password"}</button>
            <button type="button" className="button ghost full-width" onClick={onCancelPasswordChallenge} disabled={busy}>Use a different account</button>
          </form>
        ) : (
          <form onSubmit={submit} className="login-form">
            <label>Email<input autoComplete="username" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
            <label>Password<input autoComplete="current-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
            {(error || authError) && <p className="form-error" role="alert">{error || authError}</p>}
            <button className="button primary full-width" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
          </form>
        )}
        <p className="login-footnote">{customerBrand ? "CareProspect customer access is provided by invitation." : "SignalHub is an internal administration service."}</p>
        {customerBrand && <button type="button" className="care-login-home" onClick={() => navigate("/")}>← Back to CareProspect</button>}
      </section>
    </main>
  );
}

function ConfirmationModal({ title, message, confirmLabel, danger = false, busy = false, error = "", onConfirm, onCancel }) {
  const confirmRef = useRef(null);
  useEffect(() => {
    confirmRef.current?.focus();
    const handleKeyDown = (event) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [busy, onCancel]);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && !busy && onCancel()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="confirmation-title" onMouseDown={(event) => event.stopPropagation()}>
        <h2 id="confirmation-title">{title}</h2>
        <p className="muted">{message}</p>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="modal-actions">
          <button className="button secondary" onClick={onCancel} disabled={busy}>Cancel</button>
          <button ref={confirmRef} className={`button ${danger ? "reject" : "approve"}`} onClick={onConfirm} disabled={busy}>{busy ? "Saving…" : confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

function AlertModal({ title, message, onClose }) {
  const okRef = useRef(null);
  useEffect(() => {
    okRef.current?.focus();
    const handleKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);
  return <div className="modal-backdrop" role="presentation">
    <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="alert-title" aria-describedby="alert-message" onMouseDown={(event) => event.stopPropagation()}>
      <h2 id="alert-title">{title}</h2>
      <p id="alert-message" className="muted">{message}</p>
      <div className="modal-actions"><button ref={okRef} className="button primary" onClick={onClose}>OK</button></div>
    </div>
  </div>;
}

function Shell({ user, onLogout, onNavigate, currentPath, vertical, onVerticalChange, children }) {
  const email = user?.getUsername?.() || "Signed-in staff";
  const route = currentPath.split("?")[0];
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => onNavigate("/")}><span className="brand-mark small">SH</span><span>SignalHub<small>NurserySignal</small></span></button>
        <p className="sidebar-label">Workspace</p>
        <nav aria-label="Primary navigation">
          <button className={currentPath === "/" ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/")}>Overview</button>
          <button className={route.startsWith("/inbox") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/inbox")}>Review Inbox</button>
          <button className={route.startsWith("/history") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/history")}>Reviewed Signals</button>
          <button className={route.startsWith("/opportunities") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/opportunities")}>Opportunities</button>
          <button className={route.startsWith("/unmatched") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/unmatched")}>Unmatched Signals</button>
          <button className={route.startsWith("/match-review") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/match-review")}>Match Review</button>
          <button className={route.startsWith("/sources") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/sources")}>Sources</button>
          <button className={route.startsWith("/procurement") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/procurement")}>Procurement</button>
          <button className={route.startsWith("/organisations") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/organisations")}>Organisations</button>
          <button className={route.startsWith("/customers") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/customers")}>Customers</button>
          <button className={route.startsWith("/backtesting") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/backtesting")}>Backtesting</button>
        </nav>
        <div className="sidebar-bottom">
          <span className="connection-dot" /> Production workspace
        </div>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <div className="topbar-context"><span className="mobile-brand">SignalHub</span><label className="vertical-selector">Vertical<select value={vertical} onChange={(event) => onVerticalChange(event.target.value)}><option value="ALL">All verticals</option><option value="NURSERY">NurserySignal</option><option value="CHILDRENS_HOME">CareProspect</option><option value="DENTAL" disabled>DentalSignal (coming later)</option></select></label></div>
          <div className="user-menu"><span className="user-email">{email}</span><button className="button ghost" onClick={onLogout}>Log out</button></div>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}

export function SourcesPage({ apiClient, selectedVertical = "NURSERY" }) {
  const [sources, setSources] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [running, setRunning] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState("");
  const [backfillBusy, setBackfillBusy] = useState(false);
  const load = async ({ initial = false } = {}) => {
    if (initial) setLoading(true); else setRefreshing(true);
    setError(null);
    try { setSources((await apiClient("/admin/sources")).items || []); }
    catch (loadError) { setError({ title: "Sources unavailable", message: loadError.message || "The source status could not be loaded." }); }
    finally { setLoading(false); setRefreshing(false); }
  };
  useEffect(() => { load({ initial: true }); }, [apiClient]);
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const result = (await apiClient("/admin/sources")).items || [];
        setSources(result);
        const source = result.find((item) => item.key === running.sourceKey);
        if (source?.last_run?.id === running.runId && source.last_run.status !== "RUNNING") {
          setRunning(null);
          setNotice(`${source.display_name} run ${source.last_run.status.toLowerCase()}.`);
        }
      } catch (pollError) { setError({ title: "Run status unavailable", message: pollError.message || "The latest run status could not be loaded." }); }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [running, apiClient]);
  async function run(source) {
    setRunning({ sourceKey: source.key, runId: null });
    setError(null); setNotice("");
    try {
      const result = await apiClient(`/admin/sources/${source.key}/run`, {
        method: "POST",
        body: JSON.stringify({ vertical: selectedVertical }),
      });
      setRunning({ sourceKey: source.key, runId: result.run_id });
      await load();
    } catch (runError) {
      setRunning(null);
      setError({
        title: "Source run could not be started",
        message: `${source.display_name} was not started. ${runError.message || "Please try again later."}`,
      });
    }
  }
  async function runCareBackfill() {
    setBackfillBusy(true); setError(null); setNotice("");
    try {
      const result = await apiClient("/admin/verticals/CHILDRENS_HOME/backfill", {
        method: "POST",
        body: JSON.stringify({ days: 60, limit: 25 }),
      });
      setNotice(`CareProspect backfill evaluated ${result.evaluated} targeted stored records (${result.planning_evaluated || 0} planning, ${result.recruitment_evaluated || 0} recruitment); ${result.relevant} relevant and ${result.accepted} newly accepted.`);
    } catch (backfillError) {
      setError({ title: "CareProspect backfill could not be completed", message: backfillError.message || "Please try again later." });
    } finally { setBackfillBusy(false); }
  }
  if (loading) return <LoadingState label="Loading sources" />;
  return <section>
    <div className="page-heading"><div><p className="eyebrow">Collection operations</p><h1>Sources</h1><p className="muted">Monitor configured collectors and start bounded manual runs.</p></div><RefreshButton busy={refreshing} onClick={load} /></div>
    {notice && <div className="notice" role="status">{notice}</div>}
    <div className="sources-grid">{sources.map((source) => {
      const active = running?.sourceKey === source.key;
      const summary = source.last_summary || {};
      return <article className="panel source-card" key={source.key}>
        <div className="source-card-heading"><div><p className="eyebrow">{source.key}</p><h2>{source.display_name}</h2><p className="muted">Provider: {source.provider}</p><p className="muted small-text">Verticals: {(source.supported_verticals || []).map(verticalName).join(" · ")}</p></div><Badge tone={source.schedule_state === "ENABLED" ? "approved" : "neutral"}>{source.schedule_state}</Badge></div>
        <dl className="source-meta"><div><dt>Schedule</dt><dd>{source.schedule_expression}</dd></div><div><dt>Last status</dt><dd>{source.last_status || "No persisted run yet"}</dd></div><div><dt>Last attempt</dt><dd>{formatDate(source.last_attempt_at, true)}</dd></div><div><dt>Last successful</dt><dd>{formatDate(source.last_success_at, true)}</dd></div></dl>
        {source.last_run && <><h3>Last result</h3><div className="source-counts"><span>Fetched <strong>{summary.records_fetched || 0}</strong></span>{summary.unique_jobs != null && <span>Unique <strong>{summary.unique_jobs}</strong></span>}<span>Matched <strong>{summary.candidates_matched || 0}</strong></span>{summary.nursery_matched != null && <span>NurserySignal <strong>{summary.nursery_matched}</strong></span>}{summary.care_matched != null && <span>CareProspect <strong>{summary.care_matched}</strong></span>}{summary.care_relevant_routine != null && <span>Care routine <strong>{summary.care_relevant_routine}</strong></span>}{summary.care_relevant_change != null && <span>Care change <strong>{summary.care_relevant_change}</strong></span>}{summary.urn_enriched != null && <span>URN enriched <strong>{summary.urn_enriched}</strong></span>}{summary.urn_enrichment_failed != null && <span>URN failed <strong>{summary.urn_enrichment_failed}</strong></span>}{summary.organisations_attempted != null && <span>Organisations <strong>{summary.organisations_attempted}</strong></span>}{summary.exact_or_strong != null && <span>Resolved <strong>{summary.exact_or_strong}</strong></span>}{summary.ambiguous != null && <span>Review <strong>{summary.ambiguous}</strong></span>}<span>Queued <strong>{summary.signals_queued || 0}</strong></span><span>Excluded <strong>{summary.excluded || 0}</strong></span><span>Duplicates <strong>{summary.duplicates || 0}</strong></span><span>Errors <strong>{summary.errors || 0}</strong></span></div><p className="muted small-text">{titleCase(source.last_run.invocation_source)} run</p></>}
        {source.last_error && <div className="notice error-state">{source.last_error.category}: {source.last_error.message}</div>}
        <button className="button primary" onClick={() => run(source)} disabled={Boolean(running)}>{active ? "Run in progress…" : "Run now"}</button>
        {source.recent_runs?.length > 0 && <details className="source-history"><summary>Recent runs ({source.recent_runs.length})</summary>{source.recent_runs.map((item) => <div className="source-history-row" key={item.id}><span>{formatDate(item.started_at, true)} · {titleCase(item.invocation_source)}</span><Badge tone={item.status === "SUCCESS" ? "approved" : item.status === "FAILED" ? "rejected" : "pending"}>{item.status}</Badge></div>)}</details>}
      </article>;
    })}</div>
    <article className="panel source-card">
      <div className="source-card-heading"><div><p className="eyebrow">CareProspect activation</p><h2>Stored-evidence backfill</h2><p className="muted">Re-evaluate up to 25 preserved Planning records and Care-targeted Recruitment records from the last 60 days. Providers are not called.</p></div><Badge>Bounded</Badge></div>
      <button className="button secondary" onClick={runCareBackfill} disabled={backfillBusy || Boolean(running)}>{backfillBusy ? "Evaluating…" : "Run CareProspect backfill"}</button>
    </article>
    {error && <AlertModal title={error.title} message={error.message} onClose={() => setError(null)} />}
  </section>;
}

export function ProcurementEvaluationPage({ apiClient }) {
  const [result, setResult] = useState({ items: [], counts: {}, total: 0 });
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const load = async ({ initial = false } = {}) => {
    if (initial) setLoading(true); else setRefreshing(true);
    setError("");
    try { setResult(await apiClient("/admin/procurement-evaluation?limit=100")); }
    catch (loadError) { setError(loadError.message || "Procurement evaluation could not be loaded."); }
    finally { setLoading(false); setRefreshing(false); }
  };
  useEffect(() => { load({ initial: true }); }, [apiClient]);
  if (loading) return <LoadingState label="Loading procurement evaluation" />;
  if (error) return <ErrorState message={error} onRetry={() => load({ initial: true })} />;
  return <section>
    <div className="page-heading"><div><p className="eyebrow">CareProspect shadow source</p><h1>Procurement evaluation</h1><p className="muted">Official Find a Tender and Contracts Finder evidence. Shadow-only: these records cannot create live opportunities.</p></div><RefreshButton busy={refreshing} onClick={load} /></div>
    <div className="source-counts">{Object.entries(result.counts || {}).map(([key, value]) => <span key={key}>{titleCase(key)} <strong>{value}</strong></span>)}<span>Total retained <strong>{result.total || 0}</strong></span></div>
    {(result.items || []).length === 0 ? <div className="state-card"><strong>No procurement evaluation records yet.</strong><p>Run the bounded Public procurement source from Sources.</p></div> : <div className="table-wrap"><table><thead><tr><th>Notice</th><th>Buyer</th><th>Stage</th><th>Assessment</th><th>Incremental context</th></tr></thead><tbody>{result.items.map((item) => { const metadata = item.metadata || {}; return <tr key={item.id}><td><strong>{item.title}</strong><span className="cell-subtitle">{formatDate(item.publication_date)} · {titleCase(metadata.procurement_platform)}</span>{item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">View official notice</a>}</td><td>{item.buyer || "Unknown"}<span className="cell-subtitle">{item.location || "Location not stated"}</span></td><td>{titleCase(metadata.notice_stage)}</td><td><Badge tone={metadata.strong_candidate ? "approved" : metadata.procurement_category === "UNCERTAIN" ? "pending" : "neutral"}>{titleCase(metadata.procurement_category)}</Badge><span className="cell-subtitle">Rule confidence {Math.round(Number(item.confidence || metadata.procurement_confidence || 0) * 100)}%</span><span className="cell-subtitle">{metadata.procurement_reason}</span></td><td><strong>{item.appears_incremental ? "No exact buyer evidence found" : "Related evidence exists"}</strong><span className="cell-subtitle">{item.related_signal_count || 0} planning/recruitment signals · {item.related_opportunity_count || 0} opportunities</span><span className="cell-subtitle">Operator {item.operator_known ? "named at award" : "not yet known"}</span></td></tr>; })}</tbody></table></div>}
  </section>;
}

function compactOffice(address = {}) {
  return [address.locality, address.region, address.postal_code, address.country].filter(Boolean).join(", ");
}

function CandidateReviewCard({ candidate, onChoose }) {
  const sic = (candidate.sic_descriptions || []).map((item) => item.description ? `${item.code} — ${item.description}` : item.code);
  const reasons = candidate.match_reasons || [];
  const cautions = candidate.match_cautions || [];
  const externalUrl = candidate.companies_house_url || (candidate.company_number ? `https://find-and-update.company-information.service.gov.uk/company/${candidate.company_number}` : null);
  return <article className="company-candidate">
    <div className="company-candidate-heading"><div><Badge tone={candidate.match_outcome === "STRONG" ? "approved" : "pending"}>{titleCase(candidate.match_outcome || "UNCERTAIN")}</Badge>{candidate.best_supported_match && <Badge tone="approved">Best supported match</Badge>}<h3>{candidate.company_name || "Unnamed company"}</h3><p className="muted">Company no. {candidate.company_number || "not supplied"}</p></div><Badge tone={candidate.company_status === "active" ? "approved" : "neutral"}>{titleCase(candidate.company_status || "Status unavailable")}</Badge></div>
    <dl className="candidate-facts">
      {candidate.date_of_creation && <><dt>Incorporated</dt><dd>{formatDate(candidate.date_of_creation)}</dd></>}
      {candidate.type && <><dt>Company type</dt><dd>{titleCase(candidate.type)}</dd></>}
      {compactOffice(candidate.registered_office_address) && <><dt>Registered office</dt><dd>{compactOffice(candidate.registered_office_address)}</dd></>}
      {sic.length > 0 && <><dt>SIC</dt><dd>{sic.join("; ")}</dd></>}
    </dl>
    <div className="match-explanation"><strong>Why suggested</strong>{reasons.length > 0 ? <ul>{reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : <p className="muted">No positive corroboration beyond the search result.</p>}{cautions.length > 0 && <><strong>Checks needed</strong><ul className="candidate-cautions">{cautions.map((reason) => <li key={reason}>{reason}</li>)}</ul></>}</div>
    <div className="candidate-actions"><button className="button approve" onClick={() => onChoose(candidate)}>Use this company</button>{externalUrl && <a className="button secondary" href={externalUrl} target="_blank" rel="noreferrer">View Companies House</a>}</div>
  </article>;
}

function OrganisationReview({ review, onChoose, onReject, onLookup, onEnrichOfsted }) {
  const context = review.source_context || {};
  const [companyNumber, setCompanyNumber] = useState("");
  const [manualCandidate, setManualCandidate] = useState(null);
  const [lookupBusy, setLookupBusy] = useState(false);
  const [lookupError, setLookupError] = useState("");
  const [enrichmentState, setEnrichmentState] = useState("");
  const candidates = manualCandidate
    ? [...(review.candidates || []).filter((item) => item.company_number !== manualCandidate.company_number), manualCandidate]
    : (review.candidates || []);
  async function lookupCompany(event) {
    event.preventDefault();
    setLookupBusy(true);
    setLookupError("");
    try {
      setManualCandidate(await onLookup(review, companyNumber));
      setCompanyNumber("");
    } catch (error) {
      const messages = {
        invalid_company_number: "Enter a valid eight-character UK Companies House number.",
        company_not_found: "No company was found for that Companies House number.",
        company_lookup_rate_limited: "Companies House is temporarily rate limited. Please try again shortly.",
        company_lookup_not_configured: "Manual Companies House lookup is not currently configured.",
      };
      setLookupError(messages[error.message] || "The company lookup could not be completed. Please try again.");
    } finally {
      setLookupBusy(false);
    }
  }
  return <article className="organisation-review">
    <div className="organisation-review-heading"><div><p className="eyebrow">Observed operator</p><h2>{context.observed_name || review.organisation_name}</h2><p className="muted">{review.reason}</p></div><div>{(context.verticals || []).map((vertical) => <Badge key={vertical}>{verticalName(vertical)}</Badge>)}</div></div>
    <div className="source-context"><h3>SignalHub source context</h3><div className="source-context-grid"><Field label="Source types" value={(context.source_types || []).map(titleCase).join(" · ")} /><Field label="Website/domain" value={context.website} /><Field label="Known aliases" value={(context.aliases || []).map((item) => item.alias).join(" · ")} /></div>
      {(context.signals || []).map((signal) => <div className="source-context-item" key={signal.id}><div><Badge>{titleCase(signal.source_type)}</Badge> <strong>{signal.title}</strong><span className="cell-subtitle">{[signal.organisation_name, signal.town || signal.local_authority, signal.postcode, signal.location].filter(Boolean).join(" · ")}</span></div><div className="source-context-links"><a href={`#/history/${signal.id}`}>View signal</a>{signal.source_url && <a href={signal.source_url} target="_blank" rel="noreferrer">View source evidence</a>}</div></div>)}
      {(context.opportunities || []).map((opportunity) => <div className="source-context-item" key={opportunity.id}><div><Badge>{verticalName(opportunity.vertical)}</Badge> <strong>{opportunity.name}</strong><span className="cell-subtitle">{[opportunity.town, opportunity.postcode].filter(Boolean).join(" · ")}</span></div><a href={`#/opportunities/${opportunity.id}`}>View opportunity</a></div>)}
      {(context.ofsted_evidence || []).length > 0 && <div className="ofsted-corroboration"><h4>Ofsted provider evidence</h4><p className="muted small-text"><strong>Provider registered office — not the children’s-home location.</strong> Used only to corroborate organisation identity.</p>{context.ofsted_evidence.map((evidence) => <article className="source-context-item" key={evidence.urn}><div><Badge>Ofsted</Badge> <strong>{evidence.registered_provider_name || `URN ${evidence.urn}`}</strong><span className="cell-subtitle">URN {evidence.urn} · {[evidence.provider_registered_locality, evidence.provider_registered_region, evidence.provider_registered_postcode].filter(Boolean).join(" · ") || evidence.local_authority || "Provider location unavailable"}</span>{evidence.provider_registered_address && <span className="cell-subtitle">Provider registered office: {evidence.provider_registered_address}</span>}</div><div className="source-context-links">{evidence.latest_report_url && <a href={evidence.latest_report_url} target="_blank" rel="noreferrer">View latest Ofsted report</a>}{evidence.provider_page_url && <a href={evidence.provider_page_url} target="_blank" rel="noreferrer">View Ofsted record</a>}</div></article>)}</div>}
      {(context.missing_ofsted_urns || []).length > 0 && <div className="ofsted-corroboration"><h4>Ofsted provider evidence not yet enriched</h4><p className="muted small-text">URN {(context.missing_ofsted_urns || []).join(", ")} can be enriched through the bounded official Ofsted collector. The review remains usable if enrichment is unavailable.</p><button className="button secondary" disabled={enrichmentState === "busy" || enrichmentState === "queued"} onClick={async () => { setEnrichmentState("busy"); try { const result = await onEnrichOfsted(review); setEnrichmentState(result.queued ? "queued" : "complete"); } catch { setEnrichmentState("error"); } }}>{enrichmentState === "busy" ? "Requesting…" : enrichmentState === "queued" ? "Enrichment queued" : "Enrich Ofsted provider evidence"}</button>{enrichmentState === "error" && <div className="inline-alert" role="alert">Ofsted enrichment could not be queued. The existing review is still available.</div>}</div>}
      {(context.signals || []).length === 0 && (context.opportunities || []).length === 0 && <p className="muted">No linked SignalHub evidence context is currently available.</p>}
    </div>
    <div className="candidate-grid">{candidates.map((candidate) => <CandidateReviewCard key={candidate.company_number} candidate={candidate} onChoose={(selected) => onChoose(review, selected)} />)}</div>
    <form className="manual-company-lookup" onSubmit={lookupCompany}>
      <div><strong>Company number not listed?</strong><label htmlFor={`company-number-${review.id}`}>Companies House number</label><input id={`company-number-${review.id}`} value={companyNumber} onChange={(event) => setCompanyNumber(event.target.value)} placeholder="e.g. 12345678" autoComplete="off" /></div>
      <button className="button secondary" type="submit" disabled={lookupBusy || !companyNumber.trim()}>{lookupBusy ? "Looking up…" : "Look up"}</button>
      {lookupError && <div className="inline-alert" role="alert">{lookupError}</div>}
    </form>
    <button className="button reject" onClick={() => onReject(review)}>None of these companies</button>
  </article>;
}

export function OrganisationsPage({ apiClient }) {
  const [items, setItems] = useState(null);
  const [reviews, setReviews] = useState([]);
  const [detail, setDetail] = useState(null);
  const [decision, setDecision] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const load = async () => { try { setError(""); const [organisations, pending] = await Promise.all([apiClient("/admin/organisations?limit=100"), apiClient("/admin/organisation-match-review?limit=25")]); setItems(organisations.items || []); setReviews(pending.items || []); } catch (loadError) { setError(loadError.message); } };
  useEffect(() => { load(); }, [apiClient]);
  async function openDetail(id) { if (!id) return; try { setDetail(await apiClient(`/admin/organisations/${id}`)); } catch (loadError) { setError(loadError.message); } }
  async function lookupCompany(review, companyNumber) { return apiClient(`/admin/organisation-match-review/${review.id}/lookup`, { method: "POST", body: JSON.stringify({ company_number: companyNumber }) }); }
  async function enrichOfsted(review) { return apiClient(`/admin/organisation-match-review/${review.id}/enrich-ofsted`, { method: "POST", body: JSON.stringify({}) }); }
  async function resolveReview() { if (!decision) return; setBusy(true); try { const companyNumber = decision.candidate?.company_number; await apiClient(`/admin/organisation-match-review/${decision.review.id}/${decision.action}`, { method: "POST", body: JSON.stringify(companyNumber ? { company_number: companyNumber } : {}) }); setNotice(decision.action === "confirm" ? `${decision.candidate.company_name} selected as the legal company.` : "Candidate companies rejected; the observed organisation and its evidence remain intact."); setDecision(null); await load(); } catch (reviewError) { setError(reviewError.message); } finally { setBusy(false); } }
  const reviewOperatorIds = new Set(reviews.map((review) => String(review.operator_id)));
  if (error) return <ErrorState message={error} onRetry={load} />;
  if (!items) return <LoadingState label="Loading organisations" />;
  return <section><div className="page-heading"><div><p className="eyebrow">Shared identity</p><h1>Organisations</h1><p className="muted">Companies House strengthens shared legal identity; signals and opportunities remain isolated by vertical and site.</p></div><RefreshButton onClick={load} /></div>{notice && <div className="notice" role="status">{notice}</div>}{reviews.length > 0 && <div className="panel organisation-review-panel"><h2>Organisation resolution review</h2><p className="muted">Compare the observed SignalHub evidence with official company profiles. The admin decision remains authoritative.</p>{reviews.map((review) => <OrganisationReview key={review.id} review={review} onChoose={(selectedReview, candidate) => setDecision({ review: selectedReview, candidate, action: "confirm" })} onReject={(selectedReview) => setDecision({ review: selectedReview, action: "reject" })} onLookup={lookupCompany} onEnrichOfsted={enrichOfsted} />)}</div>}{detail && <div className="panel"><div className="panel-heading"><h2>{detail.legal_name || detail.name}</h2><button className="button ghost" onClick={() => setDetail(null)}>Close</button></div><div className="detail-grid"><div><Field label="Company number" value={detail.companies_house_number} /><Field label="Status" value={titleCase(detail.company_status)} /><Field label="Incorporated" value={formatDate(detail.incorporation_date)} /><Field label="Company type" value={titleCase(detail.company_type)} /><Field label="Registered office" value={Object.values(detail.registered_office || {}).join(", ")} />{detail.companies_house_url && <a href={detail.companies_house_url} target="_blank" rel="noreferrer">View Companies House record</a>}</div><div><Field label="Aliases" value={(detail.aliases || []).map((item) => item.alias).join(" · ")} /><Field label="SIC codes" value={(detail.sic_codes || []).join(", ")} /><Field label="Resolution" value={titleCase(detail.resolution_outcome)} /></div></div>{(detail.ofsted_corroboration || []).length > 0 && <><h3>Ofsted corroboration</h3><p className="muted small-text">Provider-level identity evidence only. Registered offices are not children’s-home locations.</p>{detail.ofsted_corroboration.map((evidence) => <div className="source-context-item" key={evidence.urn}><div><Badge>Ofsted</Badge> <strong>{evidence.registered_provider_name || `URN ${evidence.urn}`}</strong><span className="cell-subtitle">URN {evidence.urn} · {[evidence.provider_registered_locality, evidence.provider_registered_region, evidence.provider_registered_postcode].filter(Boolean).join(" · ") || evidence.local_authority || "Provider location unavailable"}</span></div><div className="source-context-links">{evidence.latest_report_url && <a href={evidence.latest_report_url} target="_blank" rel="noreferrer">Latest report</a>}{evidence.provider_page_url && <a href={evidence.provider_page_url} target="_blank" rel="noreferrer">Ofsted record</a>}</div></div>)}</>}<h3>Linked opportunities</h3>{(detail.opportunities || []).map((item) => <p key={item.id}><Badge>{verticalName(item.vertical)}</Badge> {item.name} · {titleCase(item.lifecycle_stage)}</p>)}</div>}{items.length === 0 ? <div className="state-card"><strong>No organisations found.</strong></div> : <div className="table-wrap"><table><thead><tr><th>Organisation</th><th>Vertical</th><th>Resolution</th><th>Signals</th><th>Opportunities</th></tr></thead><tbody>{items.map((item) => { const needsReview = item.id && reviewOperatorIds.has(String(item.id)); const resolution = needsReview ? "Needs review" : item.companies_house_number ? "Resolved" : "Not resolved"; return <tr className={item.id ? "clickable-row" : ""} key={`${item.vertical}-${item.name}`} onClick={() => openDetail(item.id)}><td><strong>{item.legal_name || item.name}</strong>{item.legal_name && item.legal_name !== item.name && <span className="cell-subtitle">{item.name}</span>}</td><td><Badge>{verticalName(item.vertical)}</Badge></td><td><Badge tone={resolution === "Resolved" ? "approved" : resolution === "Needs review" ? "pending" : "neutral"}>{resolution}</Badge><span className="cell-subtitle">{item.companies_house_number || "No company selected"}</span></td><td>{item.signal_count}</td><td>{item.opportunity_count}</td></tr>; })}</tbody></table></div>}{decision && <ConfirmationModal title={decision.action === "confirm" ? "Use this legal company?" : "Reject all candidate companies?"} message={decision.action === "confirm" ? `${decision.candidate.company_name} (${decision.candidate.company_number}) will become the resolved legal identity. Original names remain as aliases.` : "This means none of these Companies House candidates represents the observed organisation. The organisation and its CareProspect evidence will not be rejected."} confirmLabel={decision.action === "confirm" ? "Use this company" : "None of these companies"} danger={decision.action === "reject"} busy={busy} onCancel={() => setDecision(null)} onConfirm={resolveReview} />}</section>;
}

export function Dashboard({ apiClient, onNavigate }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const load = async ({ initial = false } = {}) => {
    if (initial) setLoading(true);
    else setRefreshing(true);
    setError("");
    try {
      const [pending, approved, rejected, recent, opportunities, unmatched, matchReview] = await Promise.all([
        apiClient("/admin/signals?review_status=PENDING&limit=1"),
        apiClient("/admin/signals?review_status=APPROVED&limit=1"),
        apiClient("/admin/signals?review_status=REJECTED&limit=1"),
        apiClient("/admin/signals?limit=100"),
        apiClient("/admin/opportunities?limit=1&offset=0"),
        apiClient("/admin/signals?unmatched=true&limit=1&offset=0"),
        apiClient("/admin/match-review?limit=1&offset=0"),
      ]);
      const sourceCounts = recent.items.reduce((counts, item) => {
        counts[item.source_type] = (counts[item.source_type] || 0) + 1;
        return counts;
      }, {});
      const falsePositives = recent.items.filter(isFalsePositive).length;
      setData({ pending: pending.total, approved: approved.total, rejected: rejected.total, recent: recent.items.length, falsePositives, sourceCounts, opportunities: opportunities.total, unmatched: unmatched.total, matchReview: matchReview.total });
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      if (initial) setLoading(false);
      else setRefreshing(false);
    }
  };
  useEffect(() => { load({ initial: true }); }, [apiClient]);
  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">Operations overview</p><h1>Signal review desk</h1><p className="muted">Triage incoming signals and preserve the evidence trail across active verticals.</p></div><div className="page-actions"><RefreshButton busy={refreshing} onClick={() => load()} /><button className="button primary" onClick={() => onNavigate("/inbox")}>Review inbox</button></div></div>
      {loading && <LoadingState label="Loading overview" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {data && <>
        <div className="metric-grid">
          <Metric label="Pending review" value={data.pending} tone="amber" onClick={() => onNavigate("/inbox")} />
          <Metric label="Approved" value={data.approved} tone="green" onClick={() => onNavigate("/history?review_status=APPROVED")} />
          <Metric label="Rejected" value={data.rejected} tone="red" onClick={() => onNavigate("/history?review_status=REJECTED")} />
          <Metric label="Likely false positives" value={data.falsePositives} tone="slate" onClick={() => onNavigate("/history?review_status=REJECTED")} />
          <Metric label="Opportunities" value={data.opportunities} tone="green" onClick={() => onNavigate("/opportunities")} />
          <Metric label="Unmatched signals" value={data.unmatched} tone="amber" onClick={() => onNavigate("/unmatched")} />
          <Metric label="Match review" value={data.matchReview} tone="slate" onClick={() => onNavigate("/match-review")} />
        </div>
        <div className="dashboard-grid">
          <section className="panel"><div className="panel-heading"><h2>Recent signals</h2><button className="text-button" onClick={() => onNavigate("/history")}>View history</button></div><p className="large-number">{data.recent}</p><p className="muted">signals available in the latest review window</p></section>
          <section className="panel"><div className="panel-heading"><h2>By source type</h2></div>{Object.keys(data.sourceCounts).length === 0 ? <p className="muted">No signals yet.</p> : Object.entries(data.sourceCounts).map(([source, count]) => <div className="source-row" key={source}><span>{titleCase(source)}</span><strong>{count}</strong></div>)}</section>
        </div>
      </>}
    </section>
  );
}

function Metric({ label, value, tone, onClick }) {
  const content = <><span className={`metric-icon ${tone}`} /><span className="metric-label">{label}</span><strong>{value}</strong></>;
  return onClick ? <button className="metric-card clickable" onClick={onClick}>{content}</button> : <div className="metric-card">{content}</div>;
}

function initialFilters(initialQuery, mode) {
  const params = new URLSearchParams(initialQuery);
  return {
    review_status: mode === "history" ? params.get("review_status") || "REVIEWED" : mode === "unmatched" ? "" : "PENDING",
    source_type: params.get("source_type") || "",
    discovered_from: params.get("discovered_from") || "",
    discovered_to: params.get("discovered_to") || "",
    q: params.get("q") || "",
    include_excluded: params.get("include_excluded") || "",
    opportunity_decision: params.get("opportunity_decision") || "",
  };
}

function ReprocessTool({ apiClient, onComplete }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function reprocess() {
    setBusy(true);
    setError("");
    try {
      const summary = await apiClient("/admin/planning/reprocess", {
        method: "POST",
        body: JSON.stringify({ limit: 25, source_type: "planning" }),
      });
      setOpen(false);
      onComplete(`${summary.pending_updated} pending signal${summary.pending_updated === 1 ? "" : "s"} re-evaluated; ${summary.reviewed_preserved} reviewed record${summary.reviewed_preserved === 1 ? "" : "s"} preserved.`);
    } catch (reprocessError) {
      setError(reprocessError.message);
    } finally {
      setBusy(false);
    }
  }
  return <>
    <button className="button secondary" onClick={() => setOpen(true)}>Re-evaluate stored planning</button>
    {error && <span className="inline-error" role="alert">{error}</span>}
    {open && <ConfirmationModal title="Re-evaluate stored planning?" message="Up to 25 stored planning signals will be checked with the current rules. This does not call Plota or create new evidence or queue messages." confirmLabel="Re-evaluate" busy={busy} onCancel={() => setOpen(false)} onConfirm={reprocess} />}
  </>;
}

function OpportunityRecalculateTool({ apiClient, onComplete }) {
  const [open, setOpen] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function recalculate() {
    setBusy(true); setError("");
    try {
      const result = { selected: 0, created: 0, reused: 0, merged: 0, routine_only_demoted: 0 };
      for (let offset = 0; offset < 100; offset += 25) {
        const batch = await apiClient("/admin/opportunities/recalculate", { method: "POST", body: JSON.stringify({ limit: 25, offset }) });
        for (const key of Object.keys(result)) result[key] += Number(batch[key] || 0);
        if (Number(batch.selected || 0) < 25) break;
      }
      setOpen(false);
      onComplete(`Recalculated ${result.selected} signals; ${result.created} opportunities created, ${result.reused} opportunities reused, ${result.merged} merged, and ${result.routine_only_demoted} routine-only opportunities demoted.`);
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  const displayError = error === "admin_request_failed"
    ? "The recalculation could not be completed. Please try again."
    : error;
  return <><button className="button secondary" onClick={() => { setError(""); setOpen(true); }}>Recalculate opportunities</button>{open && <ConfirmationModal title="Recalculate opportunities?" message="Up to 100 stored planning and recruitment signals in the selected vertical will be re-evaluated in safe bounded batches. Review history and source evidence remain intact." confirmLabel="Recalculate" busy={busy} error={displayError} onCancel={() => setOpen(false)} onConfirm={recalculate} />}</>;
}

function SignalListPage({ apiClient, onNavigate, initialQuery = "", mode, showVertical = false }) {
  const [filters, setFilters] = useState(() => {
    return initialFilters(initialQuery, mode);
  });
  const [page, setPage] = useState(0);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(() => new URLSearchParams(initialQuery).get("notice") || "");
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [bulkAction, setBulkAction] = useState(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE });
    Object.entries(filters).forEach(([key, value]) => value && params.set(key, value));
    if (mode === "unmatched") params.set("unmatched", "true");
    return params.toString();
  }, [filters, mode, page]);
  const load = async ({ initial = false } = {}) => {
    if (initial) setLoading(true);
    else setRefreshing(true);
    setError("");
    try { setResult(await apiClient(`/admin/signals?${query}`)); } catch (loadError) { setError(loadError.message); } finally {
      if (initial) setLoading(false);
      else setRefreshing(false);
    }
  };
  useEffect(() => { load({ initial: true }); }, [query, apiClient]);
  useEffect(() => { setSelectedIds(new Set()); }, [query]);
  function updateFilter(name, value) { setPage(0); setFilters((current) => ({ ...current, [name]: value })); }
  function toggleSelected(signalId) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(signalId)) next.delete(signalId); else next.add(signalId);
      return next;
    });
  }
  function toggleAll(items) {
    setSelectedIds((current) => {
      const next = new Set(current);
      const allSelected = items.every((item) => next.has(item.id));
      items.forEach((item) => (allSelected ? next.delete(item.id) : next.add(item.id)));
      return next;
    });
  }
  async function reviewOne(signalId, action) {
    setError(""); setNotice("");
    try {
      await apiClient(`/admin/signals/${signalId}/${action}`, { method: "POST" });
      setSelectedIds((current) => { const next = new Set(current); next.delete(signalId); return next; });
      setNotice(`Signal ${action === "approve" ? "approved" : "rejected"} successfully.`);
      await load();
    } catch (reviewError) { setError(reviewError.message); }
  }
  async function reviewBulk() {
    setBulkBusy(true); setError(""); setNotice("");
    try {
      const action = bulkAction;
      const result = await apiClient("/admin/signals/bulk-review", {
        method: "POST",
        body: JSON.stringify({ action, signal_ids: [...selectedIds] }),
      });
      setBulkAction(null); setSelectedIds(new Set());
      setNotice(`${result.updated} signal${result.updated === 1 ? "" : "s"} ${action === "approve" ? "approved" : "rejected"}.`);
      await load();
    } catch (reviewError) { setError(reviewError.message); }
    finally { setBulkBusy(false); }
  }
  const totalPages = result ? Math.max(1, Math.ceil(result.total / PAGE_SIZE)) : 1;
  const inbox = mode === "inbox";
  const unmatched = mode === "unmatched";
  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">{inbox ? "Active queue" : unmatched ? "Needs grouping" : "Decision history"}</p><h1>{inbox ? "Review Inbox" : unmatched ? "Unmatched Signals" : "Reviewed Signals"}</h1><p className="muted">{inbox ? "Work through pending signals one at a time." : unmatched ? "Signals without an active opportunity relationship." : "Search and correct previous review decisions without changing evidence."}</p></div><div className="page-actions"><RefreshButton busy={refreshing} onClick={() => load()} />{inbox ? <button className="button secondary" onClick={() => onNavigate("/history")}>View reviewed signals</button> : !unmatched && <ReprocessTool apiClient={apiClient} onComplete={setNotice} />}<span className="result-count">{result?.total ?? "—"} total</span></div></div>
      {notice && <div className="notice" role="status">{notice}</div>}
      {inbox && selectedIds.size > 0 && <div className="bulk-toolbar" role="toolbar" aria-label="Bulk review actions"><strong>{selectedIds.size} selected</strong><button className="button approve" onClick={() => setBulkAction("approve")}>Approve selected</button><button className="button reject" onClick={() => setBulkAction("reject")}>Reject selected</button><button className="button ghost" onClick={() => setSelectedIds(new Set())}>Clear</button></div>}
      <div className="filter-bar" aria-label="Signal filters">
        {!inbox && !unmatched && <label>Status<select aria-label="Review status" value={filters.review_status} onChange={(event) => updateFilter("review_status", event.target.value)}><option value="REVIEWED">All reviewed</option><option value="APPROVED">Approved</option><option value="REJECTED">Rejected</option></select></label>}
        {!inbox && <label>Search<input aria-label={unmatched ? "Search unmatched signals" : "Search reviewed signals"} type="search" placeholder="Proposal, reference, council…" value={filters.q} onChange={(event) => updateFilter("q", event.target.value)} /></label>}
        {unmatched && <><label>Opportunity decision<select aria-label="Opportunity decision" value={filters.opportunity_decision} onChange={(event) => updateFilter("opportunity_decision", event.target.value)}><option value="">All actionable</option><option value="CREATE_OPPORTUNITY">Create opportunity</option><option value="SUPPORT_EXISTING_ONLY">Support existing only</option><option value="REVIEW">Needs review</option></select></label><label className="checkbox-filter"><input type="checkbox" checked={filters.include_excluded === "true"} onChange={(event) => updateFilter("include_excluded", event.target.checked ? "true" : "")} /> Include rejected/false positives</label></>}
        <label>Source<select aria-label="Source type" value={filters.source_type} onChange={(event) => updateFilter("source_type", event.target.value)}><option value="">All sources</option><option value="planning">Planning</option><option value="recruitment">Recruitment</option><option value="ofsted">Ofsted</option><option value="operator_announcement">Operator announcement</option><option value="local_news">Local news</option></select></label>
        <label>From<input aria-label="Discovered from" type="date" value={filters.discovered_from} onChange={(event) => updateFilter("discovered_from", event.target.value)} /></label>
        <label>To<input aria-label="Discovered to" type="date" value={filters.discovered_to} onChange={(event) => updateFilter("discovered_to", event.target.value)} /></label>
        <button className="button secondary filter-reset" onClick={() => { setPage(0); setFilters(initialFilters("", mode)); }}>Reset</button>
      </div>
      {loading && <LoadingState label="Loading signals" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {!loading && !error && result?.items.length === 0 && <div className="state-card"><strong>{inbox ? "Inbox clear" : unmatched ? "No unmatched signals" : "No reviewed signals match these filters."}</strong><p className="muted">{inbox ? "There are no pending signals waiting for review." : "Try changing the search or filters."}</p></div>}
      {!loading && !error && result?.items.length > 0 && <>
        <div className="table-wrap"><table><thead><tr>{inbox && <th><input type="checkbox" aria-label="Select all signals" checked={result.items.every((item) => selectedIds.has(item.id))} onChange={() => toggleAll(result.items)} /></th>}{showVertical && <th>Vertical</th>}<th>Discovered</th><th>Signal</th><th>Source</th><th>Location / operator</th><th>Candidate</th><th>Rule confidence</th><th>AI shadow</th><th>Review</th>{inbox && <th>⋯</th>}</tr></thead><tbody>{result.items.map((item) => <SignalRow key={item.id} item={item} inbox={inbox} showVertical={showVertical} selected={selectedIds.has(item.id)} onSelect={() => toggleSelected(item.id)} onReview={reviewOne} onClick={() => onNavigate(`/${inbox ? "inbox" : unmatched ? "unmatched" : "history"}/${item.id}`)} />)}</tbody></table></div>
        <div className="pagination"><span>Page {page + 1} of {totalPages}</span><div><button className="button secondary" disabled={page === 0} onClick={() => setPage((current) => current - 1)}>Previous</button><button className="button secondary" disabled={page + 1 >= totalPages} onClick={() => setPage((current) => current + 1)}>Next</button></div></div>
      </>}
      {bulkAction && <ConfirmationModal title={`${bulkAction === "approve" ? "Approve" : "Reject"} selected signals?`} message={`This will ${bulkAction} ${selectedIds.size} pending signal${selectedIds.size === 1 ? "" : "s"}.`} confirmLabel={bulkAction === "approve" ? "Approve selected" : "Reject selected"} danger={bulkAction === "reject"} busy={bulkBusy} onCancel={() => setBulkAction(null)} onConfirm={reviewBulk} />}
    </section>
  );
}

export function ReviewInboxPage(props) { return <SignalListPage {...props} mode="inbox" />; }
export function ReviewedSignalsPage(props) { return <SignalListPage {...props} mode="history" />; }
export function UnmatchedSignalsPage(props) { return <SignalListPage {...props} mode="unmatched" />; }
export function SignalsPage(props) { return <ReviewInboxPage {...props} />; }

export function MatchReviewPage({ apiClient, onNavigate, showVertical = false }) {
  const [result, setResult] = useState(null); const [error, setError] = useState(""); const [busy, setBusy] = useState("");
  const load = async () => { try { setResult(await apiClient("/admin/match-review?limit=50&offset=0")); } catch (e) { setError(e.message); } };
  useEffect(() => { load(); }, [apiClient]);
  async function resolve(id, action) { setBusy(id); try { await apiClient(`/admin/match-review/${id}/${action}`, { method: "POST" }); await load(); } catch (e) { setError(e.message); } finally { setBusy(""); } }
  if (error) return <ErrorState message={error} onRetry={load} />;
  return <section><div className="page-heading"><div><p className="eyebrow">Grouping decisions</p><h1>Match Review</h1><p className="muted">Review uncertain system suggestions before they join an opportunity.</p></div><RefreshButton onClick={load} /></div>{!result && <LoadingState label="Loading match review" />}{result?.items?.length === 0 && <div className="state-card"><strong>No uncertain matches.</strong><p className="muted">Conservative automatic matching has no suggestions waiting.</p></div>}{result?.items?.map((item) => <article className="panel match-review-card" key={item.id}><div>{showVertical && <><Badge>{verticalName(item.vertical)}</Badge>{" "}</>}<Badge>{titleCase(item.outcome)}</Badge><h2>{item.signal_title}</h2><p className="muted">Possible match: {item.opportunity_name}</p><p>{item.reason}</p><span className="cell-subtitle">{Math.round((item.confidence || 0) * 100)}% confidence · {formatDate(item.created_at)}</span></div><div className="review-actions"><button className="button approve" disabled={busy === item.id} onClick={() => resolve(item.id, "link")}>Link</button><button className="button reject" disabled={busy === item.id} onClick={() => resolve(item.id, "reject")}>Reject</button><button className="button ghost" onClick={() => onNavigate(`/history/${item.signal_id}`)}>View signal</button><button className="button ghost" onClick={() => onNavigate(`/opportunities/${item.opportunity_id}`)}>View opportunity</button></div></article>)}</section>;
}

export function OpportunitiesPage({ apiClient, onNavigate, showVertical = false }) {
  const [result, setResult] = useState(null); const [notice, setNotice] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const load = async () => { setLoading(true); setError(""); try { const params = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE }); if (search) params.set("q", search); setResult(await apiClient(`/admin/opportunities?${params}`)); } catch (e) { setError(e.message); } finally { setLoading(false); } };
  useEffect(() => { load(); }, [page, apiClient]);
  return <section><div className="page-heading"><div><p className="eyebrow">Commercial evidence</p><h1>Opportunities</h1><p className="muted">Facilities supported by one or more independent signals.</p></div><div className="page-actions"><RefreshButton busy={loading} onClick={load} /><OpportunityRecalculateTool apiClient={apiClient} onComplete={async (message) => { setNotice(message); await load(); }} /></div></div>{notice && <div className="notice" role="status">{notice}</div>}<div className="filter-bar"><label>Search<input aria-label="Search opportunities" type="search" value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => event.key === "Enter" && (setPage(0), load())} placeholder="Name or explanation…" /></label><button className="button secondary" onClick={() => { setSearch(""); setPage(0); }}>Reset</button></div>{error && <ErrorState message={error} onRetry={load} />}{loading && !result && <LoadingState label="Loading opportunities" />}{result?.items?.length === 0 && <div className="state-card"><strong>No opportunities yet.</strong><p className="muted">Opportunities appear after signals are enriched.</p></div>}{result?.items?.length > 0 && <><div className="table-wrap"><table><thead><tr>{showVertical && <th>Vertical</th>}<th>Opportunity</th><th>Change type</th><th>Lifecycle</th><th>Confidence</th><th>Linked signals</th><th>Why created</th></tr></thead><tbody>{result.items.map((item) => <tr className="clickable-row" key={item.id} onClick={() => onNavigate(`/opportunities/${item.id}`)}>{showVertical && <td><Badge>{verticalName(item.vertical)}</Badge></td>}<td><strong>{item.name}</strong><span className="cell-subtitle">{titleCase(item.event_type)}</span></td><td><Badge>{titleCase(item.change_type || "OTHER_CHANGE")}</Badge></td><td><Badge>{titleCase(item.lifecycle_stage)}</Badge></td><td>{Math.round((item.confidence || 0) * 100)}%</td><td>{item.signal_count}</td><td>{item.creation_reason || item.stage_reason || "—"}</td></tr>)}</tbody></table></div><div className="pagination"><span>Page {page + 1} of {Math.max(1, Math.ceil(result.total / PAGE_SIZE))}</span><div><button className="button secondary" disabled={page === 0} onClick={() => setPage((value) => value - 1)}>Previous</button><button className="button secondary" disabled={(page + 1) * PAGE_SIZE >= result.total} onClick={() => setPage((value) => value + 1)}>Next</button></div></div></>}</section>;
}

function CustomerPublicationEditor({ opportunity, apiClient, onUpdated }) {
  const [title, setTitle] = useState(opportunity.customer_title || "");
  const [summary, setSummary] = useState(opportunity.customer_summary || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function update(status) {
    setBusy(true); setError("");
    try {
      await apiClient(`/admin/opportunities/${opportunity.id}/publication`, {
        method: "POST",
        body: JSON.stringify({ status, customer_title: title, customer_summary: summary }),
      });
      await onUpdated();
    } catch (updateError) { setError(updateError.message || "Customer publication could not be updated."); }
    finally { setBusy(false); }
  }
  return <div className="panel"><div className="section-heading"><div><h2>CareProspect customer publication</h2><p className="muted">Only published CareProspect opportunities with approved, non-procurement evidence can enter the customer-safe API.</p></div><Badge tone={opportunity.publication_status === "PUBLISHED" ? "approved" : "neutral"}>{opportunity.publication_status}</Badge></div>{error && <p className="form-error" role="alert">{error}</p>}<div className="customer-publication-form"><label>Customer title<input value={title} maxLength={160} onChange={(event) => setTitle(event.target.value)} placeholder="Leave blank to use the safe generated title" /></label><label>Customer summary<textarea value={summary} maxLength={500} onChange={(event) => setSummary(event.target.value)} placeholder="Leave blank to use the evidence-based explanation" /></label></div><div className="review-actions"><button className="button primary" disabled={busy} onClick={() => update("PUBLISHED")}>{busy ? "Saving…" : opportunity.publication_status === "PUBLISHED" ? "Update publication" : "Publish to CareProspect"}</button>{opportunity.publication_status === "PUBLISHED" && <button className="button secondary" disabled={busy} onClick={() => update("WITHDRAWN")}>Withdraw</button>}</div></div>;
}

export function CustomersPage({ apiClient }) {
  const [accounts, setAccounts] = useState([]);
  const [accessRequests, setAccessRequests] = useState([]);
  const [readiness, setReadiness] = useState(null);
  const [form, setForm] = useState({ name: "", email: "", plan: "STARTER", allowed_regions: "", allowed_local_authorities: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(async () => {
    setError("");
    try {
      const [accountResult, readinessResult, accessResult] = await Promise.all([apiClient("/admin/customer-accounts"), apiClient("/admin/customer-readiness"), apiClient("/admin/access-requests")]);
      setAccounts(accountResult.items || []); setReadiness(readinessResult); setAccessRequests(accessResult.items || []);
    } catch (loadError) { setError(loadError.message || "Customer accounts could not be loaded."); }
  }, [apiClient]);
  useEffect(() => { load(); }, [load]);
  async function provision(event) {
    event.preventDefault(); setBusy(true); setError(""); setNotice("");
    try {
      const value = await apiClient("/admin/customer-accounts", { method: "POST", body: JSON.stringify({ ...form, allowed_regions: form.allowed_regions.split(",").map((value) => value.trim()).filter(Boolean), allowed_local_authorities: form.allowed_local_authorities.split(",").map((value) => value.trim()).filter(Boolean) }) });
      setNotice(`Invitation queued for ${value.owner_email} to join ${value.name}. Refresh shortly to see the account.`);
      setForm({ name: "", email: "", plan: "STARTER", allowed_regions: "", allowed_local_authorities: "" });
      await load();
    } catch (provisionError) { setError(provisionError.message || "Customer could not be provisioned."); }
    finally { setBusy(false); }
  }
  async function toggleAccount(account) {
    setBusy(true); setError("");
    try { await apiClient(`/admin/customer-accounts/${account.id}`, { method: "PATCH", body: JSON.stringify({ status: account.status === "SUSPENDED" ? "ACTIVE" : "SUSPENDED" }) }); await load(); }
    catch (updateError) { setError(updateError.message || "Customer status could not be updated."); }
    finally { setBusy(false); }
  }
  return <section><div className="page-heading"><div><p className="eyebrow">Commercial pilot</p><h1>CareProspect customers</h1><p className="muted">Invite named users, assign account-level plans and enforce customer geography.</p></div><RefreshButton busy={busy} onClick={load} /></div>{notice && <div className="notice" role="status">{notice}</div>}{error && <ErrorState message={error} />}<div className="metric-grid"><article className="metric-card"><span>Customer-ready</span><strong>{readiness?.customer_eligible ?? "—"}</strong><small>Published and evidence-approved</small></article><article className="metric-card"><span>Access requests</span><strong>{accessRequests.length}</strong><small>Public pilot enquiries</small></article><article className="metric-card"><span>Draft candidates</span><strong>{readiness?.draft_candidates ?? "—"}</strong><small>Require explicit publication</small></article><article className="metric-card"><span>Email delivery</span><strong>{readiness?.email_delivery_configured ? "Ready" : "Preview"}</strong><small>{readiness?.email_delivery_configured ? "Verified sender configured" : "Verified SES sender required"}</small></article></div><div className="dashboard-grid"><form className="panel login-form" onSubmit={provision}><h2>Invite pilot customer</h2><label>Organisation<input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label><label>Owner email<input required type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} /></label><label>Plan<select value={form.plan} onChange={(event) => setForm({ ...form, plan: event.target.value })}><option>STARTER</option><option>PRO</option><option>BUSINESS</option></select></label><label>Allowed regions<input value={form.allowed_regions} onChange={(event) => setForm({ ...form, allowed_regions: event.target.value })} placeholder="Comma-separated; required for Starter unless local authorities are set" /></label><label>Allowed local authorities<input value={form.allowed_local_authorities} onChange={(event) => setForm({ ...form, allowed_local_authorities: event.target.value })} /></label><button className="button primary" disabled={busy}>{busy ? "Inviting…" : "Create account and invite"}</button></form><div className="panel"><h2>Pilot accounts</h2>{accounts.length === 0 ? <p className="muted">No customer accounts have been provisioned.</p> : accounts.map((account) => <div className="customer-account-row" key={account.id}><div><strong>{account.name}</strong><span className="cell-subtitle">{account.plan} · {account.user_count} user{account.user_count === 1 ? "" : "s"} · {account.usage_events} usage events</span><span className="cell-subtitle">{[...(account.allowed_regions || []), ...(account.allowed_local_authorities || [])].join(", ") || "National coverage"}</span></div><div><Badge tone={account.status === "SUSPENDED" ? "rejected" : "approved"}>{account.status}</Badge> <button className="button ghost" disabled={busy} onClick={() => toggleAccount(account)}>{account.status === "SUSPENDED" ? "Reactivate" : "Suspend"}</button></div></div>)}</div></div><div className="panel"><h2>Public access requests</h2>{accessRequests.length === 0 ? <p className="muted">No public pilot requests yet.</p> : <div className="table-wrap"><table><thead><tr><th>Received</th><th>Contact</th><th>Company</th><th>Category</th><th>Message</th></tr></thead><tbody>{accessRequests.map((request) => <tr key={request.id}><td>{formatDate(request.created_at, true)}</td><td><strong>{request.name}</strong><span className="cell-subtitle">{request.work_email}</span></td><td>{request.company}</td><td>{titleCase(request.supplier_category)}</td><td>{request.message || "—"}</td></tr>)}</tbody></table></div>}</div></section>;
}

export function OpportunityDetail({ opportunityId, apiClient, onBack }) {
  const [opportunity, setOpportunity] = useState(null); const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [selected, setSelected] = useState(new Set()); const [mergeTarget, setMergeTarget] = useState(""); const [signalToLink, setSignalToLink] = useState(""); const [modal, setModal] = useState(null); const [busy, setBusy] = useState(false);
  const load = () => apiClient(`/admin/opportunities/${opportunityId}`).then(setOpportunity).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [opportunityId]);
  async function action(kind, body) { setBusy(true); try { await apiClient(`/admin/opportunities/${opportunityId}/${kind}`, { method: "POST", body: JSON.stringify(body || {}) }); setNotice("Opportunity relationship updated."); setModal(null); setMergeTarget(""); setSelected(new Set()); await load(); } catch (e) { setError(e.message); } finally { setBusy(false); } }
  if (error) return <ErrorState message={error} />; if (!opportunity) return <LoadingState label="Loading opportunity" />;
  const activeSignalCount = opportunity.signals.filter((signal) => signal.relationship_status === "ACTIVE").length;
  return <section><button className="back-link" onClick={onBack}>← Back to Opportunities</button><div className="page-heading"><div><p className="eyebrow">Opportunity evidence</p><h1>{opportunity.name}</h1><p className="muted">{titleCase(opportunity.change_type || "OTHER_CHANGE")} · {titleCase(opportunity.lifecycle_stage)} · {Math.round((opportunity.confidence || 0) * 100)}% opportunity confidence</p></div><Badge>{titleCase(opportunity.lifecycle_stage)}</Badge></div>{notice && <div className="notice" role="status">{notice}</div>}<div className="panel"><h2>Opportunity basis</h2><p>{opportunity.creation_reason || "Created from preserved signal evidence."}</p><p className="muted">{activeSignalCount} active linked signal{activeSignalCount === 1 ? "" : "s"} support this opportunity. Historical inactive relationships remain visible below.</p></div>{opportunity.vertical === "CHILDRENS_HOME" && <CustomerPublicationEditor opportunity={opportunity} apiClient={apiClient} onUpdated={load} />}<div className="panel"><h2>Evidence timeline</h2>{opportunity.signals.map((signal) => <article className="timeline-item" key={signal.id}><div><input type="checkbox" aria-label={`Select ${signal.title} for split`} checked={selected.has(signal.id)} onChange={() => setSelected((current) => { const next = new Set(current); next.has(signal.id) ? next.delete(signal.id) : next.add(signal.id); return next; })} /> <Badge>{titleCase(signal.source_type)}</Badge><strong>{signal.title}</strong><span className="cell-subtitle">{formatDate(signal.discovered_at)} · Rule confidence {signal.rule_confidence == null ? "—" : `${Math.round(signal.rule_confidence * 100)}%`} · {signal.relationship_created_by || "SYSTEM"} · {signal.relationship_status}</span></div><p className="muted">{signal.match_reason === "initial signal" ? "Initial signal established this opportunity." : signal.match_reason || signal.provenance?.reason || "Relationship provenance unavailable."}</p><button className="button ghost" onClick={() => window.location.hash = `/history/${signal.id}`}>Open signal</button><button className="button ghost" onClick={() => setModal({ kind: "unlink", signalId: signal.id })}>Unlink</button>{signal.ai_status === "SUCCEEDED" && <span className="cell-subtitle">AI shadow: {titleCase(signal.ai_recommendation)} · {Math.round(signal.ai_confidence * 100)}%</span>}</article>)}{(opportunity.organisation_evidence || []).map((evidence) => <article className="timeline-item" key={`${evidence.provider}-${evidence.external_id}-${evidence.retrieved_at}`}><div><Badge>Organisation enrichment</Badge><strong>{titleCase(evidence.provider)}</strong><span className="cell-subtitle">{formatDate(evidence.retrieved_at)} · {titleCase(evidence.resolution_outcome)}</span></div><p className="muted">{evidence.reason}</p></article>)}{selected.size > 0 && <button className="button secondary" onClick={() => setModal({ kind: "split" })}>Split selected signals</button>}</div><div className="panel"><h2>Manual correction</h2><p className="muted">Manual links are authoritative and preserve the original relationship history.</p><div className="inline-form"><input aria-label="Signal ID to link" placeholder="Signal ID to link" value={signalToLink} onChange={(event) => setSignalToLink(event.target.value)} /><button className="button secondary" disabled={!signalToLink} onClick={() => action("link", { signal_id: signalToLink })}>Link signal</button></div><div className="inline-form"><input aria-label="Target opportunity ID" placeholder="Target opportunity ID" value={mergeTarget} onChange={(event) => setMergeTarget(event.target.value)} /><button className="button secondary" disabled={!mergeTarget} onClick={() => setModal({ kind: "merge" })}>Merge into target</button></div></div><div className="panel"><h2>Latest stage explanation</h2><p>{opportunity.stage_reason || "No separate stage update explanation."}</p><pre>{JSON.stringify(opportunity.confidence_breakdown || {}, null, 2)}</pre></div>{modal?.kind === "unlink" && <ConfirmationModal title="Unlink signal?" message="The relationship will be marked rejected and preserved in the audit history." confirmLabel="Unlink" danger={busy} busy={busy} onCancel={() => setModal(null)} onConfirm={() => action("unlink", { signal_id: modal.signalId })} />}{modal?.kind === "merge" && <ConfirmationModal title="Merge opportunities?" message="The source opportunity and its active links will be preserved in history and attached to the target." confirmLabel="Merge" danger={busy} busy={busy} onCancel={() => setModal(null)} onConfirm={() => action("merge", { target_opportunity_id: mergeTarget })} />}{modal?.kind === "split" && <ConfirmationModal title="Split selected signals?" message="Selected links will be preserved and moved to a new opportunity." confirmLabel="Split" danger={busy} busy={busy} onCancel={() => setModal(null)} onConfirm={() => action("split", { signal_ids: [...selected] })} />}</section>;
}

function SignalRow({ item, onClick, inbox, showVertical, selected, onSelect, onReview }) {
  const council = item.metadata?.council;
  const recruitmentFacts = item.extracted_facts || {};
  const aiLabel = item.source_type === "ofsted"
    ? "Not applicable"
    : item.ai_status === "SUCCEEDED" && item.ai_recommendation
    ? `${titleCase(item.ai_recommendation)} · ${Math.round(item.ai_confidence * 100)}%`
    : item.ai_status === "FAILED" ? "Unavailable" : "—";
  return <tr className={isFalsePositive(item) ? "false-positive-row" : "clickable-row"} onClick={onClick} tabIndex="0" onKeyDown={(event) => event.key === "Enter" && onClick()}>
    {inbox && <td onClick={(event) => event.stopPropagation()}><input type="checkbox" aria-label={`Select ${item.title}`} checked={selected} onChange={onSelect} /></td>}{showVertical && <td><Badge>{verticalName(item.vertical)}</Badge></td>}<td className="nowrap">{formatDate(item.discovered_at)}</td><td><strong>{item.title}</strong><span className="cell-subtitle">{item.external_id}</span></td><td><Badge>{titleCase(item.source_type)}</Badge>{item.source_type === "recruitment" && <span className="cell-subtitle">{titleCase(recruitmentFacts.recruitment_relevance || "—")}</span>}</td><td>{council || item.organisation_hint || "—"}<span className="cell-subtitle">{item.location_hint || "—"}</span></td><td><strong>{titleCase(item.event_type)}</strong><span className="cell-subtitle">{item.source_type === "recruitment" ? `${titleCase(recruitmentFacts.recruitment_role_category)} · Change ${titleCase(recruitmentFacts.commercial_change_evidence)}` : titleCase(item.lifecycle_stage)}</span>{isFalsePositive(item) && <span className="false-label">Likely false positive</span>}</td><td>{item.confidence == null ? "—" : `${Math.round(item.confidence * 100)}%`}</td><td><span className="cell-subtitle">AI shadow</span>{aiLabel}</td><td><Badge tone={reviewTone(item.review_status)}>{item.review_status || "PROCESSING"}</Badge></td>{inbox && <td className="row-actions" onClick={(event) => event.stopPropagation()}><RowActions item={item} onClick={onClick} onReview={onReview} /></td>}
  </tr>;
}

export function RowActionMenu({ label, actions }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState(null);
  const triggerRef = useRef(null);
  const menuRef = useRef(null);
  const menuId = `row-menu-${useId()}`;

  const close = useCallback((returnFocus = true) => {
    setOpen(false);
    setPosition(null);
    if (returnFocus) triggerRef.current?.focus();
  }, []);

  const updatePosition = useCallback(() => {
    if (!triggerRef.current || !menuRef.current) return;
    const trigger = triggerRef.current.getBoundingClientRect();
    const menu = menuRef.current.getBoundingClientRect();
    const margin = 8;
    const gap = 6;
    const width = menu.width || 144;
    const height = menu.height || 116;
    const availableBelow = window.innerHeight - trigger.bottom - margin;
    const placeAbove = availableBelow < height + gap && trigger.top >= height + gap + margin;
    const top = placeAbove
      ? Math.max(margin, trigger.top - height - gap)
      : Math.min(trigger.bottom + gap, window.innerHeight - height - margin);
    const left = Math.min(
      Math.max(margin, trigger.right - width),
      Math.max(margin, window.innerWidth - width - margin),
    );
    setPosition({ top, left, placement: placeAbove ? "top" : "bottom" });
  }, []);

  useLayoutEffect(() => {
    if (!open) return undefined;
    updatePosition();
    menuRef.current?.querySelector('[role="menuitem"]')?.focus();

    const onPointerDown = (event) => {
      if (!triggerRef.current?.contains(event.target) && !menuRef.current?.contains(event.target)) close(false);
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    };
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [close, open, updatePosition]);

  function onMenuKeyDown(event) {
    const items = [...menuRef.current.querySelectorAll('[role="menuitem"]')];
    const current = items.indexOf(document.activeElement);
    let next = null;
    if (event.key === "ArrowDown") next = items[(current + 1) % items.length];
    if (event.key === "ArrowUp") next = items[(current - 1 + items.length) % items.length];
    if (event.key === "Home") next = items[0];
    if (event.key === "End") next = items.at(-1);
    if (next) {
      event.preventDefault();
      next.focus();
    }
  }

  const menu = open && createPortal(
    <div
      className="row-menu"
      id={menuId}
      role="menu"
      ref={menuRef}
      data-placement={position?.placement || "bottom"}
      style={{ position: "fixed", top: position?.top ?? -9999, left: position?.left ?? -9999 }}
      onKeyDown={onMenuKeyDown}
    >
      {actions.map((action) => <button type="button" role="menuitem" key={action.label} onClick={() => { close(false); action.onClick(); }}>{action.label}</button>)}
    </div>,
    document.body,
  );

  return <div className="row-actions-wrap">
    <button
      type="button"
      className="icon-button"
      ref={triggerRef}
      aria-label={label}
      aria-haspopup="menu"
      aria-controls={open ? menuId : undefined}
      aria-expanded={open}
      onClick={() => setOpen((value) => !value)}
      onKeyDown={(event) => {
        if (!open && event.key === "ArrowDown") {
          event.preventDefault();
          setOpen(true);
        }
      }}
    >⋯</button>
    {menu}
  </div>;
}

function RowActions({ item, onClick, onReview }) {
  return <RowActionMenu label={`Actions for ${item.title}`} actions={[
    { label: "Approve", onClick: () => onReview(item.id, "approve") },
    { label: "Reject", onClick: () => onReview(item.id, "reject") },
    { label: "View", onClick },
  ]} />;
}

export function SignalDetail({ signalId, apiClient, onBack, queueMode = false, unmatchedMode = false, initialNotice = "", onReviewed }) {
  const [signal, setSignal] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(initialNotice);
  const [busy, setBusy] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  const [opportunityBusy, setOpportunityBusy] = useState(false);
  const load = async () => { setLoading(true); setError(""); try { setSignal(await apiClient(`/admin/signals/${signalId}`)); } catch (loadError) { setError(loadError.message); } finally { setLoading(false); } };
  useEffect(() => { load(); }, [signalId]);
  async function review(action) {
    setBusy(true);
    setError("");
    try {
      await apiClient(`/admin/signals/${signalId}/${action}`, { method: "POST" });
      const message = `Signal ${action === "approve" ? "approved" : "rejected"} successfully.`;
      if (queueMode) {
        let nextId = null;
        try {
          const pending = await apiClient("/admin/signals?review_status=PENDING&limit=1&offset=0");
          nextId = pending.items?.[0]?.id || null;
        } catch (nextError) {
          setError("The decision was saved, but the next pending signal could not be loaded.");
        }
        onReviewed?.({ message, nextId });
      } else {
        setNotice(message);
        await load();
      }
    } catch (reviewError) {
      setError(reviewError.message);
    } finally {
      setBusy(false);
    }
  }
  async function openEvidence() {
    const tab = window.open("about:blank", "_blank");
    try { const result = await apiClient(`/admin/signals/${signalId}/evidence`); if (tab) tab.location = result.url; } catch (evidenceError) { tab?.close(); setError(evidenceError.message); }
  }
  async function runAiAssessment() {
    setAiBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await apiClient(`/admin/signals/${signalId}/ai-review`, { method: "POST" });
      setNotice(result.idempotent ? "Existing AI shadow assessment returned." : "AI shadow assessment completed.");
      await load();
    } catch (assessmentError) {
      setError(assessmentError.message);
    } finally {
      setAiBusy(false);
    }
  }
  async function createOpportunity() {
    setOpportunityBusy(true); setError("");
    try { const result = await apiClient(`/admin/signals/${signalId}/create-opportunity`, { method: "POST" }); setNotice(result.created ? "Opportunity created from signal." : "Signal is already linked to an opportunity."); } catch (e) { setError(e.message); } finally { setOpportunityBusy(false); }
  }
  if (loading) return <LoadingState label="Loading signal" />;
  if (error && !signal) return <ErrorState message={error} onRetry={load} />;
  const enrichment = signal?.enrichment;
  const falsePositive = isFalsePositive(enrichment);
  const aiSupported = ["planning", "recruitment"].includes(signal.source_type);
  const planning = signal.source_type === "planning" ? signal.metadata || {} : null;
  const ofsted = signal.source_type === "ofsted" ? signal.ofsted_enrichment || null : null;
  const latestDocument = signal.documents?.[signal.documents.length - 1];
  const reviewStatus = enrichment?.review_status;
  const correctionAction = reviewStatus === "APPROVED" ? "reject" : "approve";
  return <section>
    <button className="back-link" onClick={onBack}>← Back to {queueMode ? "Review Inbox" : "Reviewed Signals"}</button>
    <div className="page-heading detail-heading"><div><p className="eyebrow">{queueMode ? "Inbox review" : "Reviewed signal"}</p><h1>{signal.title}</h1><p className="muted">{signal.source_type} · {signal.external_id}</p></div><Badge tone={reviewTone(enrichment?.review_status)}>{enrichment?.review_status || "PROCESSING"}</Badge></div>
    {notice && <div className="notice toast" role="status">{notice}</div>}{error && <div className="notice toast error-state" role="alert">{error}</div>}
    {queueMode && reviewStatus === "PENDING" && <div className="review-action-bar"><div><strong>Ready for decision</strong><span className="muted">Approve or reject this candidate.</span></div><div className="review-actions"><button className="button approve" disabled={busy} onClick={() => review("approve")}>Approve</button><button className="button reject" disabled={busy} onClick={() => review("reject")}>Reject</button></div></div>}
    {unmatchedMode && <div className="review-action-bar"><div><strong>No active opportunity</strong><span className="muted">Create a conservative opportunity from this preserved signal.</span></div><button className="button primary" onClick={createOpportunity} disabled={opportunityBusy}>{opportunityBusy ? "Creating…" : "Create opportunity"}</button></div>}
    {!queueMode && ["APPROVED", "REJECTED"].includes(reviewStatus) && <div className="review-action-bar"><div><strong>Decision recorded</strong><span className="muted">This reviewed decision can be deliberately corrected.</span></div><div className="review-actions">{aiSupported && <button className="button secondary" onClick={runAiAssessment} disabled={aiBusy}>{aiBusy ? "Running AI…" : "Run AI shadow assessment"}</button>}<button className={`button ${correctionAction === "reject" ? "reject" : "approve"}`} disabled={busy} onClick={() => review(correctionAction)}>Change to {correctionAction === "approve" ? "Approved" : "Rejected"}</button></div></div>}
    <div className="detail-grid">
      <DetailPanel title="Raw signal"><Field label="Source type" value={titleCase(signal.source_type)} /><Field label="Source URL" value={<a href={signal.source_url} target="_blank" rel="noreferrer">{signal.source_url}</a>} /><Field label="External ID" value={signal.external_id} /><Field label="Discovered" value={formatDate(signal.discovered_at, true)} /><Field label="Title" value={signal.title} /><Field label="Raw text" value={<p className="raw-text">{signal.raw_text}</p>} /><Field label="Organisation hint" value={signal.organisation_hint} /><Field label="Location hint" value={signal.location_hint} /><Field label="Metadata" value={<pre>{JSON.stringify(signal.metadata || {}, null, 2)}</pre>} /></DetailPanel>
      {planning && <DetailPanel title="Planning application"><Field label="Provider reference" value={planning.provider_application_id} /><Field label="Council" value={planning.council} /><Field label="Application date" value={planning.application_date} /><Field label="Planning status" value={planning.planning_status} /><Field label="Decision" value={planning.decision} /><Field label="Postcode" value={planning.postcode} /><Field label="Coordinates" value={planning.latitude == null ? null : `${planning.latitude}, ${planning.longitude}`} /><Field label="Tracked revisions" value={signal.planning_revisions?.length || 0} /></DetailPanel>}
      {signal.source_type === "ofsted" && <DetailPanel title="Ofsted regulatory evidence">{ofsted ? <><Field label="URN" value={ofsted.urn} /><Field label="Registered provider" value={ofsted.registered_provider_name} /><Field label="Provision type" value={ofsted.provision_type} /><Field label="Registration date" value={formatDate(ofsted.registration_date)} /><Field label="Local authority" value={ofsted.local_authority} /><Field label="Provider location" value={[ofsted.provider_registered_locality, ofsted.provider_registered_region, ofsted.provider_registered_postcode].filter(Boolean).join(" · ")} /><Field label="Provider registered office (not home/site)" value={ofsted.provider_registered_address} /><Field label="Latest inspection" value={formatDate(ofsted.latest_report_date)} /><Field label="Report published" value={formatDate(ofsted.latest_report_publication_date)} />{ofsted.latest_report_url && <a href={ofsted.latest_report_url} target="_blank" rel="noreferrer">View latest Ofsted report</a>}{ofsted.provider_page_url && <a href={ofsted.provider_page_url} target="_blank" rel="noreferrer">View Ofsted record</a>}<p className="muted small-text">Ofsted suppresses the home name and address. Provider-level location is used only to corroborate organisation identity.</p></> : <p className="muted">URN-specific enrichment has not been collected yet. The annual-register evidence remains available.</p>}</DetailPanel>}
      <DetailPanel title="Deterministic assessment">{enrichment ? <><Field label="Event type" value={titleCase(enrichment.event_type)} /><Field label={signal.vertical === "CHILDRENS_HOME" ? "Home / site" : "Nursery name"} value={enrichment.nursery_name} /><Field label="Operator" value={enrichment.operator_name} /><Field label="Address" value={enrichment.address} /><Field label="Expected opening" value={enrichment.expected_opening_date} /><Field label="Capacity" value={enrichment.capacity} /><Field label="Lifecycle stage" value={<Badge>{titleCase(enrichment.lifecycle_stage)}</Badge>} /><Field label="Rule confidence" value={enrichment.confidence == null ? "—" : `${Math.round(enrichment.confidence * 100)}%`} />{signal.source_type === "recruitment" && <><Field label="Role" value={titleCase(enrichment.extracted_facts?.care_role_category || enrichment.extracted_facts?.recruitment_role_category)} /><Field label="Setting" value={titleCase(enrichment.extracted_facts?.care_setting_category || enrichment.extracted_facts?.recruitment_setting_category)} /><Field label="Recruitment relevance" value={titleCase(enrichment.extracted_facts?.recruitment_relevance)} /><Field label="Change evidence" value={titleCase(enrichment.extracted_facts?.commercial_change_evidence)} /></>}{signal.vertical === "CHILDRENS_HOME" && <><Field label="Opportunity action" value={titleCase(enrichment.extracted_facts?.opportunity_creation_decision)} /><Field label="Change type" value={titleCase(enrichment.extracted_facts?.opportunity_change_type)} /><Field label="Location handling" value="Exact location restricted to internal SignalHub" /></>}{falsePositive && <div className="false-positive-callout">Likely false positive · {enrichment.extracted_facts?.classification}</div>}<Field label="Extracted facts" value={<pre>{JSON.stringify(enrichment.extracted_facts || {}, null, 2)}</pre>} /><Field label="Evidence used" value={<pre>{JSON.stringify(enrichment.evidence || {}, null, 2)}</pre>} /></> : <p className="muted">Enrichment is still processing.</p>}</DetailPanel>
      <DetailPanel title="AI shadow assessment"><p className="muted small-text">Advisory only — human review remains authoritative.</p>{!aiSupported ? <p className="muted">Not applicable to Ofsted regulatory evidence. Regulatory status is preserved as source evidence and is not assessed using planning or recruitment prompts.</p> : signal.ai_reviews?.[0]?.status === "SUCCEEDED" ? <><Field label="AI assessment" value={titleCase(signal.ai_reviews[0].recommendation)} /><Field label="AI confidence" value={`${Math.round(signal.ai_reviews[0].confidence * 100)}%`} />{signal.source_type === "planning" ? <Field label="Planning relevance" value={titleCase(signal.ai_reviews[0].planning_relevance)} /> : <Field label="Recruitment relevance" value={titleCase(signal.ai_reviews[0].recruitment_relevance)} />}<Field label="Change evidence" value={titleCase(signal.ai_reviews[0].commercial_change_evidence)} /><Field label="Reason" value={signal.ai_reviews[0].reason} /><Field label="Model" value={signal.ai_reviews[0].model_id} /><Field label="Prompt version" value={signal.ai_reviews[0].prompt_version} /><Field label="Evaluated" value={formatDate(signal.ai_reviews[0].evaluated_at, true)} /></> : signal.ai_reviews?.[0]?.status === "FAILED" ? <p className="muted">AI assessment unavailable ({titleCase(signal.ai_reviews[0].failure_category)}). Human review is unaffected.</p> : <p className="muted">No AI shadow assessment available.</p>}{aiSupported && (queueMode || reviewStatus === "PENDING") && <button className="button secondary" onClick={runAiAssessment} disabled={aiBusy}>{aiBusy ? "Running AI assessment…" : signal.ai_reviews?.length ? "Re-run AI assessment" : "Run AI assessment"}</button>}</DetailPanel>
      <DetailPanel title="Evidence & provenance"><Field label="First seen" value={formatDate(signal.created_at, true)} /><Field label="Latest update" value={formatDate(signal.planning_revisions?.[0]?.observed_at || enrichment?.updated_at, true)} /><Field label="Evidence key" value={<code>{latestDocument?.s3_key || "—"}</code>} /><Field label="Evidence checksum" value={<code>{latestDocument?.sha256 || "—"}</code>} /><button className="button secondary" onClick={openEvidence} disabled={!signal.documents?.length}>View preserved JSON</button><p className="muted small-text">Access uses a short-lived authenticated download URL. The evidence bucket remains private.</p></DetailPanel>
      <DetailPanel title="Review"><Field label="Current state" value={<Badge tone={reviewTone(enrichment?.review_status)}>{enrichment?.review_status || "PROCESSING"}</Badge>} /><Field label="Reviewer" value={enrichment?.reviewed_by} /><Field label="Reviewed at" value={formatDate(enrichment?.reviewed_at, true)} /></DetailPanel>
    </div>
  </section>;
}

function DetailPanel({ title, children }) { return <section className="panel detail-panel"><h2>{title}</h2>{children}</section>; }
function Field({ label, value }) { return <div className="field"><dt>{label}</dt><dd>{value || "—"}</dd></div>; }

function metricPercent(value) {
  return value == null ? "Not measurable" : `${Math.round(Number(value) * 100)}%`;
}

export function BacktestingPage({ apiClient, selectedVertical = "CHILDRENS_HOME" }) {
  const benchmarkVertical = selectedVertical === "NURSERY" ? "NURSERY" : "CHILDRENS_HOME";
  const [summary, setSummary] = useState(null);
  const [detail, setDetail] = useState(null);
  const [comparison, setComparison] = useState(null);
  const [sensitivity, setSensitivity] = useState(null);
  const [recruitmentShadow, setRecruitmentShadow] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [asOf, setAsOf] = useState(() => new Date().toISOString().slice(0, 10));
  const load = async () => {
    setError("");
    try {
      const value = await apiClient("/admin/backtesting");
      setSummary(value);
      const canonicalRuns = (value.runs || []).filter((item) => !item.parameters || item.parameters.lookback_days === 365);
      if (canonicalRuns[0]?.id) setDetail(await apiClient(`/admin/backtesting/runs/${canonicalRuns[0].id}`));
      else setDetail(null);
      if (canonicalRuns[1]?.id) setComparison(await apiClient(`/admin/backtesting/compare?left=${canonicalRuns[1].id}&right=${canonicalRuns[0].id}`));
      else setComparison(null);
      if (benchmarkVertical === "CHILDRENS_HOME") setRecruitmentShadow(await apiClient("/admin/backtesting/recruitment-shadow?limit=100"));
      else setRecruitmentShadow(null);
    } catch (loadError) { setError(loadError.message || "Backtesting data could not be loaded."); }
  };
  useEffect(() => { load(); }, [apiClient]);

  async function seed() {
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await apiClient("/admin/backtesting/seed", {
        method: "POST",
        body: JSON.stringify({ vertical: "CHILDRENS_HOME", benchmark_version: "care-ofsted-v1", limit: 30 }),
      });
      setNotice(`Benchmark ready: ${value.inserted} cases added and ${value.existing} already present.`);
      await load();
    } catch (seedError) { setError(seedError.message || "Benchmark cases could not be seeded."); }
    finally { setBusy(false); }
  }

  async function run() {
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await apiClient("/admin/backtesting/run", {
        method: "POST",
        body: JSON.stringify({
          vertical: benchmarkVertical,
          benchmark_version: benchmarkVertical === "CHILDRENS_HOME" ? "care-ofsted-v1" : "nursery-outcomes-v1",
          as_of: `${asOf}T23:59:59Z`,
          lookback_days: 365,
          max_cases: 30,
          max_signals: 500,
        }),
      });
      setNotice(value.idempotent ? "Identical bounded run reused reproducibly." : "Bounded historical replay completed.");
      await load();
      setDetail(value);
    } catch (runError) { setError(runError.message || "Backtest could not be completed."); }
    finally { setBusy(false); }
  }

  async function importResearch() {
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await apiClient("/admin/backtesting/research/import", { method: "POST" });
      const corpus = value.summary || {};
      setNotice(value.idempotent
        ? "The reviewed historical corpus is already imported."
        : `Historical corpus imported: ${corpus.accepted_planning_items || 0} planning and ${corpus.accepted_recruitment_items || 0} recruitment items.`);
      await load();
    } catch (importError) { setError(importError.message || "Historical research could not be imported."); }
    finally { setBusy(false); }
  }

  async function runSensitivity() {
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await apiClient("/admin/backtesting/sensitivity", {
        method: "POST",
        body: JSON.stringify({
          vertical: "CHILDRENS_HOME",
          benchmark_version: "care-ofsted-v1",
          as_of: `${asOf}T23:59:59Z`,
          max_cases: 30,
          max_signals: 500,
        }),
      });
      setSensitivity(value);
      setNotice("Bounded 365/450/540-day sensitivity comparison completed.");
      await load();
    } catch (sensitivityError) { setError(sensitivityError.message || "Sensitivity comparison could not be completed."); }
    finally { setBusy(false); }
  }

  const metrics = detail?.metrics || summary?.runs?.[0]?.metrics || {};
  const contribution = detail?.source_contribution || summary?.runs?.[0]?.source_contribution || {};
  const research = summary?.historical_research;
  const researchMetrics = research?.summary || {};
  return <section>
    <div className="page-heading"><div><p className="eyebrow">Evaluation</p><h1>Historical Backtesting</h1><p className="muted">CareProspect-first, point-in-time replay. Benchmark truth is kept separate from historical inputs.</p></div><RefreshButton busy={busy} onClick={load} /></div>
    {notice && <div className="notice" role="status">{notice}</div>}
    {error && <ErrorState message={error} />}
    <div className="panel backtest-controls"><div><strong>Bounded replay</strong><p className="muted">Ofsted registration defines the outcome and is excluded from pre-registration input.</p></div><label>As of<input type="date" aria-label="Backtest as of" value={asOf} onChange={(event) => setAsOf(event.target.value)} /></label>{benchmarkVertical === "CHILDRENS_HOME" && <button className="button secondary" disabled={busy} onClick={seed}>Seed verified outcomes</button>}{benchmarkVertical === "CHILDRENS_HOME" && <button className="button secondary" disabled={busy} onClick={importResearch}>Import researched corpus</button>}{benchmarkVertical === "CHILDRENS_HOME" && <button className="button secondary" disabled={busy} onClick={runSensitivity}>Compare lookbacks</button>}<button className="button primary" disabled={busy} onClick={run}>{busy ? "Running…" : "Run benchmark"}</button></div>
    {!summary && !error && <LoadingState label="Loading benchmark" />}
    {research && <div className="panel"><div className="section-heading"><div><h2>Historical corpus</h2><p className="muted">{research.corpus_version} · official, date-verifiable evidence only</p></div><Badge>{researchMetrics.benchmark_cases || 0} researched</Badge></div><div className="source-counts"><span>Planning cases <strong>{researchMetrics.cases_with_planning || 0}</strong></span><span>Recruitment cases <strong>{researchMetrics.cases_with_recruitment || 0}</strong></span><span>Both <strong>{researchMetrics.cases_with_both || 0}</strong></span><span>Neither <strong>{researchMetrics.cases_with_neither || 0}</strong></span><span>Rejected candidates <strong>{researchMetrics.candidate_items_rejected || 0}</strong></span></div><div className="table-wrap"><table><thead><tr><th>Outcome</th><th>Provider</th><th>Planning</th><th>Recruitment</th><th>Research result</th></tr></thead><tbody>{(research.cases || []).map((item) => <tr key={item.benchmark_case_id}><td>{formatDate(item.outcome_date)}</td><td>{item.known_operator}</td><td>{item.eligible_planning || 0}</td><td>{item.eligible_recruitment || 0}</td><td>{item.records_accepted ? <Badge tone="approved">Replay evidence</Badge> : <Badge>Excluded</Badge>}<span className="cell-subtitle">{titleCase(item.not_found_reason) || item.notes || "No eligible evidence"}</span></td></tr>)}</tbody></table></div></div>}
    <div className="metric-grid">
      <article className="metric-card"><span>Usable cases</span><strong>{metrics.cases_usable ?? "—"}</strong><small>{metrics.cases_excluded ?? 0} excluded honestly</small></article>
      <article className="metric-card"><span>Recall</span><strong>{metricPercent(metrics.recall)}</strong><small>{metrics.detected_cases ?? 0} detected</small></article>
      <article className="metric-card"><span>Precision</span><strong>{metricPercent(metrics.precision)}</strong><small>{metrics.unlabelled_opportunities ?? 0} unlabelled, not assumed false</small></article>
      <article className="metric-card"><span>Median lead time</span><strong>{metrics.lead_time_days?.median == null ? "—" : `${metrics.lead_time_days.median} days`}</strong><small>p25 {metrics.lead_time_days?.p25 ?? "—"} · p75 {metrics.lead_time_days?.p75 ?? "—"}</small></article>
      <article className="metric-card"><span>Organisation accuracy</span><strong>{metricPercent(metrics.organisation_accuracy)}</strong><small>Operator identity measured separately</small></article>
      <article className="metric-card"><span>Site accuracy</span><strong>{metricPercent(metrics.site_accuracy)}</strong><small>No inferred redacted locations</small></article>
    </div>
    {recruitmentShadow && <div className="panel"><div className="section-heading"><div><h2>Current recruitment policy preview</h2><p className="muted">Read-only evaluation of the latest {recruitmentShadow.evaluated} stored CareProspect recruitment records.</p></div><Badge>{recruitmentShadow.changed_count} changed</Badge></div><div className="source-counts"><span>Change <strong>{recruitmentShadow.counts?.RELEVANT_CHANGE || 0}</strong></span><span>Routine <strong>{recruitmentShadow.counts?.RELEVANT_ROUTINE || 0}</strong></span><span>Uncertain <strong>{recruitmentShadow.counts?.UNCERTAIN || 0}</strong></span><span>Irrelevant <strong>{recruitmentShadow.counts?.IRRELEVANT || 0}</strong></span></div>{recruitmentShadow.changed?.length > 0 && <div className="table-wrap"><table><thead><tr><th>Vacancy</th><th>Employer</th><th>Previous</th><th>Preview</th><th>Evidence</th></tr></thead><tbody>{recruitmentShadow.changed.map((item) => <tr key={item.signal_id}><td>{item.title}<span className="cell-subtitle">{item.location || "—"}</span></td><td>{item.employer || "—"}</td><td>{titleCase(item.previous_relevance)}</td><td><Badge tone={item.new_relevance === "RELEVANT_CHANGE" ? "approved" : "pending"}>{titleCase(item.new_relevance)}</Badge></td><td>{(item.change_terms || []).map(titleCase).join(", ") || "Role/setting context"}</td></tr>)}</tbody></table></div>}</div>}
    {sensitivity?.runs?.length > 0 && <div className="panel"><h2>Lookback sensitivity</h2><p className="muted">Read-only comparison; the canonical benchmark remains 365 days.</p><div className="table-wrap"><table><thead><tr><th>Window</th><th>Usable</th><th>Detected</th><th>Recall</th><th>Median lead</th></tr></thead><tbody>{sensitivity.runs.map((item) => <tr key={item.lookback_days}><td>{item.lookback_days} days</td><td>{item.metrics?.cases_usable ?? "—"}</td><td>{item.metrics?.detected_cases ?? "—"}</td><td>{metricPercent(item.metrics?.recall)}</td><td>{item.metrics?.lead_time_days?.median == null ? "—" : `${item.metrics.lead_time_days.median} days`}</td></tr>)}</tbody></table></div></div>}
    {detail && <>
      <div className="dashboard-grid">
        <div className="panel"><h2>Source contribution</h2>{["planning", "recruitment"].map((source) => <div className="field" key={source}><dt>{titleCase(source)}</dt><dd>{contribution[source]?.first_discoveries ?? 0} first · {contribution[source]?.corroborations ?? 0} corroborations · {contribution[source]?.missed ?? 0} missed</dd></div>)}<div className="field"><dt>Combined</dt><dd>{contribution.combined?.found_by_either ?? 0} found · {contribution.combined?.neither_detected ?? 0} neither source</dd></div></div>
        <div className="panel"><h2>Run provenance</h2><Field label="Benchmark" value={detail.benchmark_version} /><Field label="Corpus" value={detail.corpus_version} /><Field label="Engine" value={detail.engine_version} /><Field label="Status" value={<Badge>{detail.status}</Badge>} /><Field label="As of" value={formatDate(detail.as_of, true)} /><Field label="Review burden" value={`${metrics.reviews_per_genuine_opportunity ?? "—"} per genuine opportunity`} /></div>
      </div>
      {comparison && <div className="panel"><h2>Latest run comparison</h2><p className="muted">Measured deltas only; positive does not automatically mean better.</p><div className="source-counts"><span>Recall <strong>{comparison.delta?.recall ?? "—"}</strong></span><span>Precision <strong>{comparison.delta?.precision ?? "—"}</strong></span><span>Median lead time <strong>{comparison.delta?.median_lead_time_days ?? "—"} days</strong></span><span>Organisation <strong>{comparison.delta?.organisation_accuracy ?? "—"}</strong></span><span>Site <strong>{comparison.delta?.site_accuracy ?? "—"}</strong></span><span>Review burden <strong>{comparison.delta?.reviews_per_genuine_opportunity ?? "—"}</strong></span></div></div>}
      <div className="panel"><h2>Case results</h2><div className="table-wrap"><table><thead><tr><th>Outcome</th><th>Provider</th><th>Result</th><th>First source</th><th>Lead time</th><th>Organisation</th><th>Site/project</th><th>Reviews</th></tr></thead><tbody>{(detail.case_results || []).map((item) => <tr key={item.benchmark_case_uuid}><td><strong>{item.outcome_type}</strong><span className="cell-subtitle">{formatDate(item.outcome_date)} · {item.known_regulatory_id || "—"}</span></td><td>{item.known_operator || "—"}<span className="cell-subtitle">{item.known_location || "—"}</span></td><td>{item.usable ? <Badge tone={item.detected ? "approved" : "pending"}>{item.detected ? "Detected" : "Missed"}</Badge> : <Badge>Excluded</Badge>}<span className="cell-subtitle">{item.exclusion_reason || (item.opportunity_created ? "Opportunity created" : "No opportunity")}</span></td><td>{titleCase(item.first_source)}</td><td>{item.lead_time_days == null ? "—" : `${item.lead_time_days} days`}</td><td>{titleCase(item.organisation_resolution)}</td><td>{titleCase(item.site_resolution)}</td><td>{item.review_items}</td></tr>)}</tbody></table></div></div>
    </>}
  </section>;
}

export default function App() {
  const auth = useAuth();
  const path = useHashLocation();
  const apiClient = useMemo(() => useApi(auth.getToken, auth.logout), [auth.getToken, auth.logout]);
  const [vertical, setVertical] = useState(() => restoredVertical());
  const customerHostname = window.location.hostname === "careprospect.co.uk";
  useEffect(() => {
    document.title = customerHostname
      ? "CareProspect — Early intelligence on new children’s homes"
      : "SignalHub — Internal intelligence";
  }, [customerHostname]);
  const onVerticalChange = useCallback((value) => {
    setVertical(value);
    window.sessionStorage.setItem("signalhub.vertical", value);
  }, []);
  const scopedApiClient = useMemo(() => async (requestPath, options = {}) => {
    const method = String(options.method || "GET").toUpperCase();
    if (method !== "GET" && requestPath !== "/admin/opportunities/recalculate") return apiClient(requestPath, options);
    return apiClient(verticalScopedPath(requestPath, vertical), options);
  }, [apiClient, vertical]);
  if (auth.loading) return <div className="app-loading"><span className="spinner" /> Checking session…</div>;
  if (!auth.user && customerHostname && path === "/privacy") return <PublicLegalPage page="privacy" />;
  if (!auth.user && customerHostname && path === "/terms") return <PublicLegalPage page="terms" />;
  if (!auth.user && customerHostname && !path.startsWith("/care/login")) return <PublicSite />;
  if (!auth.user) return <LoginPage onLogin={auth.login} authError={auth.authError} configured={auth.configured} passwordChallenge={auth.passwordChallenge} onCompleteNewPassword={auth.completeNewPassword} onCancelPasswordChallenge={auth.cancelPasswordChallenge} customerBrand={customerHostname || path.startsWith("/care")} />;
  const isAdmin = auth.groups.includes("NurserySignalAdmins");
  const isCustomer = auth.groups.includes("CareSignalCustomers");
  if (isCustomer && !isAdmin) return <CustomerApp auth={auth} path={path} />;
  if (!isAdmin) return <main className="login-page"><section className="login-card"><div className="brand-mark">SH</div><h1>Access not assigned</h1><p className="muted">This account has not been assigned to SignalHub or a CareProspect customer workspace.</p><button className="button primary full-width" onClick={auth.logout}>Log out</button></section></main>;
  const route = path.split("?")[0];
  const listQuery = path.includes("?") ? path.split("?")[1] : "";
  const detailMatch = route.match(/^\/(inbox|history|unmatched)\/([^/?#]+)/);
  const opportunityMatch = route.match(/^\/opportunities\/([^/?#]+)/);
  const detailMode = detailMatch?.[1] === "inbox" ? "inbox" : detailMatch?.[1] === "unmatched" ? "unmatched" : "history";
  const detailNotice = new URLSearchParams(listQuery).get("notice") || "";
  const showVertical = vertical === "ALL";
  return <Shell user={auth.user} onLogout={auth.logout} onNavigate={navigate} currentPath={path} vertical={vertical} onVerticalChange={onVerticalChange}>
    {detailMatch ? <SignalDetail key={vertical} signalId={detailMatch[2]} apiClient={scopedApiClient} queueMode={detailMode === "inbox"} unmatchedMode={detailMode === "unmatched"} initialNotice={detailNotice} onBack={() => navigate(detailMode === "inbox" ? "/inbox" : detailMode === "unmatched" ? "/unmatched" : "/history")} onReviewed={({ message, nextId }) => navigate(nextId ? `/inbox/${nextId}?notice=${encodeURIComponent(message)}` : `/inbox?notice=${encodeURIComponent(message)}`)} /> : opportunityMatch ? <OpportunityDetail key={vertical} opportunityId={opportunityMatch[1]} apiClient={scopedApiClient} onBack={() => navigate("/opportunities")} /> : route === "/inbox" ? <ReviewInboxPage key={vertical} apiClient={scopedApiClient} onNavigate={navigate} initialQuery={listQuery} showVertical={showVertical} /> : route === "/history" ? <ReviewedSignalsPage key={vertical} apiClient={scopedApiClient} onNavigate={navigate} initialQuery={listQuery} showVertical={showVertical} /> : route === "/unmatched" ? <UnmatchedSignalsPage key={vertical} apiClient={scopedApiClient} onNavigate={navigate} initialQuery={listQuery} showVertical={showVertical} /> : route === "/match-review" ? <MatchReviewPage key={vertical} apiClient={scopedApiClient} onNavigate={navigate} showVertical={showVertical} /> : route === "/opportunities" ? <OpportunitiesPage key={vertical} apiClient={scopedApiClient} onNavigate={navigate} showVertical={showVertical} /> : route === "/sources" ? <SourcesPage key={vertical} apiClient={scopedApiClient} selectedVertical={vertical} /> : route === "/procurement" ? <ProcurementEvaluationPage key={vertical} apiClient={scopedApiClient} /> : route === "/organisations" ? <OrganisationsPage key={vertical} apiClient={scopedApiClient} /> : route === "/customers" ? <CustomersPage apiClient={apiClient} /> : route === "/backtesting" ? <BacktestingPage key={vertical} apiClient={scopedApiClient} selectedVertical={vertical} /> : <Dashboard key={vertical} apiClient={scopedApiClient} onNavigate={navigate} />}
  </Shell>;
}
