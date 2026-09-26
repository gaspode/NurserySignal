import { useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "./api.js";
import { displayAttributeName, useAuth } from "./auth.js";

const PAGE_SIZE = 10;

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

export function LoginPage({ onLogin, authError = "", configured = true, passwordChallenge, onCompleteNewPassword, onCancelPasswordChallenge }) {
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
    <main className="login-page">
      <section className="login-card">
        <div className="brand-mark">NS</div>
        <p className="eyebrow">Internal operations</p>
        <h1>NurserySignal</h1>
        <p className="muted">Review nursery signals before they become customer opportunities.</p>
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
        <p className="login-footnote">Access is restricted to invited staff accounts.</p>
      </section>
    </main>
  );
}

function ConfirmationModal({ title, message, confirmLabel, danger = false, busy = false, onConfirm, onCancel }) {
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
        <div className="modal-actions">
          <button className="button secondary" onClick={onCancel} disabled={busy}>Cancel</button>
          <button ref={confirmRef} className={`button ${danger ? "reject" : "approve"}`} onClick={onConfirm} disabled={busy}>{busy ? "Saving…" : confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

function Shell({ user, onLogout, onNavigate, currentPath, children }) {
  const email = user?.getUsername?.() || "Signed-in staff";
  const route = currentPath.split("?")[0];
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => onNavigate("/")}><span className="brand-mark small">NS</span><span>NurserySignal</span></button>
        <p className="sidebar-label">Workspace</p>
        <nav aria-label="Primary navigation">
          <button className={currentPath === "/" ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/")}>Overview</button>
          <button className={route.startsWith("/inbox") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/inbox")}>Review Inbox</button>
          <button className={route.startsWith("/history") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/history")}>Reviewed Signals</button>
          <button className={route.startsWith("/opportunities") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/opportunities")}>Opportunities</button>
          <button className={route.startsWith("/sources") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/sources")}>Sources</button>
        </nav>
        <div className="sidebar-bottom">
          <span className="connection-dot" /> Production workspace
        </div>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <div><span className="mobile-brand">NurserySignal</span></div>
          <div className="user-menu"><span className="user-email">{email}</span><button className="button ghost" onClick={onLogout}>Log out</button></div>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}

export function SourcesPage({ apiClient }) {
  const [sources, setSources] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [running, setRunning] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = async ({ initial = false } = {}) => {
    if (initial) setLoading(true); else setRefreshing(true);
    setError("");
    try { setSources((await apiClient("/admin/sources")).items || []); }
    catch (loadError) { setError(loadError.message); }
    finally { setLoading(false); setRefreshing(false); }
  };
  useEffect(() => { load({ initial: true }); }, []);
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
      } catch (pollError) { setError(pollError.message); }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [running, apiClient]);
  async function run(source) {
    setRunning({ sourceKey: source.key, runId: null });
    setError(""); setNotice("");
    try {
      const result = await apiClient(`/admin/sources/${source.key}/run`, { method: "POST" });
      setRunning({ sourceKey: source.key, runId: result.run_id });
      await load();
    } catch (runError) { setRunning(null); setError(runError.message); }
  }
  if (loading) return <LoadingState label="Loading sources" />;
  return <section>
    <div className="page-heading"><div><p className="eyebrow">Collection operations</p><h1>Sources</h1><p className="muted">Monitor configured collectors and start bounded manual runs.</p></div><RefreshButton busy={refreshing} onClick={load} /></div>
    {notice && <div className="notice" role="status">{notice}</div>}{error && <ErrorState message={error} onRetry={load} />}
    <div className="sources-grid">{sources.map((source) => {
      const active = running?.sourceKey === source.key;
      const summary = source.last_summary || {};
      return <article className="panel source-card" key={source.key}>
        <div className="source-card-heading"><div><p className="eyebrow">{source.key}</p><h2>{source.display_name}</h2><p className="muted">Provider: {source.provider}</p></div><Badge tone={source.schedule_state === "ENABLED" ? "approved" : "neutral"}>{source.schedule_state}</Badge></div>
        <dl className="source-meta"><div><dt>Schedule</dt><dd>{source.schedule_expression}</dd></div><div><dt>Last status</dt><dd>{source.last_status || "No persisted run yet"}</dd></div><div><dt>Last attempt</dt><dd>{formatDate(source.last_attempt_at, true)}</dd></div><div><dt>Last successful</dt><dd>{formatDate(source.last_success_at, true)}</dd></div></dl>
        {source.last_run && <><h3>Last result</h3><div className="source-counts"><span>Fetched <strong>{summary.records_fetched || 0}</strong></span><span>Matched <strong>{summary.candidates_matched || 0}</strong></span><span>Queued <strong>{summary.signals_queued || 0}</strong></span><span>Excluded <strong>{summary.excluded || 0}</strong></span><span>Duplicates <strong>{summary.duplicates || 0}</strong></span><span>Errors <strong>{summary.errors || 0}</strong></span></div><p className="muted small-text">{titleCase(source.last_run.invocation_source)} run</p></>}
        {source.last_error && <div className="notice error-state">{source.last_error.category}: {source.last_error.message}</div>}
        <button className="button primary" onClick={() => run(source)} disabled={Boolean(running)}>{active ? "Run in progress…" : "Run now"}</button>
        {source.recent_runs?.length > 0 && <details className="source-history"><summary>Recent runs ({source.recent_runs.length})</summary>{source.recent_runs.map((item) => <div className="source-history-row" key={item.id}><span>{formatDate(item.started_at, true)} · {titleCase(item.invocation_source)}</span><Badge tone={item.status === "SUCCESS" ? "approved" : item.status === "FAILED" ? "rejected" : "pending"}>{item.status}</Badge></div>)}</details>}
      </article>;
    })}</div>
  </section>;
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
      const [pending, approved, rejected, recent] = await Promise.all([
        apiClient("/admin/signals?review_status=PENDING&limit=1"),
        apiClient("/admin/signals?review_status=APPROVED&limit=1"),
        apiClient("/admin/signals?review_status=REJECTED&limit=1"),
        apiClient("/admin/signals?limit=100"),
      ]);
      const sourceCounts = recent.items.reduce((counts, item) => {
        counts[item.source_type] = (counts[item.source_type] || 0) + 1;
        return counts;
      }, {});
      const falsePositives = recent.items.filter(isFalsePositive).length;
      setData({ pending: pending.total, approved: approved.total, rejected: rejected.total, recent: recent.items.length, falsePositives, sourceCounts });
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      if (initial) setLoading(false);
      else setRefreshing(false);
    }
  };
  useEffect(() => { load({ initial: true }); }, []);
  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">Operations overview</p><h1>Signal review desk</h1><p className="muted">Triage incoming nursery signals and preserve the evidence trail.</p></div><div className="page-actions"><RefreshButton busy={refreshing} onClick={() => load()} /><button className="button primary" onClick={() => onNavigate("/inbox")}>Review inbox</button></div></div>
      {loading && <LoadingState label="Loading overview" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {data && <>
        <div className="metric-grid">
          <Metric label="Pending review" value={data.pending} tone="amber" onClick={() => onNavigate("/inbox")} />
          <Metric label="Approved" value={data.approved} tone="green" onClick={() => onNavigate("/history?review_status=APPROVED")} />
          <Metric label="Rejected" value={data.rejected} tone="red" onClick={() => onNavigate("/history?review_status=REJECTED")} />
          <Metric label="Likely false positives" value={data.falsePositives} tone="slate" onClick={() => onNavigate("/history?review_status=REJECTED")} />
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
    review_status: mode === "history" ? params.get("review_status") || "REVIEWED" : "PENDING",
    source_type: params.get("source_type") || "",
    discovered_from: params.get("discovered_from") || "",
    discovered_to: params.get("discovered_to") || "",
    q: params.get("q") || "",
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

function SignalListPage({ apiClient, onNavigate, initialQuery = "", mode }) {
  const [filters, setFilters] = useState(() => {
    return initialFilters(initialQuery, mode);
  });
  const [page, setPage] = useState(0);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(() => new URLSearchParams(initialQuery).get("notice") || "");
  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE });
    Object.entries(filters).forEach(([key, value]) => value && params.set(key, value));
    return params.toString();
  }, [filters, page]);
  const load = async ({ initial = false } = {}) => {
    if (initial) setLoading(true);
    else setRefreshing(true);
    setError("");
    try { setResult(await apiClient(`/admin/signals?${query}`)); } catch (loadError) { setError(loadError.message); } finally {
      if (initial) setLoading(false);
      else setRefreshing(false);
    }
  };
  useEffect(() => { load({ initial: true }); }, [query]);
  function updateFilter(name, value) { setPage(0); setFilters((current) => ({ ...current, [name]: value })); }
  const totalPages = result ? Math.max(1, Math.ceil(result.total / PAGE_SIZE)) : 1;
  const inbox = mode === "inbox";
  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">{inbox ? "Active queue" : "Decision history"}</p><h1>{inbox ? "Review Inbox" : "Reviewed Signals"}</h1><p className="muted">{inbox ? "Work through pending signals one at a time." : "Search and correct previous review decisions without changing evidence."}</p></div><div className="page-actions"><RefreshButton busy={refreshing} onClick={() => load()} />{inbox ? <button className="button secondary" onClick={() => onNavigate("/history")}>View reviewed signals</button> : <ReprocessTool apiClient={apiClient} onComplete={setNotice} />}<span className="result-count">{result?.total ?? "—"} total</span></div></div>
      {notice && <div className="notice" role="status">{notice}</div>}
      <div className="filter-bar" aria-label="Signal filters">
        {!inbox && <label>Status<select aria-label="Review status" value={filters.review_status} onChange={(event) => updateFilter("review_status", event.target.value)}><option value="REVIEWED">All reviewed</option><option value="APPROVED">Approved</option><option value="REJECTED">Rejected</option></select></label>}
        {!inbox && <label>Search<input aria-label="Search reviewed signals" type="search" placeholder="Proposal, reference, council…" value={filters.q} onChange={(event) => updateFilter("q", event.target.value)} /></label>}
        <label>Source<select aria-label="Source type" value={filters.source_type} onChange={(event) => updateFilter("source_type", event.target.value)}><option value="">All sources</option><option value="planning">Planning</option><option value="recruitment">Recruitment</option><option value="operator_announcement">Operator announcement</option><option value="local_news">Local news</option></select></label>
        <label>From<input aria-label="Discovered from" type="date" value={filters.discovered_from} onChange={(event) => updateFilter("discovered_from", event.target.value)} /></label>
        <label>To<input aria-label="Discovered to" type="date" value={filters.discovered_to} onChange={(event) => updateFilter("discovered_to", event.target.value)} /></label>
        <button className="button secondary filter-reset" onClick={() => { setPage(0); setFilters(initialFilters("", mode)); }}>Reset</button>
      </div>
      {loading && <LoadingState label="Loading signals" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {!loading && !error && result?.items.length === 0 && <div className="state-card"><strong>{inbox ? "Inbox clear" : "No reviewed signals match these filters."}</strong><p className="muted">{inbox ? "There are no pending signals waiting for review." : "Try changing the search or filters."}</p></div>}
      {!loading && !error && result?.items.length > 0 && <>
        <div className="table-wrap"><table><thead><tr><th>Discovered</th><th>Signal</th><th>Source</th><th>Location / operator</th><th>Candidate</th><th>Rule confidence</th><th>AI shadow</th><th>Review</th></tr></thead><tbody>{result.items.map((item) => <SignalRow key={item.id} item={item} onClick={() => onNavigate(`/${inbox ? "inbox" : "history"}/${item.id}`)} />)}</tbody></table></div>
        <div className="pagination"><span>Page {page + 1} of {totalPages}</span><div><button className="button secondary" disabled={page === 0} onClick={() => setPage((current) => current - 1)}>Previous</button><button className="button secondary" disabled={page + 1 >= totalPages} onClick={() => setPage((current) => current + 1)}>Next</button></div></div>
      </>}
    </section>
  );
}

export function ReviewInboxPage(props) { return <SignalListPage {...props} mode="inbox" />; }
export function ReviewedSignalsPage(props) { return <SignalListPage {...props} mode="history" />; }
export function SignalsPage(props) { return <ReviewInboxPage {...props} />; }

export function OpportunitiesPage({ apiClient, onNavigate }) {
  const [result, setResult] = useState(null);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const load = async () => { setLoading(true); setError(""); try { const params = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE }); if (search) params.set("q", search); setResult(await apiClient(`/admin/opportunities?${params}`)); } catch (e) { setError(e.message); } finally { setLoading(false); } };
  useEffect(() => { load(); }, [page]);
  return <section><div className="page-heading"><div><p className="eyebrow">Commercial evidence</p><h1>Opportunities</h1><p className="muted">Facilities supported by one or more independent signals.</p></div><RefreshButton busy={loading} onClick={load} /></div><div className="filter-bar"><label>Search<input aria-label="Search opportunities" type="search" value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => event.key === "Enter" && (setPage(0), load())} placeholder="Name or explanation…" /></label><button className="button secondary" onClick={() => { setSearch(""); setPage(0); }}>Reset</button></div>{error && <ErrorState message={error} onRetry={load} />}{loading && !result && <LoadingState label="Loading opportunities" />}{result?.items?.length === 0 && <div className="state-card"><strong>No opportunities yet.</strong><p className="muted">Opportunities appear after signals are enriched.</p></div>}{result?.items?.length > 0 && <><div className="table-wrap"><table><thead><tr><th>Opportunity</th><th>Lifecycle</th><th>Confidence</th><th>Evidence</th><th>Why</th></tr></thead><tbody>{result.items.map((item) => <tr className="clickable-row" key={item.id} onClick={() => onNavigate(`/opportunities/${item.id}`)}><td><strong>{item.name}</strong><span className="cell-subtitle">{titleCase(item.event_type)}</span></td><td><Badge>{titleCase(item.lifecycle_stage)}</Badge></td><td>{Math.round((item.confidence || 0) * 100)}%</td><td>{item.signal_count}</td><td>{item.stage_reason || "—"}</td></tr>)}</tbody></table></div><div className="pagination"><span>Page {page + 1} of {Math.max(1, Math.ceil(result.total / PAGE_SIZE))}</span><div><button className="button secondary" disabled={page === 0} onClick={() => setPage((value) => value - 1)}>Previous</button><button className="button secondary" disabled={(page + 1) * PAGE_SIZE >= result.total} onClick={() => setPage((value) => value + 1)}>Next</button></div></div></>}</section>;
}

