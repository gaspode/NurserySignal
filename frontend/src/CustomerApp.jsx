import { useCallback, useEffect, useMemo, useState } from "react";
import { useApi } from "./api.js";
import { CareProspectLogo } from "./PublicSite.jsx";

function go(path) {
  window.location.hash = path;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("en-GB", { dateStyle: "medium" }).format(date);
}

export function CustomerShell({ account, user, path, onLogout, vertical, onVerticalChange, children }) {
  const route = path.split("?")[0];
  const product = vertical === "NURSERY" ? "NurserySignal" : "CareProspect";
  return <div className="care-app">
    <header className="care-header">
      <button className="care-brand" aria-label={`${product} opportunities`} onClick={() => go(`/care/opportunities?vertical=${vertical}`)}>{vertical === "CHILDRENS_HOME" ? <CareProspectLogo compact tagline="Early intelligence on new children’s homes" /> : <span><strong>NurserySignal</strong><small>Early intelligence on nursery opportunities</small></span>}</button>
      <nav aria-label={`${product} navigation`}>
        <button className={route.startsWith("/care/opportunities") ? "active" : ""} onClick={() => go(`/care/opportunities?vertical=${vertical}`)}>Opportunities</button>
        <button className={route === "/care/saved" ? "active" : ""} onClick={() => go(`/care/saved?vertical=${vertical}`)}>Saved</button>
        <button className={route === "/care/alerts" ? "active" : ""} onClick={() => go(`/care/alerts?vertical=${vertical}`)}>Alerts</button>
        <button className={route === "/care/account" ? "active" : ""} onClick={() => go(`/care/account?vertical=${vertical}`)}>Account</button>
      </nav>
      {(account?.allowed_verticals || []).length > 1 && <label className="care-vertical-select">Product<select value={vertical} onChange={(event) => onVerticalChange(event.target.value)}>{account.allowed_verticals.map((value) => <option key={value} value={value}>{value === "NURSERY" ? "NurserySignal" : "CareProspect"}</option>)}</select></label>}
      <div className="care-user"><span>{account?.account_name || user?.getUsername?.()}</span><button onClick={onLogout}>Log out</button></div>
    </header>
    <main className="care-main">{children}</main>
  </div>;
}

function PortalState({ children, tone = "" }) {
  return <div className={`care-state ${tone}`} role={tone === "error" ? "alert" : undefined}>{children}</div>;
}

function OpportunityCard({ item, onOpen, onSave }) {
  return <article className="care-opportunity-card">
    <div className="care-card-top"><span className={`care-strength strength-${item.strength?.toLowerCase()}`}>{item.strength}</span><button className="care-save" aria-label={item.saved ? "Remove from saved" : "Save opportunity"} onClick={() => onSave(item)}>{item.saved ? "★ Saved" : "☆ Save"}</button></div>
    <button className="care-card-title" onClick={onOpen}><h2>{item.title}</h2></button>
    <p className="care-location">{[item.local_authority || item.town, item.region, item.postcode].filter(Boolean).join(" · ") || "Location not yet published"}</p>
    <div className="care-badges"><span>{item.change_label}</span><span>{item.stage_label}</span>{item.source_types.map((source) => <span key={source}>{source}</span>)}</div>
    <p>{item.summary}</p>
    <dl className="care-card-dates"><div><dt>First detected</dt><dd>{formatDate(item.first_detected)}</dd></div><div><dt>Latest update</dt><dd>{formatDate(item.last_updated)}</dd></div></dl>
    <button className="care-link" onClick={onOpen}>View evidence and details →</button>
  </article>;
}

