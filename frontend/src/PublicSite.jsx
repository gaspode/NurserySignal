import { useState } from "react";
import { apiRequest } from "./api.js";

export function CareProspectMark({ compact = false }) {
  return <span className={`cp-mark${compact ? " compact" : ""}`} aria-hidden="true" />;
}

export function CareProspectLogo({ compact = false, variant = "", tagline = "" }) {
  const classes = ["cp-wordmark", compact ? "compact" : "", variant ? `cp-wordmark-${variant}` : ""].filter(Boolean).join(" ");
  return <span className={classes} aria-label="CareProspect"><CareProspectMark compact={compact} /><span className="cp-logo-copy"><span aria-hidden="true">CareProspect</span>{tagline && <small>{tagline}</small>}</span></span>;
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

const registrationTiming = {
  axisMaxDays: 390,
  planning: { minDays: 96, q1Days: 228, medianDays: 319, q3Days: 366, maxDays: 389 },
  recruitment: { exampleDays: 97 },
  registration: { days: 0 },
  ticks: [
    { label: "13 months", daysLabel: "≈ 390 days", days: 390 },
    { label: "10 months", daysLabel: "≈ 300 days", days: 300 },
    { label: "7 months", daysLabel: "≈ 210 days", days: 210 },
    { label: "4 months", daysLabel: "≈ 120 days", days: 120 },
    { label: "1 month", daysLabel: "≈ 30 days", days: 30 },
    { label: "Ofsted registration", daysLabel: "0 days", days: 0 },
  ],
};

function timingPosition(days) {
  return `${((registrationTiming.axisMaxDays - days) / registrationTiming.axisMaxDays) * 100}%`;
}

function TimingSection() {
  const { planning, recruitment, registration, ticks } = registrationTiming;
  const observedLeft = timingPosition(planning.maxDays);
  const observedWidth = `${((planning.maxDays - planning.minDays) / registrationTiming.axisMaxDays) * 100}%`;
  const middleLeft = timingPosition(planning.q3Days);
  const middleWidth = `${((planning.q3Days - planning.q1Days) / registrationTiming.axisMaxDays) * 100}%`;
  return <section className="cp-lead-time" id="how-early">
    <div className="cp-section-intro"><p className="cp-kicker">The timing advantage</p><h2>Signals can appear months before registration</h2><p>CareProspect watches for the public evidence that appears as a new children’s home develops. Different signals can emerge at different stages, often long before the home reaches the Ofsted register.</p></div>
    <div className="cp-timing-scroll" aria-label="Signals before Ofsted registration timeline">
      <div className="cp-timing-chart">
        <div className="cp-timing-axis-label">Months before Ofsted registration</div>
        <div className="cp-timing-axis" aria-hidden="true">{ticks.map((tick) => <div className={`cp-timing-tick${tick.days === 30 ? " month-one" : ""}${tick.days === 0 ? " endpoint" : ""}`} key={tick.days} style={{ left: timingPosition(tick.days) }}><strong>{tick.label}</strong><span>{tick.daysLabel}</span></div>)}</div>
        <div className="cp-timing-grid" aria-hidden="true">{ticks.map((tick) => <i className={tick.days === 30 ? "month-one" : tick.days === 0 ? "endpoint" : ""} key={tick.days} style={{ left: timingPosition(tick.days) }} />)}</div>

        <div className="cp-timing-label planning"><strong>Planning</strong><span>Planning applications and related council records</span></div>
        <div className="cp-timing-plot planning" aria-label="Planning evidence was observed from 389 to 96 days before registration, with a middle 50 percent range of 228 to 366 days and a median of 319 days.">
          <div className="cp-planning-range" style={{ left: observedLeft, width: observedWidth }}><span className="cp-range-start">389 days<small>Earliest observed</small></span><span className="cp-range-end">96 days<small>Latest observed</small></span></div>
          <div className="cp-planning-middle" style={{ left: middleLeft, width: middleWidth }}><span>Middle 50%<small>228–366 days</small></span></div>
          <div className="cp-planning-median" style={{ left: timingPosition(planning.medianDays) }}><i /><strong>Median</strong><span>319 days</span></div>
        </div>

        <div className="cp-timing-label recruitment"><strong>Recruitment signal</strong><span>Job advert / recruitment evidence</span></div>
        <div className="cp-timing-plot recruitment" aria-label="A recruitment signal appeared 97 days before registration."><div className="cp-recruitment-point" style={{ left: timingPosition(recruitment.exampleDays) }}><i /><strong>97 days</strong><span>Recruitment example</span></div></div>

        <div className="cp-timing-label registration"><strong>Ofsted registration</strong><span>Home appears on the Ofsted register</span></div>
        <div className="cp-timing-plot registration" aria-label={`Ofsted registration is the endpoint at ${registration.days} days.`}><div className="cp-registration-point"><i /><strong>Ofsted registration</strong><span>0 days</span></div></div>
      </div>
    </div>
    <div className="cp-timing-highlights" aria-label="Timing highlights">
      <article><span>Observed planning range</span><strong>96–389 days</strong><small>before registration</small></article>
      <article><span>Planning median</span><strong>319 days</strong><small>before registration</small></article>
      <article className="recruitment"><span>Recruitment example</span><strong>97 days</strong><small>before registration</small></article>
    </div>
    <p className="cp-benchmark-note">Based on reconstructed historical registrations. Timing varies, and not every home produces every type of signal.</p>
  </section>;
}

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
  return <header className="cp-public-header"><a className="cp-brand-link" href="#/" aria-label="CareProspect home"><CareProspectLogo /></a><nav aria-label="Main navigation"><a href="#how-early">How early</a><a href="#how-it-works">How it works</a><a href="#who-its-for">Who it’s for</a><a href="#pricing">Pricing</a><a href="#faq">FAQ</a></nav><div className="cp-header-actions"><button className="cp-sign-in" onClick={() => go("/care/login")}>Sign in</button><a className="cp-button primary" href="#request-access">Request access</a></div></header>;
}