export function OpportunityDetail({ opportunityId, apiClient, onBack }) {
  const [opportunity, setOpportunity] = useState(null); const [error, setError] = useState("");
  useEffect(() => { apiClient(`/admin/opportunities/${opportunityId}`).then(setOpportunity).catch((e) => setError(e.message)); }, [opportunityId]);
  if (error) return <ErrorState message={error} />; if (!opportunity) return <LoadingState label="Loading opportunity" />;
  return <section><button className="back-link" onClick={onBack}>← Back to Opportunities</button><div className="page-heading"><div><p className="eyebrow">Opportunity evidence</p><h1>{opportunity.name}</h1><p className="muted">{titleCase(opportunity.lifecycle_stage)} · {Math.round((opportunity.confidence || 0) * 100)}% opportunity confidence</p></div><Badge>{titleCase(opportunity.lifecycle_stage)}</Badge></div><div className="panel"><h2>Evidence timeline</h2>{opportunity.signals.map((signal) => <article className="timeline-item" key={signal.id}><div><Badge>{titleCase(signal.source_type)}</Badge><strong>{signal.title}</strong><span className="cell-subtitle">{formatDate(signal.discovered_at)} · Rule confidence {signal.rule_confidence == null ? "—" : `${Math.round(signal.rule_confidence * 100)}%`}</span></div><p className="muted">{signal.provenance?.reason || "Linked by deterministic v1 correlation."}</p><button className="button ghost" onClick={() => window.location.hash = `/history/${signal.id}`}>Open signal</button>{signal.ai_status === "SUCCEEDED" && <span className="cell-subtitle">AI shadow: {titleCase(signal.ai_recommendation)} · {Math.round(signal.ai_confidence * 100)}%</span>}</article>)}</div><div className="panel"><h2>Correlation explanation</h2><p>{opportunity.stage_reason || "Evidence collected from the signal pipeline."}</p><pre>{JSON.stringify(opportunity.confidence_breakdown || {}, null, 2)}</pre></div></section>;
}