export function OpportunityFeed({ api, savedOnly = false, vertical }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState({ q: "", region: "", local_authority: "", change_type: "", stage: "", source_type: "", updated_since: "" });
  const load = useCallback(async () => {
    setLoading(true); setError("");
    const params = new URLSearchParams();
    params.set("vertical", vertical);
    Object.entries(filters).forEach(([key, value]) => value && params.set(key, value));
    try {
      setData(await api(`${savedOnly ? "/customer/saved" : "/customer/opportunities"}?${params}`));
      if (Object.values(filters).some(Boolean)) api("/customer/events", { method: "POST", body: JSON.stringify({ event_type: "FILTER_USED", metadata: { filter: params.toString() } }) }).catch(() => {});
    } catch (loadError) { setError(loadError.message || "Opportunities could not be loaded."); }
    finally { setLoading(false); }
  }, [api, filters, savedOnly]);
  useEffect(() => { load(); }, [load]);
  async function toggleSave(item) {
    try {
      await api(`/customer/opportunities/${item.id}/save?vertical=${vertical}`, { method: item.saved ? "DELETE" : "POST" });
      await load();
    } catch (saveError) { setError(saveError.message || "The opportunity could not be saved."); }
  }
  return <section>
    <div className="care-page-heading"><div><p className="care-eyebrow">Commercial intelligence</p><h1>{savedOnly ? "Saved opportunities" : vertical === "NURSERY" ? "Nursery opportunities" : "Children’s-home opportunities"}</h1><p>{savedOnly ? "Developments you’re watching, kept in one place." : vertical === "NURSERY" ? "Reviewed evidence of new nurseries and material early-years changes." : "Reviewed evidence of new openings and material changes across children’s residential care."}</p></div><button className="care-secondary" onClick={load} disabled={loading}>{loading ? "Refreshing…" : "Refresh"}</button></div>
    {!savedOnly && <div className="care-filters">
      <label className="wide">Search<input aria-label="Search opportunities" value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} placeholder="Operator, location or postcode" /></label>
      <label>Region<input value={filters.region} onChange={(event) => setFilters({ ...filters, region: event.target.value })} /></label>
      <label>Local authority<input value={filters.local_authority} onChange={(event) => setFilters({ ...filters, local_authority: event.target.value })} /></label>
      <label>Change<select value={filters.change_type} onChange={(event) => setFilters({ ...filters, change_type: event.target.value })}><option value="">All</option><option value="OPENING">New opening</option><option value="EXPANSION">Expansion</option><option value="RELOCATION">Relocation</option><option value="OTHER_CHANGE">Other change</option></select></label>
      <label>Stage<select value={filters.stage} onChange={(event) => setFilters({ ...filters, stage: event.target.value })}><option value="">All</option>{vertical === "NURSERY" ? <><option value="PLANNING_PENDING">Planning pending</option><option value="PLANNING_APPROVED">Planning approved</option><option value="RECRUITMENT_ACTIVITY">Recruitment activity</option><option value="OTHER_CHANGE">Other change</option></> : <><option value="DISCOVERED">Early signal</option><option value="PLANNING">Planning</option><option value="RECRUITING">Recruiting</option><option value="REGISTRATION">Registration</option><option value="OPEN">Open / confirmed</option></>}</select></label>
      <label>Source<select value={filters.source_type} onChange={(event) => setFilters({ ...filters, source_type: event.target.value })}><option value="">All</option><option value="planning">Planning</option><option value="recruitment">Recruitment</option><option value="ofsted">Ofsted</option></select></label>
    </div>}
    {error && <PortalState tone="error"><strong>We couldn’t load the feed.</strong><p>{error}</p></PortalState>}
    {loading && !data ? <PortalState>Loading opportunities…</PortalState> : data?.items?.length ? <><p className="care-result-count">{data.total} {data.total === 1 ? "opportunity" : "opportunities"}</p><div className="care-card-grid">{data.items.map((item) => <OpportunityCard key={item.id} item={item} onSave={toggleSave} onOpen={() => go(`/care/opportunities/${item.id}?vertical=${vertical}`)} />)}</div></> : <PortalState><strong>{savedOnly ? "Nothing saved yet" : "No opportunities match these filters"}</strong><p>{savedOnly ? "Save an opportunity from the main feed and it will appear here." : "Try broadening the geography or stage filters."}</p></PortalState>}
  </section>;
}

