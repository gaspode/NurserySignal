import { useState } from "react";
import { apiRequest } from "./api.js";

export function CareProspectMark({ compact = false }) {
  return <span className={`cp-mark${compact ? " compact" : ""}`} aria-hidden="true">
    <svg viewBox="0 0 58 30" focusable="false">
      <path d="M5 23 28 16 51 7" />
      <circle className="planning" cx="7" cy="22" r="4" />
      <rect className="recruitment" x="24" y="12" width="8" height="8" />
      <path className="registration" d="m51 2 6 6-6 6-6-6Z" />
    </svg>
  </span>;
}

function Brand() {
  return <span className="cp-wordmark"><CareProspectMark /><span>CareProspect</span></span>;
}

function go(path) {
  window.location.hash = path;
  window.scrollTo?.({ top: 0 });
}

const opportunityExamples = [
  { place: "Wolverhampton WV1", stage: "Planning", tone: "planning", detected: "14 Feb", strength: "Developing" },
  { place: "Liverpool L19", stage: "Recruiting", tone: "recruitment", detected: "03 Apr", strength: "Strong" },
  { place: "Sandwell B43", stage: "Registration", tone: "registration", detected: "22 May", strength: "Confirmed" },
];

function StageBadge({ tone, children }) {
  return <span className={`cp-stage cp-stage-${tone}`}><span aria-hidden="true" />{children}</span>;
}

function ExampleOpportunity({ detailed = false }) {
  return <article className={`cp-example-record${detailed ? " detailed" : ""}`}>
    <div className="cp-example-kicker"><span>Example opportunity</span><button type="button" aria-label="Example opportunity saved">★ Saved</button></div>
    <h3>New children’s home — Liverpool L19</h3>
    <p className="cp-example-location">Liverpool · Merseyside · L19</p>
    <div className="cp-example-badges"><StageBadge tone="planning">Planning</StageBadge><span>Opening</span><span>Developing evidence</span></div>
    <p className="cp-example-summary">A planning application explicitly proposes a new children’s home. The operator is not yet confirmed.</p>
    <dl>
      <div><dt>First detected</dt><dd>14 February 2026</dd></div>
      <div><dt>Latest update</dt><dd>Planning evidence added</dd></div>
      <div><dt>Operator</dt><dd>Not yet known</dd></div>
      <div><dt>Evidence</dt><dd>Official planning record</dd></div>
    </dl>
    <ol className="cp-mini-timeline">
      <li className="planning"><time>14 Feb</time><span>Planning application identified</span></li>
      <li className="latest"><time>Monitoring</time><span>Latest public evidence checked</span></li>
    </ol>
    <a href="https://planningportal.co.uk/" target="_blank" rel="noreferrer">View example source link ↗</a>
  </article>;
}