function SignalRow({ item, onClick }) {
  const council = item.metadata?.council;
  const recruitmentFacts = item.extracted_facts || {};
  const aiLabel = item.ai_status === "SUCCEEDED" && item.ai_recommendation
    ? `${titleCase(item.ai_recommendation)} · ${Math.round(item.ai_confidence * 100)}%`
    : item.ai_status === "FAILED" ? "Unavailable" : "—";
  return <tr className={isFalsePositive(item) ? "false-positive-row" : "clickable-row"} onClick={onClick} tabIndex="0" onKeyDown={(event) => event.key === "Enter" && onClick()}>
    <td className="nowrap">{formatDate(item.discovered_at)}</td><td><strong>{item.title}</strong><span className="cell-subtitle">{item.external_id}</span></td><td><Badge>{titleCase(item.source_type)}</Badge>{item.source_type === "recruitment" && <span className="cell-subtitle">{titleCase(recruitmentFacts.recruitment_relevance || "—")}</span>}</td><td>{council || item.organisation_hint || "—"}<span className="cell-subtitle">{item.location_hint || "—"}</span></td><td><strong>{titleCase(item.event_type)}</strong><span className="cell-subtitle">{item.source_type === "recruitment" ? `${titleCase(recruitmentFacts.recruitment_role_category)} · Change ${titleCase(recruitmentFacts.commercial_change_evidence)}` : titleCase(item.lifecycle_stage)}</span>{isFalsePositive(item) && <span className="false-label">Likely false positive</span>}</td><td>{item.confidence == null ? "—" : `${Math.round(item.confidence * 100)}%`}</td><td><span className="cell-subtitle">AI shadow</span>{aiLabel}</td><td><Badge tone={reviewTone(item.review_status)}>{item.review_status || "PROCESSING"}</Badge></td>
  </tr>;
}