function Footer() {
  return <footer className="cp-footer"><div><CareProspectLogo variant="footer" /><p>Early intelligence on new children’s homes.</p><small>A product of Mugwump.net Ltd</small></div><nav aria-label="Footer navigation"><button onClick={() => go("/privacy")}>Privacy</button><button onClick={() => go("/terms")}>Terms</button><a href="#request-access">Contact</a><button onClick={() => go("/care/login")}>Sign in</button></nav></footer>;
}

export function PublicLegalPage({ page }) {
  const privacy = page === "privacy";
  return <div className="cp-public"><PublicHeader /><main className="cp-legal"><p className="cp-kicker">CareProspect</p><h1>{privacy ? "Privacy" : "Pilot terms"}</h1>{privacy ? <><p>CareProspect processes access-request and customer-account information to respond to enquiries, provide invited access and operate product alerts. We do not sell personal data.</p><p>The product uses official public organisation-level evidence. It does not reconstruct redacted children’s-home addresses or collect resident information.</p><p>For a privacy question, use the request-access form and select the closest supplier category; explain your request in the message field.</p></> : <><p>CareProspect is currently offered through invited commercial pilots. Coverage, alert frequency, geography and price are agreed with each participating organisation.</p><p>Opportunity evidence is provided for sales research. It is not a guarantee that a home will open, that a supplier decision remains available, or that coverage is exhaustive.</p><p>Full pilot terms are supplied before paid access is activated.</p></>}<button className="cp-button secondary" onClick={() => go("/")}>← Back to CareProspect</button></main><Footer /></div>;
}

export default function PublicSite() {
  return <div className="cp-public"><PublicHeader /><main>
    <section className="cp-hero"><div className="cp-hero-copy"><p className="cp-kicker">Early commercial intelligence for children’s-home suppliers</p><h1>Find new children’s homes before they appear on the register.</h1><p className="cp-lede">CareProspect follows planning, recruitment and regulatory evidence and links it into one opportunity, while supplier decisions may still be open.</p><div className="cp-hero-actions"><a className="cp-button primary" href="#request-access">Request access</a><a className="cp-button secondary" href="#product-example">See an example</a></div><p className="cp-trust-note"><span aria-hidden="true">◇</span> Public official sources only. Redacted addresses are never reconstructed.</p></div><div className="cp-hero-preview" id="product-example"><div className="cp-preview-layer back" /><div className="cp-preview-layer middle" /><ExampleOpportunity /></div></section>

    <TimingSection />

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