function RequestAccessForm() {
  const [form, setForm] = useState({ name: "", company: "", email: "", supplier_category: "", message: "", website: "" });
  const [state, setState] = useState({ busy: false, error: "", sent: false });
  async function submit(event) {
    event.preventDefault();
    setState({ busy: true, error: "", sent: false });
    try {
      await apiRequest("/public/access-requests", { method: "POST", body: JSON.stringify(form) });
      setState({ busy: false, error: "", sent: true });
      setForm({ name: "", company: "", email: "", supplier_category: "", message: "", website: "" });
    } catch {
      setState({ busy: false, error: "We couldn’t send your request. Please check the form and try again.", sent: false });
    }
  }
  return <form className="cp-access-form" onSubmit={submit}>
    <div><label>Name<input required autoComplete="name" maxLength="120" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label><label>Company<input required autoComplete="organization" maxLength="180" value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} /></label></div>
    <div><label>Work email<input required type="email" autoComplete="email" maxLength="254" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></label><label>Supplier category<select required value={form.supplier_category} onChange={(e) => setForm({ ...form, supplier_category: e.target.value })}><option value="">Choose one</option><option value="FIT_OUT_FURNITURE">Fit-out & furniture</option><option value="RECRUITMENT">Recruitment</option><option value="TRAINING">Training</option><option value="SOFTWARE">Software</option><option value="TELECOMS">Telecoms</option><option value="SECURITY">Security</option><option value="PROPERTY_SERVICES">Property services</option><option value="VEHICLES">Vehicles</option><option value="CATERING">Catering</option><option value="COMPLIANCE">Compliance</option><option value="OTHER">Other</option></select></label></div>
    <label>What would make CareProspect useful to you? <span>(optional)</span><textarea maxLength="1500" value={form.message} onChange={(e) => setForm({ ...form, message: e.target.value })} /></label>
    <label className="cp-honeypot" aria-hidden="true">Website<input tabIndex="-1" autoComplete="off" value={form.website} onChange={(e) => setForm({ ...form, website: e.target.value })} /></label>
    {state.error && <p className="cp-form-error" role="alert">{state.error}</p>}
    {state.sent && <p className="cp-form-success" role="status"><strong>Thank you.</strong> Your pilot request has been received. We’ll contact you using the work email supplied.</p>}
    <button className="cp-button primary" disabled={state.busy}>{state.busy ? "Sending…" : "Request access"}</button>
  </form>;
}

function PublicHeader() {
  return <header className="cp-public-header"><a className="cp-brand-link" href="#/" aria-label="CareProspect home"><Brand /></a><nav aria-label="Main navigation"><a href="#how-early">How early</a><a href="#how-it-works">How it works</a><a href="#who-its-for">Who it’s for</a><a href="#pricing">Pricing</a><a href="#faq">FAQ</a></nav><div className="cp-header-actions"><button className="cp-sign-in" onClick={() => go("/care/login")}>Sign in</button><a className="cp-button primary" href="#request-access">Request access</a></div></header>;
}

function Footer() {
  return <footer className="cp-footer"><div><Brand /><p>Early intelligence on new children’s homes.</p><small>A product of Mugwump.net Ltd</small></div><nav aria-label="Footer navigation"><button onClick={() => go("/privacy")}>Privacy</button><button onClick={() => go("/terms")}>Terms</button><a href="#request-access">Contact</a><button onClick={() => go("/care/login")}>Sign in</button></nav></footer>;
}

export function PublicLegalPage({ page }) {
  const privacy = page === "privacy";
  return <div className="cp-public"><PublicHeader /><main className="cp-legal"><p className="cp-kicker">CareProspect</p><h1>{privacy ? "Privacy" : "Pilot terms"}</h1>{privacy ? <><p>CareProspect processes access-request and customer-account information to respond to enquiries, provide invited access and operate product alerts. We do not sell personal data.</p><p>The product uses official public organisation-level evidence. It does not reconstruct redacted children’s-home addresses or collect resident information.</p><p>For a privacy question, use the request-access form and select the closest supplier category; explain your request in the message field.</p></> : <><p>CareProspect is currently offered through invited commercial pilots. Coverage, alert frequency, geography and price are agreed with each participating organisation.</p><p>Opportunity evidence is provided for sales research. It is not a guarantee that a home will open, that a supplier decision remains available, or that coverage is exhaustive.</p><p>Full pilot terms are supplied before paid access is activated.</p></>}<button className="cp-button secondary" onClick={() => go("/")}>← Back to CareProspect</button></main><Footer /></div>;
}