export function SignalDetail({ signalId, apiClient, onBack, queueMode = false, initialNotice = "", onReviewed }) {
  const [signal, setSignal] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(initialNotice);
  const [modalAction, setModalAction] = useState(null);
  const [busy, setBusy] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  const load = async () => { setLoading(true); setError(""); try { setSignal(await apiClient(`/admin/signals/${signalId}`)); } catch (loadError) { setError(loadError.message); } finally { setLoading(false); } };
  useEffect(() => { load(); }, [signalId]);
  async function confirmReview() {
    const action = modalAction;
    if (!action) return;
    setBusy(true);
    setError("");
    try {
      await apiClient(`/admin/signals/${signalId}/${action}`, { method: "POST" });
      const message = `Signal ${action === "approve" ? "approved" : "rejected"} successfully.`;
      setModalAction(null);
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
  if (loading) return <LoadingState label="Loading signal" />;
  if (error && !signal) return <ErrorState message={error} onRetry={load} />;
  const enrichment = signal?.enrichment;
  const falsePositive = isFalsePositive(enrichment);
  const planning = signal.source_type === "planning" ? signal.metadata || {} : null;
  const latestDocument = signal.documents?.[signal.documents.length - 1];
  const reviewStatus = enrichment?.review_status;
  const correctionAction = reviewStatus === "APPROVED" ? "reject" : "approve";
  const modalTitle = modalAction === "approve" ? "Approve this signal?" : "Reject this signal?";
  const modalMessage = !queueMode && ["APPROVED", "REJECTED"].includes(reviewStatus)
    ? `Current decision: ${reviewStatus}. Change it to ${correctionAction === "approve" ? "APPROVED" : "REJECTED"}? Preserved evidence remains unchanged.`
    : modalAction === "approve"
      ? "This will mark the candidate approved for the current review workflow."
      : "This will mark the candidate rejected. The preserved evidence and review history remain available.";
  return <section>
    <button className="back-link" onClick={onBack}>← Back to {queueMode ? "Review Inbox" : "Reviewed Signals"}</button>
    <div className="page-heading detail-heading"><div><p className="eyebrow">{queueMode ? "Inbox review" : "Reviewed signal"}</p><h1>{signal.title}</h1><p className="muted">{signal.source_type} · {signal.external_id}</p></div><Badge tone={reviewTone(enrichment?.review_status)}>{enrichment?.review_status || "PROCESSING"}</Badge></div>
    {notice && <div className="notice" role="status">{notice}</div>}{error && <div className="notice error-state" role="alert">{error}</div>}
    {queueMode && reviewStatus === "PENDING" && <div className="review-action-bar"><div><strong>Ready for decision</strong><span className="muted">Approve or reject this candidate.</span></div><div className="review-actions"><button className="button approve" onClick={() => setModalAction("approve")}>Approve</button><button className="button reject" onClick={() => setModalAction("reject")}>Reject</button></div></div>}
    {!queueMode && ["APPROVED", "REJECTED"].includes(reviewStatus) && <div className="review-action-bar"><div><strong>Decision recorded</strong><span className="muted">This reviewed decision can be deliberately corrected.</span></div><div className="review-actions"><button className="button secondary" onClick={runAiAssessment} disabled={aiBusy}>{aiBusy ? "Running AI…" : "Run AI shadow assessment"}</button><button className={`button ${correctionAction === "reject" ? "reject" : "approve"}`} onClick={() => setModalAction(correctionAction)}>Change to {correctionAction === "approve" ? "Approved" : "Rejected"}</button></div></div>}
    <div className="detail-grid">
      <DetailPanel title="Raw signal"><Field label="Source type" value={titleCase(signal.source_type)} /><Field label="Source URL" value={<a href={signal.source_url} target="_blank" rel="noreferrer">{signal.source_url}</a>} /><Field label="External ID" value={signal.external_id} /><Field label="Discovered" value={formatDate(signal.discovered_at, true)} /><Field label="Title" value={signal.title} /><Field label="Raw text" value={<p className="raw-text">{signal.raw_text}</p>} /><Field label="Organisation hint" value={signal.organisation_hint} /><Field label="Location hint" value={signal.location_hint} /><Field label="Metadata" value={<pre>{JSON.stringify(signal.metadata || {}, null, 2)}</pre>} /></DetailPanel>
      {planning && <DetailPanel title="Planning application"><Field label="Provider reference" value={planning.provider_application_id} /><Field label="Council" value={planning.council} /><Field label="Application date" value={planning.application_date} /><Field label="Planning status" value={planning.planning_status} /><Field label="Decision" value={planning.decision} /><Field label="Postcode" value={planning.postcode} /><Field label="Coordinates" value={planning.latitude == null ? null : `${planning.latitude}, ${planning.longitude}`} /><Field label="Tracked revisions" value={signal.planning_revisions?.length || 0} /></DetailPanel>}
      <DetailPanel title="Deterministic assessment">{enrichment ? <><Field label="Event type" value={titleCase(enrichment.event_type)} /><Field label="Nursery name" value={enrichment.nursery_name} /><Field label="Operator" value={enrichment.operator_name} /><Field label="Address" value={enrichment.address} /><Field label="Expected opening" value={enrichment.expected_opening_date} /><Field label="Capacity" value={enrichment.capacity} /><Field label="Lifecycle stage" value={<Badge>{titleCase(enrichment.lifecycle_stage)}</Badge>} /><Field label="Rule confidence" value={enrichment.confidence == null ? "—" : `${Math.round(enrichment.confidence * 100)}%`} />{signal.source_type === "recruitment" && <><Field label="Role" value={titleCase(enrichment.extracted_facts?.recruitment_role_category)} /><Field label="Setting" value={titleCase(enrichment.extracted_facts?.recruitment_setting_category)} /><Field label="Recruitment relevance" value={titleCase(enrichment.extracted_facts?.recruitment_relevance)} /><Field label="Change evidence" value={titleCase(enrichment.extracted_facts?.commercial_change_evidence)} /></>}{falsePositive && <div className="false-positive-callout">Likely false positive · {enrichment.extracted_facts?.classification}</div>}<Field label="Extracted facts" value={<pre>{JSON.stringify(enrichment.extracted_facts || {}, null, 2)}</pre>} /><Field label="Evidence used" value={<pre>{JSON.stringify(enrichment.evidence || {}, null, 2)}</pre>} /></> : <p className="muted">Enrichment is still processing.</p>}</DetailPanel>
      <DetailPanel title="AI shadow assessment"><p className="muted small-text">Advisory only — human review remains authoritative.</p>{signal.ai_reviews?.[0]?.status === "SUCCEEDED" ? <><Field label="AI assessment" value={titleCase(signal.ai_reviews[0].recommendation)} /><Field label="AI confidence" value={`${Math.round(signal.ai_reviews[0].confidence * 100)}%`} />{signal.source_type === "planning" ? <Field label="Planning relevance" value={titleCase(signal.ai_reviews[0].planning_relevance)} /> : <Field label="Recruitment relevance" value={titleCase(signal.ai_reviews[0].recruitment_relevance)} />}<Field label="Change evidence" value={titleCase(signal.ai_reviews[0].commercial_change_evidence)} /><Field label="Reason" value={signal.ai_reviews[0].reason} /><Field label="Model" value={signal.ai_reviews[0].model_id} /><Field label="Prompt version" value={signal.ai_reviews[0].prompt_version} /><Field label="Evaluated" value={formatDate(signal.ai_reviews[0].evaluated_at, true)} /></> : signal.ai_reviews?.[0]?.status === "FAILED" ? <p className="muted">AI assessment unavailable ({titleCase(signal.ai_reviews[0].failure_category)}). Human review is unaffected.</p> : <p className="muted">No AI shadow assessment available.</p>}{(queueMode || reviewStatus === "PENDING") && <button className="button secondary" onClick={runAiAssessment} disabled={aiBusy}>{aiBusy ? "Running AI assessment…" : signal.ai_reviews?.length ? "Re-run AI assessment" : "Run AI assessment"}</button>}</DetailPanel>
      <DetailPanel title="Evidence & provenance"><Field label="First seen" value={formatDate(signal.created_at, true)} /><Field label="Latest update" value={formatDate(signal.planning_revisions?.[0]?.observed_at || enrichment?.updated_at, true)} /><Field label="Evidence key" value={<code>{latestDocument?.s3_key || "—"}</code>} /><Field label="Evidence checksum" value={<code>{latestDocument?.sha256 || "—"}</code>} /><button className="button secondary" onClick={openEvidence} disabled={!signal.documents?.length}>View preserved JSON</button><p className="muted small-text">Access uses a short-lived authenticated download URL. The evidence bucket remains private.</p></DetailPanel>
      <DetailPanel title="Review"><Field label="Current state" value={<Badge tone={reviewTone(enrichment?.review_status)}>{enrichment?.review_status || "PROCESSING"}</Badge>} /><Field label="Reviewer" value={enrichment?.reviewed_by} /><Field label="Reviewed at" value={formatDate(enrichment?.reviewed_at, true)} /></DetailPanel>
    </div>
    {modalAction && <ConfirmationModal title={modalTitle} message={modalMessage} confirmLabel={modalAction === "approve" ? "Approve" : "Reject"} danger={modalAction === "reject"} busy={busy} onCancel={() => setModalAction(null)} onConfirm={confirmReview} />}
  </section>;
}

function DetailPanel({ title, children }) { return <section className="panel detail-panel"><h2>{title}</h2>{children}</section>; }
function Field({ label, value }) { return <div className="field"><dt>{label}</dt><dd>{value || "—"}</dd></div>; }

export default function App() {
  const auth = useAuth();
  const path = useHashLocation();
  const apiClient = useMemo(() => useApi(auth.getToken, auth.logout), [auth.getToken, auth.logout]);
  if (auth.loading) return <div className="app-loading"><span className="spinner" /> Checking session…</div>;
  if (!auth.user) return <LoginPage onLogin={auth.login} authError={auth.authError} configured={auth.configured} passwordChallenge={auth.passwordChallenge} onCompleteNewPassword={auth.completeNewPassword} onCancelPasswordChallenge={auth.cancelPasswordChallenge} />;
  const route = path.split("?")[0];
  const listQuery = path.includes("?") ? path.split("?")[1] : "";
  const detailMatch = route.match(/^\/(inbox|history)\/([^/?#]+)/);
  const opportunityMatch = route.match(/^\/opportunities\/([^/?#]+)/);
  const detailMode = detailMatch?.[1] === "inbox" ? "inbox" : "history";
  const detailNotice = new URLSearchParams(listQuery).get("notice") || "";
  return <Shell user={auth.user} onLogout={auth.logout} onNavigate={navigate} currentPath={path}>
    {detailMatch ? <SignalDetail signalId={detailMatch[2]} apiClient={apiClient} queueMode={detailMode === "inbox"} initialNotice={detailNotice} onBack={() => navigate(detailMode === "inbox" ? "/inbox" : "/history")} onReviewed={({ message, nextId }) => navigate(nextId ? `/inbox/${nextId}?notice=${encodeURIComponent(message)}` : `/inbox?notice=${encodeURIComponent(message)}`)} /> : opportunityMatch ? <OpportunityDetail opportunityId={opportunityMatch[1]} apiClient={apiClient} onBack={() => navigate("/opportunities")} /> : route === "/inbox" ? <ReviewInboxPage apiClient={apiClient} onNavigate={navigate} initialQuery={listQuery} /> : route === "/history" ? <ReviewedSignalsPage apiClient={apiClient} onNavigate={navigate} initialQuery={listQuery} /> : route === "/opportunities" ? <OpportunitiesPage apiClient={apiClient} onNavigate={navigate} /> : route === "/sources" ? <SourcesPage apiClient={apiClient} /> : <Dashboard apiClient={apiClient} onNavigate={navigate} />}
  </Shell>;
}