export function CustomerOpportunityDetail({ api, id, vertical }) {
  const [item, setItem] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api(`/customer/opportunities/${id}?vertical=${vertical}`).then((value) => {
      setItem(value);
      api("/customer/events", { method: "POST", body: JSON.stringify({ event_type: "OPPORTUNITY_VIEWED", opportunity_id: id }) }).catch(() => {});
    }).catch((loadError) => setError(loadError.message || "Opportunity not found."));
  }, [api, id]);
  if (error) return <PortalState tone="error">{error}</PortalState>;
  if (!item) return <PortalState>Loading opportunity…</PortalState>;
  async function toggleSave() {
    await api(`/customer/opportunities/${id}/save?vertical=${vertical}`, { method: item.saved ? "DELETE" : "POST" });
    setItem({ ...item, saved: !item.saved });
  }
  return <section>
    <button className="care-back" onClick={() => go(`/care/opportunities?vertical=${vertical}`)}>← Back to opportunities</button>
    <div className="care-detail-hero"><div><div className="care-badges"><span>{item.change_label}</span><span>{item.stage_label}</span><span>{item.strength}</span></div><h1>{item.title}</h1><p className="care-location">{[item.local_authority || item.town, item.region, item.postcode].filter(Boolean).join(" · ") || "Location not yet published"}</p><p className="care-lead">{item.summary}</p></div><button className="care-primary" onClick={toggleSave}>{item.saved ? "★ Saved" : "☆ Save opportunity"}</button></div>
    <div className="care-detail-grid">
      <div className="care-panel"><h2>Why this matters</h2><p>{item.why}</p><dl className="care-facts"><div><dt>Operator/provider</dt><dd>{item.operator || "Not yet known"}</dd></div><div><dt>Change</dt><dd>{item.change_label}</dd></div><div><dt>Current stage</dt><dd>{item.stage_label}</dd></div><div><dt>First detected</dt><dd>{formatDate(item.first_detected)}</dd></div><div><dt>Latest update</dt><dd>{formatDate(item.last_updated)}</dd></div><div><dt>Location precision</dt><dd>{item.location_precision === "AREA_ONLY" ? "Area only — exact site is not shown" : "Published location"}</dd></div></dl></div>
      <div className="care-panel"><h2>Organisation</h2>{item.organisation ? <dl className="care-facts"><div><dt>Legal name</dt><dd>{item.organisation.legal_name}</dd></div><div><dt>Company number</dt><dd>{item.organisation.company_number || "—"}</dd></div><div><dt>Status</dt><dd>{item.organisation.status || "—"}</dd></div><div><dt>Incorporated</dt><dd>{formatDate(item.organisation.incorporation_date)}</dd></div></dl> : <p>Operator identity has not yet been confirmed.</p>}{item.organisation?.source_url && <a className="care-link" href={item.organisation.source_url} target="_blank" rel="noreferrer">View Companies House record ↗</a>}</div>
    </div>
    <div className="care-panel"><h2>Evidence timeline</h2><p className="care-muted">Public evidence, presented chronologically. CareProspect does not expose internal notes or raw provider data.</p><ol className="care-timeline">{item.evidence_timeline.map((entry, index) => <li key={`${entry.source_type}-${entry.date}-${index}`}><div className="timeline-dot"/><div><time>{formatDate(entry.date)}</time><h3>{entry.source_label}</h3><p>{entry.description}</p>{entry.source_title && <p className="care-muted">{entry.source_title}</p>}{entry.reference && <span className="care-reference">Reference: {entry.reference}</span>}{entry.source_url && <a href={entry.source_url} target="_blank" rel="noreferrer" onClick={() => api("/customer/events", { method: "POST", body: JSON.stringify({ event_type: "SOURCE_LINK_CLICKED", opportunity_id: id, metadata: { source_type: entry.source_type } }) }).catch(() => {})}>View official source ↗</a>}</div></li>)}</ol><p className="care-monitoring">{item.monitoring_message}</p></div>
  </section>;
}

export function AlertsPage({ api, account, vertical }) {
  const [preferences, setPreferences] = useState(null);
  const [digest, setDigest] = useState(null);
  const [savedSearches, setSavedSearches] = useState([]);
  const [searchName, setSearchName] = useState("");
  const [searchRegion, setSearchRegion] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    api("/customer/preferences").then(setPreferences).catch((e) => setError(e.message));
    if (account.entitlements.saved_searches) {
      api(`/customer/saved-searches?vertical=${vertical}`).then((value) => setSavedSearches(value.items || [])).catch((e) => setError(e.message));
    }
  }, [api, account.entitlements.saved_searches]);
  async function save(event) {
    event.preventDefault(); setError("");
    try { setPreferences(await api("/customer/preferences", { method: "PUT", body: JSON.stringify(preferences) })); setNotice("Alert preferences saved."); } catch (e) { setError(e.message); }
  }
  async function saveSearch(event) {
    event.preventDefault(); setError("");
    try {
      const value = await api("/customer/saved-searches", { method: "POST", body: JSON.stringify({ name: searchName, criteria: { region: searchRegion, vertical } }) });
      setSavedSearches([value, ...savedSearches]); setSearchName(""); setSearchRegion("");
      setNotice("Saved search created for future alerts.");
    } catch (e) { setError(e.message); }
  }
  if (!preferences) return <PortalState>{error || "Loading alert preferences…"}</PortalState>;
  const frequencies = account.entitlements.alert_frequencies || ["OFF", "WEEKLY"];
  return <section><div className="care-page-heading"><div><p className="care-eyebrow">Stay informed</p><h1>Alerts and preferences</h1><p>Choose how {vertical === "NURSERY" ? "NurserySignal" : "CareProspect"} should keep you updated.</p></div></div>{notice && <PortalState>{notice}</PortalState>}{error && <PortalState tone="error">{error}</PortalState>}<div className="care-detail-grid"><form className="care-panel care-form" onSubmit={save}><h2>Digest frequency</h2><label>Email frequency<select value={preferences.frequency} onChange={(e) => setPreferences({ ...preferences, frequency: e.target.value })}>{frequencies.map((value) => <option key={value} value={value}>{value === "OFF" ? "Off" : value[0] + value.slice(1).toLowerCase()}</option>)}</select></label><label>Regions<input value={(preferences.regions || []).join(", ")} onChange={(e) => setPreferences({ ...preferences, regions: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) })} placeholder="e.g. West Midlands" /></label><label>Local authorities<input value={(preferences.local_authorities || []).join(", ")} onChange={(e) => setPreferences({ ...preferences, local_authorities: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) })} /></label><button className="care-primary">Save preferences</button></form><div className="care-panel"><h2>Weekly digest preview</h2><p>Preview the customer-safe intelligence that will appear in your digest.</p><button className="care-secondary" onClick={() => api(`/customer/digest/preview?vertical=${vertical}`).then(setDigest).catch((e) => setError(e.message))}>Generate preview</button>{digest && <div className="digest-preview"><strong>{digest.subject}</strong><p>{digest.opportunities.length} recent opportunities</p><ul>{digest.opportunities.slice(0, 5).map((item) => <li key={item.id}>{item.title}</li>)}</ul>{digest.delivery_status === "PREVIEW_ONLY" && <p className="care-muted">Email delivery is awaiting a verified sender; preferences and digest generation are active.</p>}</div>}</div></div>{account.entitlements.saved_searches && <form className="care-panel care-form" onSubmit={saveSearch}><h2>Saved searches</h2><p className="care-muted">Keep a simple geography search ready for future digests.</p><label>Search name<input required value={searchName} onChange={(e) => setSearchName(e.target.value)} placeholder="West Midlands openings" /></label><label>Region<input required value={searchRegion} onChange={(e) => setSearchRegion(e.target.value)} placeholder="West Midlands" /></label><button className="care-secondary">Save search</button>{savedSearches.map((item) => <div className="saved-search-row" key={item.id}><strong>{item.name}</strong><span>{item.criteria?.region || "All permitted areas"}</span></div>)}</form>}</section>;
}

