import { useEffect, useMemo, useState } from "react";
import { useApi } from "./api.js";
import { useAuth } from "./auth.js";

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
  return signal?.extracted_facts?.classification === "irrelevant-or-unclear";
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

function Badge({ children, tone = "neutral" }) {
  return <span className={`badge badge-${tone}`}>{children || "—"}</span>;
}

function reviewTone(status) {
  return { PENDING: "pending", APPROVED: "approved", REJECTED: "rejected" }[status] || "neutral";
}

export function LoginPage({ onLogin, authError = "", configured = true }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

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

  return (
    <main className="login-page">
      <section className="login-card">
        <div className="brand-mark">NS</div>
        <p className="eyebrow">Internal operations</p>
        <h1>NurserySignal</h1>
        <p className="muted">Review nursery signals before they become customer opportunities.</p>
        {!configured ? (
          <ErrorState message="This deployment has no Cognito configuration." />
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

function Shell({ user, onLogout, onNavigate, currentPath, children }) {
  const email = user?.getUsername?.() || "Signed-in staff";
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => onNavigate("/")}><span className="brand-mark small">NS</span><span>NurserySignal</span></button>
        <p className="sidebar-label">Workspace</p>
        <nav aria-label="Primary navigation">
          <button className={currentPath === "/" ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/")}>Overview</button>
          <button className={currentPath.startsWith("/signals") ? "nav-link active" : "nav-link"} onClick={() => onNavigate("/signals")}>Signals</button>
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

export function Dashboard({ apiClient, onNavigate }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const load = async () => {
    setLoading(true);
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
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);
  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">Operations overview</p><h1>Signal review desk</h1><p className="muted">Triage incoming nursery signals and preserve the evidence trail.</p></div><button className="button primary" onClick={() => onNavigate("/signals")}>Review signals</button></div>
      {loading && <LoadingState label="Loading overview" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {data && <>
        <div className="metric-grid">
          <Metric label="Pending review" value={data.pending} tone="amber" onClick={() => onNavigate("/signals?review_status=PENDING")} />
          <Metric label="Approved" value={data.approved} tone="green" />
          <Metric label="Rejected" value={data.rejected} tone="red" />
          <Metric label="Likely false positives" value={data.falsePositives} tone="slate" onClick={() => onNavigate("/signals?review_status=REJECTED")} />
        </div>
        <div className="dashboard-grid">
          <section className="panel"><div className="panel-heading"><h2>Recent signals</h2><button className="text-button" onClick={() => onNavigate("/signals")}>View all</button></div><p className="large-number">{data.recent}</p><p className="muted">signals available in the latest review window</p></section>
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

export function SignalsPage({ apiClient, onNavigate, initialQuery = "" }) {
  const [filters, setFilters] = useState(() => {
    const params = new URLSearchParams(initialQuery);
    return { review_status: params.get("review_status") || "", source_type: "", discovered_from: "", discovered_to: "" };
  });
  const [page, setPage] = useState(0);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE });
    Object.entries(filters).forEach(([key, value]) => value && params.set(key, value));
    return params.toString();
  }, [filters, page]);
  const load = async () => {
    setLoading(true);
    setError("");
    try { setResult(await apiClient(`/admin/signals?${query}`)); } catch (loadError) { setError(loadError.message); } finally { setLoading(false); }
  };
  useEffect(() => { load(); }, [query]);
  function updateFilter(name, value) { setPage(0); setFilters((current) => ({ ...current, [name]: value })); }
  const totalPages = result ? Math.max(1, Math.ceil(result.total / PAGE_SIZE)) : 1;
  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">Review queue</p><h1>Signals</h1><p className="muted">Raw signals and their evidence-backed enrichment candidates.</p></div><span className="result-count">{result?.total ?? "—"} total</span></div>
      <div className="filter-bar" aria-label="Signal filters">
        <label>Status<select aria-label="Review status" value={filters.review_status} onChange={(event) => updateFilter("review_status", event.target.value)}><option value="">All statuses</option><option value="PENDING">Pending</option><option value="APPROVED">Approved</option><option value="REJECTED">Rejected</option></select></label>
        <label>Source<select aria-label="Source type" value={filters.source_type} onChange={(event) => updateFilter("source_type", event.target.value)}><option value="">All sources</option><option value="planning">Planning</option><option value="recruitment">Recruitment</option><option value="operator_announcement">Operator announcement</option><option value="local_news">Local news</option></select></label>
        <label>From<input aria-label="Discovered from" type="date" value={filters.discovered_from} onChange={(event) => updateFilter("discovered_from", event.target.value)} /></label>
        <label>To<input aria-label="Discovered to" type="date" value={filters.discovered_to} onChange={(event) => updateFilter("discovered_to", event.target.value)} /></label>
        <button className="button secondary filter-reset" onClick={() => { setPage(0); setFilters({ review_status: "", source_type: "", discovered_from: "", discovered_to: "" }); }}>Reset</button>
      </div>
      {loading && <LoadingState label="Loading signals" />}
      {error && <ErrorState message={error} onRetry={load} />}
      {!loading && !error && result?.items.length === 0 && <div className="state-card"><strong>No signals match these filters.</strong><p className="muted">Try resetting the filters or ingest a fixture signal.</p></div>}
      {!loading && !error && result?.items.length > 0 && <>
        <div className="table-wrap"><table><thead><tr><th>Discovered</th><th>Signal</th><th>Source</th><th>Location / operator</th><th>Candidate</th><th>Confidence</th><th>Review</th></tr></thead><tbody>{result.items.map((item) => <SignalRow key={item.id} item={item} onClick={() => onNavigate(`/signals/${item.id}`)} />)}</tbody></table></div>
        <div className="pagination"><span>Page {page + 1} of {totalPages}</span><div><button className="button secondary" disabled={page === 0} onClick={() => setPage((current) => current - 1)}>Previous</button><button className="button secondary" disabled={page + 1 >= totalPages} onClick={() => setPage((current) => current + 1)}>Next</button></div></div>
      </>}
    </section>
  );
}

function SignalRow({ item, onClick }) {
  return <tr className={isFalsePositive(item) ? "false-positive-row" : "clickable-row"} onClick={onClick} tabIndex="0" onKeyDown={(event) => event.key === "Enter" && onClick()}>
    <td className="nowrap">{formatDate(item.discovered_at)}</td><td><strong>{item.title}</strong><span className="cell-subtitle">{item.external_id}</span></td><td><Badge>{titleCase(item.source_type)}</Badge></td><td>{item.organisation_hint || "—"}<span className="cell-subtitle">{item.location_hint || "—"}</span></td><td><strong>{titleCase(item.event_type)}</strong><span className="cell-subtitle">{titleCase(item.lifecycle_stage)}</span>{isFalsePositive(item) && <span className="false-label">Likely false positive</span>}</td><td>{item.confidence == null ? "—" : `${Math.round(item.confidence * 100)}%`}</td><td><Badge tone={reviewTone(item.review_status)}>{item.review_status || "PROCESSING"}</Badge></td>
  </tr>;
}

export function SignalDetail({ signalId, apiClient, onBack }) {
  const [signal, setSignal] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = async () => { setLoading(true); setError(""); try { setSignal(await apiClient(`/admin/signals/${signalId}`)); } catch (loadError) { setError(loadError.message); } finally { setLoading(false); } };
  useEffect(() => { load(); }, [signalId]);
  async function review(action) {
    if (!window.confirm(`Are you sure you want to ${action} this signal?`)) return;
    setNotice("");
    try { await apiClient(`/admin/signals/${signalId}/${action}`, { method: "POST" }); setNotice(`Signal ${action}d successfully.`); await load(); } catch (reviewError) { setError(reviewError.message); }
  }
  async function openEvidence() {
    const tab = window.open("about:blank", "_blank");
    try { const result = await apiClient(`/admin/signals/${signalId}/evidence`); if (tab) tab.location = result.url; } catch (evidenceError) { tab?.close(); setError(evidenceError.message); }
  }
  if (loading) return <LoadingState label="Loading signal" />;
  if (error && !signal) return <ErrorState message={error} onRetry={load} />;
  const enrichment = signal?.enrichment;
  const falsePositive = isFalsePositive(enrichment);
  const planning = signal.source_type === "planning" ? signal.metadata || {} : null;
  const latestDocument = signal.documents?.[signal.documents.length - 1];
  return <section>
    <button className="back-link" onClick={onBack}>← Back to signals</button>
    <div className="page-heading detail-heading"><div><p className="eyebrow">Signal detail</p><h1>{signal.title}</h1><p className="muted">{signal.source_type} · {signal.external_id}</p></div><Badge tone={reviewTone(enrichment?.review_status)}>{enrichment?.review_status || "PROCESSING"}</Badge></div>
    {notice && <div className="notice" role="status">{notice}</div>}{error && <div className="notice error-state" role="alert">{error}</div>}
    <div className="detail-grid">
      <DetailPanel title="Raw signal"><Field label="Source type" value={titleCase(signal.source_type)} /><Field label="Source URL" value={<a href={signal.source_url} target="_blank" rel="noreferrer">{signal.source_url}</a>} /><Field label="External ID" value={signal.external_id} /><Field label="Discovered" value={formatDate(signal.discovered_at, true)} /><Field label="Title" value={signal.title} /><Field label="Raw text" value={<p className="raw-text">{signal.raw_text}</p>} /><Field label="Organisation hint" value={signal.organisation_hint} /><Field label="Location hint" value={signal.location_hint} /><Field label="Metadata" value={<pre>{JSON.stringify(signal.metadata || {}, null, 2)}</pre>} /></DetailPanel>
      {planning && <DetailPanel title="Planning application"><Field label="Provider reference" value={planning.provider_application_id} /><Field label="Council" value={planning.council} /><Field label="Application date" value={planning.application_date} /><Field label="Planning status" value={planning.planning_status} /><Field label="Decision" value={planning.decision} /><Field label="Postcode" value={planning.postcode} /><Field label="Coordinates" value={planning.latitude == null ? null : `${planning.latitude}, ${planning.longitude}`} /><Field label="Tracked revisions" value={signal.planning_revisions?.length || 0} /></DetailPanel>}
      <DetailPanel title="Enrichment candidate">{enrichment ? <><Field label="Event type" value={titleCase(enrichment.event_type)} /><Field label="Nursery name" value={enrichment.nursery_name} /><Field label="Operator" value={enrichment.operator_name} /><Field label="Address" value={enrichment.address} /><Field label="Expected opening" value={enrichment.expected_opening_date} /><Field label="Capacity" value={enrichment.capacity} /><Field label="Lifecycle stage" value={<Badge>{titleCase(enrichment.lifecycle_stage)}</Badge>} /><Field label="Confidence" value={enrichment.confidence == null ? "—" : `${Math.round(enrichment.confidence * 100)}%`} />{falsePositive && <div className="false-positive-callout">Likely false positive · {enrichment.extracted_facts?.classification}</div>}<Field label="Extracted facts" value={<pre>{JSON.stringify(enrichment.extracted_facts || {}, null, 2)}</pre>} /><Field label="Evidence used" value={<pre>{JSON.stringify(enrichment.evidence || {}, null, 2)}</pre>} /></> : <p className="muted">Enrichment is still processing.</p>}</DetailPanel>
      <DetailPanel title="Evidence & provenance"><Field label="First seen" value={formatDate(signal.created_at, true)} /><Field label="Latest update" value={formatDate(signal.planning_revisions?.[0]?.observed_at || enrichment?.updated_at, true)} /><Field label="Evidence key" value={<code>{latestDocument?.s3_key || "—"}</code>} /><Field label="Evidence checksum" value={<code>{latestDocument?.sha256 || "—"}</code>} /><button className="button secondary" onClick={openEvidence} disabled={!signal.documents?.length}>View preserved JSON</button><p className="muted small-text">Access uses a short-lived authenticated download URL. The evidence bucket remains private.</p></DetailPanel>
      <DetailPanel title="Review"><Field label="Current state" value={<Badge tone={reviewTone(enrichment?.review_status)}>{enrichment?.review_status || "PROCESSING"}</Badge>} /><Field label="Reviewer" value={enrichment?.reviewed_by} /><Field label="Reviewed at" value={formatDate(enrichment?.reviewed_at, true)} />{enrichment?.review_status === "PENDING" && <div className="review-actions"><button className="button approve" onClick={() => review("approve")}>Approve</button><button className="button reject" onClick={() => review("reject")}>Reject</button></div>}</DetailPanel>
    </div>
  </section>;
}

function DetailPanel({ title, children }) { return <section className="panel detail-panel"><h2>{title}</h2>{children}</section>; }
function Field({ label, value }) { return <div className="field"><dt>{label}</dt><dd>{value || "—"}</dd></div>; }

export default function App() {
  const auth = useAuth();
  const path = useHashLocation();
  const apiClient = useMemo(() => useApi(auth.getToken, auth.logout), [auth.getToken, auth.logout]);
  if (auth.loading) return <div className="app-loading"><span className="spinner" /> Checking session…</div>;
  if (!auth.user) return <LoginPage onLogin={auth.login} authError={auth.authError} configured={auth.configured} />;
  const detailMatch = path.match(/^\/signals\/([^/?#]+)/);
  const listQuery = path.includes("?") ? path.split("?")[1] : "";
  return <Shell user={auth.user} onLogout={auth.logout} onNavigate={navigate} currentPath={path}>
    {detailMatch ? <SignalDetail signalId={detailMatch[1]} apiClient={apiClient} onBack={() => navigate("/signals")} /> : path.startsWith("/signals") ? <SignalsPage apiClient={apiClient} onNavigate={navigate} initialQuery={listQuery} /> : <Dashboard apiClient={apiClient} onNavigate={navigate} />}
  </Shell>;
}