export default function PublicSite() {
  return <div className="cp-public"><PublicHeader /><main>
    <section className="cp-hero"><div className="cp-hero-copy"><p className="cp-kicker">Early commercial intelligence for children’s-home suppliers</p><h1>Find new children’s homes before they appear on the register.</h1><p className="cp-lede">CareProspect follows planning, recruitment and regulatory evidence and links it into one opportunity, while supplier decisions may still be open.</p><div className="cp-hero-actions"><a className="cp-button primary" href="#request-access">Request access</a><a className="cp-button secondary" href="#product-example">See an example</a></div><p className="cp-trust-note"><span aria-hidden="true">◇</span> Public official sources only. Redacted addresses are never reconstructed.</p></div><div className="cp-hero-preview" id="product-example"><div className="cp-preview-layer back" /><div className="cp-preview-layer middle" /><ExampleOpportunity /></div></section>

    <section className="cp-lead-time" id="how-early"><div className="cp-section-intro"><p className="cp-kicker">The timing advantage</p><h2>Registration is the last signal, not the first</h2><p>Each bar is one historical benchmark case, measured from the first evidence we detected to Ofsted registration.</p></div><div className="cp-bars">{[{ days: 348, source: "Planning", width: 100, tone: "planning" }, { days: 290, source: "Planning", width: 83, tone: "planning" }, { days: 207, source: "Planning", width: 59, tone: "planning" }, { days: 97, source: "Recruitment", width: 28, tone: "recruitment" }].map((item) => <div className="cp-bar-row" key={item.days}><span>{item.days} days</span><div><i className={item.tone} style={{ width: `${item.width}%` }} /><b>{item.source}</b></div></div>)}</div><p className="cp-benchmark-note">Individual cases from a small historical sample. Not an average, and not a promise about future openings.</p></section>

    <section className="cp-product-section" id="how-it-works"><div className="cp-section-intro"><p className="cp-kicker">What you receive</p><h2>An evidence-led opportunity feed, not another list of records</h2><p>Filter by commercial relevance, follow an opportunity as it develops and open the official evidence behind it.</p></div><div className="cp-product-demo"><div className="cp-feed-example"><div className="cp-demo-filters"><span>Search opportunities</span><span>Region</span><span>Stage</span></div>{opportunityExamples.map((item) => <article key={item.place}><div><StageBadge tone={item.tone}>{item.stage}</StageBadge><small>Example</small></div><h3>New children’s home — {item.place}</h3><p>First detected {item.detected} · {item.strength} evidence</p><button type="button">☆ Watch</button></article>)}<aside><strong>Alerts</strong><span>New planning evidence · Liverpool</span><span>Operator identified · Wolverhampton</span></aside></div><ExampleOpportunity detailed /></div></section>

    <section className="cp-benefits"><div className="cp-section-intro"><p className="cp-kicker">Why earlier intelligence matters</p><h2>More time to start the right conversation</h2></div><ul><li>Reach prospects before competitors</li><li>Contact operators while supplier decisions may still be open</li><li>Spend less time checking planning sites and job boards</li><li>Follow opportunities as they develop</li><li>Focus sales teams on higher-intent prospects</li></ul></section>

    <section className="cp-audience" id="who-its-for"><div className="cp-section-intro"><p className="cp-kicker">Who it’s for</p><h2>Built for suppliers to children’s homes</h2><p>CareProspect is built for businesses that sell into children’s homes, helping sales teams identify opportunities earlier.</p></div><div className="cp-category-grid">{["Fit-out & furniture", "Recruitment", "Training", "Software", "Telecoms", "Security", "Property services", "Vehicles", "Catering", "Compliance"].map((name, index) => <span key={name} className={["planning", "recruitment", "registration"][index % 3]}>{name}</span>)}</div></section>

    <section className="cp-linking"><div className="cp-section-intro"><p className="cp-kicker">Detect · Link · Alert</p><h2>Signals in, one opportunity out</h2><p>CareProspect does not dump isolated public records. It links them into one evolving commercial opportunity with a single evidence timeline.</p></div><div className="cp-link-flow"><div className="cp-signal-list"><h3>Detect</h3><span>Planning application</span><span>Recruitment advert</span><span>Ofsted evidence</span><span>Companies House record</span></div><div className="cp-flow-arrow" aria-hidden="true">→</div><div className="cp-linked-card"><small>LINK · ONE OPPORTUNITY</small><h3>New children’s home — Liverpool L19</h3><p>One evidence timeline<br />Consolidated context<br />Operator identity when known</p></div><div className="cp-flow-arrow" aria-hidden="true">→</div><div className="cp-alert-list"><h3>Alert</h3><span>New evidence added</span><span>Operator identified</span><span>Registration confirmed</span></div></div></section>

    <section className="cp-evidence"><div className="cp-section-intro"><p className="cp-kicker">Evidence and trust</p><h2>How we handle evidence</h2></div><div><article><h3>What we show</h3><ul><li>Official public evidence</li><li>Why each opportunity exists</li><li>Links to the source record</li><li>Updates over time</li></ul></article><article><h3>What we don’t do</h3><ul><li>Invent or reconstruct addresses</li><li>Hide where evidence came from</li><li>Claim certainty</li><li>Claim exhaustive coverage</li></ul></article></div></section>

    <section className="cp-pricing" id="pricing"><div className="cp-section-intro"><p className="cp-kicker">Indicative pilot pricing</p><h2>Choose the coverage your sales team needs</h2></div><div className="cp-price-grid"><article><h3>Starter</h3><p className="cp-price">£79 <span>/ month</span></p><ul><li>Selected geography</li><li>Opportunity feed</li><li>Weekly digest</li><li>Watchlist</li></ul><a href="#request-access" className="cp-button secondary">Request access</a></article><article className="featured"><small>Best for nationwide sales teams</small><h3>Pro</h3><p className="cp-price">£149 <span>/ month</span></p><ul><li>Nationwide coverage</li><li>Faster alerts</li><li>Richer filters and saved searches</li><li>Full evidence timeline</li></ul><a href="#request-access" className="cp-button primary">Request access</a></article><article><h3>Business</h3><p className="cp-price">Contact us</p><ul><li>Custom exports</li><li>Integrations</li><li>Wider team use</li></ul><a href="#request-access" className="cp-button secondary">Talk to us</a></article></div></section>

    <section className="cp-faq" id="faq"><div className="cp-section-intro"><p className="cp-kicker">Questions</p><h2>FAQ</h2></div><div>{[
      ["What counts as an opportunity?", "Evidence of a plausible new opening, expansion, relocation or other material change—not routine activity alone."],
      ["How early can CareProspect detect a new home?", "Timing varies. Planning and recruitment can appear before registration, but no lead time is guaranteed."],
      ["Where does the data come from?", "Official public planning, recruitment, Companies House and Ofsted evidence, linked and reviewed by CareProspect."],
      ["Does CareProspect show exact home addresses?", "Only where a legitimate public source supports customer-safe location detail. We never reconstruct Ofsted-redacted addresses."],
      ["Is this just Ofsted data?", "No. Ofsted is usually a later regulatory signal; CareProspect also follows earlier planning and recruitment evidence."],
      ["How often is the data updated?", "Core sources are monitored regularly and customer records show both first detection and the latest meaningful update."],
      ["Can I choose regions?", "Yes. Starter pilots can be configured for selected regions or local authorities; Pro provides nationwide coverage."],
      ["Can I cancel?", "Pilot terms, including cancellation, are agreed clearly before paid access is activated."],
      ["Is there a trial or pilot?", "We are inviting a small number of suppliers into paid pilots. Request access to discuss fit and coverage."],
    ].map(([question, answer]) => <details key={question}><summary>{question}</summary><p>{answer}</p></details>)}</div></section>

    <section className="cp-access" id="request-access"><div><p className="cp-kicker">Request access</p><h2>See whether CareProspect fits your sales territory</h2><p>Tell us what you supply and where you sell. We’ll use this information only to respond about a CareProspect pilot.</p></div><RequestAccessForm /></section>
  </main><Footer /></div>;
}