function AccountPage({ account }) {
  return <section><div className="care-page-heading"><div><p className="care-eyebrow">Your subscription</p><h1>{account.account_name}</h1><p>Account and CareProspect access details.</p></div></div><div className="care-panel"><dl className="care-facts"><div><dt>Plan</dt><dd>{account.plan}</dd></div><div><dt>Status</dt><dd>{account.account_status}</dd></div><div><dt>User</dt><dd>{account.display_name || account.email}</dd></div><div><dt>Coverage</dt><dd>{account.entitlements.nationwide ? "United Kingdom" : [...account.allowed_regions, ...account.allowed_local_authorities].join(", ") || "No geography assigned"}</dd></div><div><dt>Alerts</dt><dd>{account.entitlements.alert_frequencies.join(", ")}</dd></div></dl></div></section>;
}

export default function CustomerApp({ auth, path }) {
  const api = useMemo(() => useApi(auth.getToken, auth.logout), [auth.getToken, auth.logout]);
  const [account, setAccount] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const previousTitle = document.title;
    document.title = "SignalHub opportunities";
    return () => { document.title = previousTitle; };
  }, []);
  useEffect(() => { api("/customer/me").then((value) => { setAccount(value); api("/customer/events", { method: "POST", body: JSON.stringify({ event_type: "LOGIN" }) }).catch(() => {}); }).catch((e) => setError(e.message)); }, [api]);
  if (error) return <div className="care-login-error"><h1>SignalHub</h1><PortalState tone="error"><strong>Your account could not be loaded.</strong><p>{error}</p><button className="care-secondary" onClick={auth.logout}>Log out</button></PortalState></div>;
  if (!account) return <div className="care-loading">Loading opportunities…</div>;
  const route = path.startsWith("/care") ? path.split("?")[0] : "/care/opportunities";
  const vertical = new URLSearchParams(path.split("?")[1] || "").get("vertical") || account.allowed_verticals?.[0] || "CHILDRENS_HOME";
  const safeVertical = account.allowed_verticals?.includes(vertical) ? vertical : account.allowed_verticals?.[0] || "CHILDRENS_HOME";
  const changeVertical = (next) => go(`${route}?vertical=${next}`);
  const detail = route.match(/^\/care\/opportunities\/([^/]+)$/);
  return <CustomerShell account={account} user={auth.user} path={route} onLogout={auth.logout} vertical={safeVertical} onVerticalChange={changeVertical}>
    {detail ? <CustomerOpportunityDetail api={api} id={detail[1]} vertical={safeVertical} /> : route === "/care/saved" ? <OpportunityFeed api={api} savedOnly vertical={safeVertical} /> : route === "/care/alerts" ? <AlertsPage api={api} account={account} vertical={safeVertical} /> : route === "/care/account" ? <AccountPage account={account} /> : <OpportunityFeed api={api} vertical={safeVertical} />}
  </CustomerShell>;
}
