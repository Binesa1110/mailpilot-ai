import { useCallback, useEffect, useState } from "react";
import {
  ArrowUpRight,
  Check,
  CheckCheck,
  ChevronDown,
  CircleHelp,
  Clipboard,
  Clock3,
  Feather,
  FileText,
  Flag,
  History,
  Lightbulb,
  LoaderCircle,
  LogOut,
  MailCheck,
  MessageSquareText,
  RotateCcw,
  Send,
  ShieldCheck,
  Sparkles,
  WandSparkles,
} from "lucide-react";

const initialForm = {
  recipient: "",
  purpose: "",
  context: "",
  category: "Professional",
  tone: "Warm & professional",
  language: "English",
  signature: "",
};

const categories = [
  "Professional",
  "Formal",
  "Academic",
  "Business",
  "Personal",
  "Job application",
  "Complaint",
  "Apology",
  "Follow-up",
];
const tones = ["Warm & professional", "Formal", "Friendly", "Confident", "Empathetic", "Concise"];
const languages = ["English", "Spanish", "French", "German", "Portuguese", "Hindi", "Japanese"];

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || "Something went wrong. Please try again.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function FieldLabel({ children, optional = false }) {
  return (
    <span className="field-label">
      {children}
      {optional && <span className="optional">Optional</span>}
    </span>
  );
}

function SelectField({ label, value, options, onChange }) {
  return (
    <label className="field">
      <FieldLabel>{label}</FieldLabel>
      <span className="select-wrap">
        <select value={value} onChange={(event) => onChange(event.target.value)}>
          {options.map((option) => <option key={option}>{option}</option>)}
        </select>
        <ChevronDown size={16} aria-hidden="true" />
      </span>
    </label>
  );
}

function statusLabel(status) {
  return {
    draft: "Draft",
    approved: "Approved",
    sending: "Sending",
    sent: "Sent",
    failed: "Send failed",
    uncertain: "Outcome uncertain",
  }[status] || status;
}

export default function App() {
  const [form, setForm] = useState(initialForm);
  const [result, setResult] = useState(null);
  const [history, setHistory] = useState([]);
  const [autoSettings, setAutoSettings] = useState(null);
  const [autoModalOpen, setAutoModalOpen] = useState(false);
  const [autoRecipients, setAutoRecipients] = useState("");
  const [autoCategories, setAutoCategories] = useState([]);
  const [autoDailyLimit, setAutoDailyLimit] = useState(5);
  const [autoAcknowledged, setAutoAcknowledged] = useState(false);
  const [autoConfirmPhrase, setAutoConfirmPhrase] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [auth, setAuth] = useState(null);
  const [authLoaded, setAuthLoaded] = useState(false);
  const [approved, setApproved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  const refreshHistory = useCallback(async () => {
    const items = await api("/api/drafts");
    setHistory(items);
    return items;
  }, []);

  const refreshAuth = useCallback(async () => {
    try {
      const session = await api("/api/auth/session");
      setAuth(session);
      return session;
    } catch (requestError) {
      setAuth(null);
      if (requestError.status !== 401) setError(requestError.message);
      return null;
    } finally {
      setAuthLoaded(true);
    }
  }, []);

  const refreshAutoSettings = useCallback(async () => {
    const settings = await api("/api/settings/auto-send");
    setAutoSettings(settings);
    setAutoRecipients(settings.recipient_allowlist.join(", "));
    setAutoCategories(settings.categories);
    setAutoDailyLimit(settings.daily_limit);
    return settings;
  }, []);

  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);

  useEffect(() => {
    if (auth) refreshHistory().catch((requestError) => setError(requestError.message));
    else setHistory([]);
  }, [auth, refreshHistory]);

  useEffect(() => {
    if (auth) refreshAutoSettings().catch((requestError) => setError(requestError.message));
    else setAutoSettings(null);
  }, [auth, refreshAutoSettings]);

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const updateResult = (key, value) => {
    setResult((current) => ({
      ...current,
      [key]: value,
      ...(current.status === "approved" ? { status: "draft" } : {}),
    }));
    setApproved(false);
    setError("");
  };
  const canGenerate = form.purpose.trim().length > 0 && form.context.trim().length > 0;

  async function generateEmail(event) {
    event.preventDefault();
    if (!canGenerate || busy) return;
    setBusy(true);
    setError("");
    setCopied(false);
    setResult(null);
    setApproved(false);
    try {
      const data = await api("/api/emails/generate", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(form),
      });
      setResult(data);
      await refreshHistory();
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function approveEmail() {
    if (!result || busy || sending) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/drafts/${result.id}/approve`, {
        method: "POST",
        body: JSON.stringify({
          recipient: result.recipient,
          subject: result.subject,
          body: result.body,
        }),
      });
      setApproved(true);
      setResult((current) => ({ ...current, status: "approved" }));
      await refreshHistory();
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  }

  async function sendEmail() {
    if (!result || !approved || sending || !auth?.gmail_connected) return;
    setSending(true);
    setError("");
    try {
      const data = await api(`/api/drafts/${result.id}/send`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      setResult((current) => ({
        ...current,
        status: "sent",
        message_id: data.message_id,
        sent_at: data.sent_at,
        send_authorization: data.send_authorization,
        policy_decision: data.policy_decision,
      }));
      setApproved(false);
      await refreshHistory();
    } catch (requestError) {
      setError(requestError.message);
      setApproved(false);
      try {
        const items = await refreshHistory();
        const saved = items.find((item) => item.id === result.id);
        if (saved) setResult((current) => ({ ...current, status: saved.status, message_id: saved.provider_message_id }));
      } catch {
        // Keep the provider error visible if history refresh is unavailable.
      }
    } finally {
      setSending(false);
    }
  }

  async function copyEmail() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(`To: ${result.recipient}\nSubject: ${result.subject}\n\n${result.body}`);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Clipboard access was blocked. Select and copy the email instead.");
    }
  }

  function reset() {
    setForm(initialForm);
    setResult(null);
    setError("");
    setCopied(false);
    setApproved(false);
  }

  async function signOut() {
    try {
      await api("/api/auth/logout", { method: "POST" });
      setAuth(null);
      setResult(null);
      setError("");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function disconnectGmail() {
    try {
      await api("/api/auth/gmail", { method: "DELETE" });
      await refreshAuth();
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function enableAutoSend(event) {
    event.preventDefault();
    const recipients = autoRecipients.split(/[,\n;]/).map((value) => value.trim()).filter(Boolean);
    setSettingsBusy(true);
    setError("");
    try {
      const settings = await api("/api/settings/auto-send/enable", {
        method: "POST",
        body: JSON.stringify({
          confirm_phrase: autoConfirmPhrase,
          consequences_acknowledged: autoAcknowledged,
          recipient_allowlist: recipients,
          categories: autoCategories,
          daily_limit: Number(autoDailyLimit),
        }),
      });
      setAutoSettings(settings);
      setAutoModalOpen(false);
      setAutoAcknowledged(false);
      setAutoConfirmPhrase("");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setSettingsBusy(false);
    }
  }

  async function disableAutoSend() {
    setSettingsBusy(true);
    setError("");
    try {
      const settings = await api("/api/settings/auto-send/disable", { method: "POST" });
      setAutoSettings((current) => ({ ...current, ...settings }));
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setSettingsBusy(false);
    }
  }

  async function engageKillSwitch() {
    const confirmed = window.confirm("Stop automatic sending for every account now? This is a global emergency stop.");
    if (!confirmed) return;
    setSettingsBusy(true);
    setError("");
    try {
      await api("/api/settings/auto-send/kill-switch", { method: "POST" });
      await refreshAutoSettings();
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setSettingsBusy(false);
    }
  }

  function toggleAutoCategory(category) {
    const value = category.toLowerCase();
    setAutoCategories((current) => (
      current.includes(value) ? current.filter((entry) => entry !== value) : [...current, value]
    ));
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#" aria-label="MailPilot AI home">
          <span className="brand-mark"><Feather size={20} strokeWidth={2.2} /></span>
          <span>mailpilot<span className="brand-ai">.ai</span></span>
        </a>
        <div className="topbar-right">
          <span className="private-badge"><ShieldCheck size={14} /> Private by design</span>
          {auth && autoSettings && (
            <span className={`connection-badge ${autoSettings.enabled && !autoSettings.kill_switch_enabled ? "connected" : ""}`}>
              <ShieldCheck size={14} /> {autoSettings.kill_switch_enabled ? "Auto-send stopped" : autoSettings.enabled ? "Auto-send on" : "Approval required"}
            </span>
          )}
          {authLoaded && auth ? (
            <>
              <span className={`connection-badge ${auth.gmail_connected ? "connected" : ""}`}>
                <MailCheck size={14} /> {auth.gmail_connected ? auth.account_email : "Gmail not connected"}
              </span>
              {auth.gmail_connected ? (
                <button className="text-button" type="button" onClick={disconnectGmail}>Disconnect</button>
              ) : (
                <a className="text-button" href="/api/auth/google/start">Connect Gmail</a>
              )}
              <button className="icon-button" type="button" aria-label="Sign out" title="Sign out" onClick={signOut}><LogOut size={17} /></button>
            </>
          ) : authLoaded ? (
            <a className="connect-button" href="/api/auth/google/start">Sign in with Google</a>
          ) : <span className="private-badge">Checking account…</span>}
          <button className="icon-button help-button" aria-label="Help" title="Auto-send is off unless you explicitly opt in">
            <CircleHelp size={18} />
          </button>
        </div>
      </header>

      <main className="workspace">
        <section className="intro">
          <div className="eyebrow"><span className="eyebrow-dot" /> YOUR THOUGHTS, WELL EXPRESSED</div>
          <h1>Every email, a little <span>more effortless.</span></h1>
          <p>Tell us what’s on your mind. We’ll help you find just the right words.</p>
        </section>

        {!auth && authLoaded && (
          <div className="auth-notice"><ShieldCheck size={17} /><span>Sign in with Google to save drafts and connect Gmail. Auto-send remains off unless you deliberately enable it; otherwise you approve every message.</span><a href="/api/auth/google/start">Sign in</a></div>
        )}

        {auth && autoSettings && (
          <section className="auto-settings-card card">
            <div className="auto-settings-main">
              <div className="auto-settings-copy">
                <div className="section-mini-title"><ShieldCheck size={15} /> SENDING PERMISSIONS</div>
                <h2>{autoSettings.kill_switch_enabled ? "Automatic sending is globally stopped" : autoSettings.enabled ? "Automatic sending is enabled for eligible drafts" : "Approval required for every email"}</h2>
                <p>
                  {autoSettings.enabled
                    ? `${autoSettings.sent_today} of ${autoSettings.daily_limit} automatic sends used today. Only allowlisted recipients and categories can pass the deterministic safety policy.`
                    : "Auto-send is off unless you deliberately opt in. Risky or uncertain drafts always require your individual approval."}
                </p>
                <div className="auto-permissions-summary">
                  <span><b>Recipients:</b> {autoSettings.recipient_allowlist.length ? autoSettings.recipient_allowlist.join(", ") : "None allowlisted"}</span>
                  <span><b>Categories:</b> {autoSettings.categories.length ? autoSettings.categories.join(", ") : "None allowed"}</span>
                  <span><b>Limit:</b> {autoSettings.daily_limit} per day</span>
                </div>
              </div>
              <div className="auto-settings-actions">
                {autoSettings.enabled ? (
                  <>
                    <button className="secondary-button" type="button" disabled={settingsBusy || autoSettings.kill_switch_enabled} onClick={() => setAutoModalOpen(true)}>Edit allowlist and limits</button>
                    <button className="secondary-button revoke-button" type="button" disabled={settingsBusy} onClick={disableAutoSend}>Disable auto-send now</button>
                  </>
                ) : (
                  <button className="approve-button" type="button" disabled={settingsBusy || autoSettings.kill_switch_enabled || !autoSettings.gmail_connected} onClick={() => setAutoModalOpen(true)}>Configure auto-send</button>
                )}
                {autoSettings.can_engage_kill_switch && <button className="secondary-button kill-button" type="button" disabled={settingsBusy || autoSettings.kill_switch_enabled} onClick={engageKillSwitch}>Emergency stop all auto-send</button>}
                {!autoSettings.gmail_connected && <span className="send-hint">Connect Gmail before opting in.</span>}
              </div>
            </div>
            {autoSettings.kill_switch_enabled && <div className="uncertain-notice">Global kill switch active. Auto-send cannot be re-enabled until an operator clears the emergency stop.</div>}
          </section>
        )}

        <div className="workbench">
          <form className="composer card" onSubmit={generateEmail}>
            <div className="card-heading">
              <div className="heading-icon purple"><MessageSquareText size={18} /></div>
              <div>
                <h2>Let’s write your email</h2>
                <p>A few details are all we need to get started.</p>
              </div>
            </div>
            <div className="form-grid">
              <label className="field full-width">
                <FieldLabel>Who are you writing to?</FieldLabel>
                <input value={form.recipient} onChange={(event) => update("recipient", event.target.value)} placeholder="e.g. Alex, Hiring Manager, Professor Lee" maxLength={200} />
              </label>
              <SelectField label="Email category" value={form.category} options={categories} onChange={(value) => update("category", value)} />
              <SelectField label="Tone of voice" value={form.tone} options={tones} onChange={(value) => update("tone", value)} />
              <label className="field full-width">
                <FieldLabel>What’s the purpose?</FieldLabel>
                <input required value={form.purpose} onChange={(event) => update("purpose", event.target.value)} placeholder="e.g. Ask to reschedule our Thursday meeting" maxLength={500} />
              </label>
              <label className="field full-width">
                <FieldLabel>Anything else we should know?</FieldLabel>
                <textarea required value={form.context} onChange={(event) => update("context", event.target.value)} placeholder="Share the details, background, or key points you’d like to include…" rows={4} maxLength={5000} />
                <span className="textarea-meta">{form.context.length}/5,000</span>
              </label>
              <SelectField label="Write in" value={form.language} options={languages} onChange={(value) => update("language", value)} />
              <label className="field">
                <FieldLabel optional>Your sign-off</FieldLabel>
                <input value={form.signature} onChange={(event) => update("signature", event.target.value)} placeholder="e.g. Best, Jamie" maxLength={300} />
              </label>
            </div>
            {error && <div className="error-message" role="alert">{error}</div>}
            <div className="form-footer">
              <span className="privacy-note"><ShieldCheck size={15} /> Review, approve, then send yourself.</span>
              <button className="primary-button" type="submit" disabled={!canGenerate || busy || !auth}>
                {busy && !result ? <><LoaderCircle className="spin" size={17} /> Writing & reviewing…</> : <><Sparkles size={17} /> Write my email <ArrowUpRight size={16} /></>}
              </button>
            </div>
          </form>

          <section className={`result-card card ${result ? "has-result" : ""}`} aria-live="polite">
            {!result && !busy ? (
              <div className="empty-state">
                <div className="empty-illustration">
                  <div className="illustration-glow" />
                  <div className="paper"><div className="paper-lines"><i /><i /><i /></div><span className="paper-spark"><Sparkles size={17} /></span></div>
                </div>
                <h2>Your next great email starts here</h2>
                <p>Once you share a little context, your carefully crafted draft will appear right here.</p>
                <div className="empty-features"><span><Check size={14} /> Thoughtfully written</span><span><Check size={14} /> Reviewed for you</span></div>
                <div className="tip"><Lightbulb size={17} /><span><b>A little detail goes a long way.</b><br />Mention the key points you want to get across.</span></div>
              </div>
            ) : busy && !result ? (
              <div className="loading-state">
                <div className="loading-orb"><WandSparkles size={24} /></div>
                <h2>Finding just the right words</h2>
                <p>Writing your draft, then giving it a careful review…</p>
                <div className="loading-steps"><span className="active"><Check size={13} /> Drafting</span><span className="step-arrow" /><span className="active"><LoaderCircle className="spin" size={13} /> Reviewing</span></div>
              </div>
            ) : result ? (
              <>
                <div className="result-heading">
                  <div className="heading-icon green"><CheckCheck size={18} /></div>
                  <div><h2>{result.status === "sent" ? "Email sent" : "Your email is ready"}</h2><p>Reviewed and saved. You remain in control.</p></div>
                  <span className={`reviewed-pill ${result.status === "sent" ? "sent-pill" : ""}`}><ShieldCheck size={13} /> {statusLabel(result.status)}</span>
                </div>
                <div className="review-banner"><ShieldCheck size={16} /><span><b>AI reviewed</b> · Confirm the exact address and final message before approving.</span></div>
                <label className="result-field">
                  <span>RECIPIENT · VERIFY THE EMAIL ADDRESS</span>
                  <input aria-label="Recipient email address" type="email" value={result.recipient} onChange={(event) => updateResult("recipient", event.target.value)} maxLength={254} disabled={result.status === "sent"} />
                </label>
                <label className="result-field">
                  <span>SUBJECT</span>
                  <input aria-label="Email subject" value={result.subject} onChange={(event) => updateResult("subject", event.target.value)} maxLength={200} disabled={result.status === "sent"} />
                </label>
                <label className="result-field body-field">
                  <span>FINAL EMAIL BODY</span>
                  <textarea aria-label="Email body" value={result.body} onChange={(event) => updateResult("body", event.target.value)} rows={9} maxLength={10000} disabled={result.status === "sent"} />
                </label>
                {result.review?.notes?.length > 0 && <div className="review-notes"><div className="section-mini-title"><FileText size={14} /> REVIEW NOTES</div><ul>{result.review.notes.map((note, index) => <li key={`${index}-${note}`}>{note}</li>)}</ul></div>}
                {result.review?.risk_flags?.length > 0 && <div className="risk-flags"><div className="section-mini-title"><Flag size={14} /> WORTH A SECOND LOOK</div><ul>{result.review.risk_flags.map((flag, index) => <li key={`${index}-${flag}`}>{flag}</li>)}</ul></div>}
                {approved && result.status === "approved" && <div className="approved-notice"><ShieldCheck size={15} /><span>Approved for this exact recipient, subject, and body. Editing any field requires approval again.</span></div>}
                {result.status === "sending" && <div className="uncertain-notice">A send attempt is still recorded as in progress. Do not retry; check Gmail and refresh the send history.</div>}
                {result.status === "uncertain" && <div className="uncertain-notice">Gmail’s result was inconclusive. Check your Sent folder before taking any further action. MailPilot will not retry this message.</div>}
                {result.status === "sent" && <div className="approved-notice">Gmail message ID: <code>{result.message_id}</code></div>}
                {result.status === "sent" && result.send_authorization === "auto_policy" && <div className="approved-notice"><ShieldCheck size={15} /> Automatically sent under your saved permission and policy. You can revoke this permission at any time.</div>}
                {result.send_authorization === "manual_approval_required" && result.policy_decision?.reasons?.length > 0 && (
                  <div className="review-notes policy-notice"><div className="section-mini-title"><ShieldCheck size={14} /> AUTO-SEND POLICY · MANUAL APPROVAL REQUIRED</div><ul>{result.policy_decision.reasons.map((reason) => <li key={reason}>{reason.replaceAll("_", " ")}</li>)}</ul></div>
                )}
                <div className="result-footer">
                  <span className="never-sent"><Clock3 size={14} /> {result.status === "sent" ? "Sent by your explicit action" : "No automatic sending"}</span>
                  <div className="result-actions">
                    <button className="secondary-button" type="button" onClick={reset}><RotateCcw size={15} /> Start over</button>
                    <button className="secondary-button" type="button" onClick={copyEmail}>{copied ? <Check size={15} /> : <Clipboard size={15} />}{copied ? "Copied" : "Copy"}</button>
                  </div>
                </div>
                {result.status !== "sent" && result.status !== "uncertain" && result.status !== "sending" && (
                  <div className="approval-actions">
                    {!approved ? (
                      <button className="approve-button" type="button" disabled={busy || sending || !auth} onClick={approveEmail}>
                        {busy ? <><LoaderCircle className="spin" size={16} /> Saving approval…</> : <><ShieldCheck size={16} /> Approve this exact email</>}
                      </button>
                    ) : (
                      <button className="send-button" type="button" disabled={sending || !auth?.gmail_connected} onClick={sendEmail}>
                        {sending ? <><LoaderCircle className="spin" size={16} /> Sending…</> : <><Send size={16} /> Send approved email to {result.recipient}</>}
                      </button>
                    )}
                    {!auth?.gmail_connected && <span className="send-hint">Connect Gmail to send. Approval will be required again after any edit.</span>}
                    {result.status === "failed" && <span className="send-hint">A provider error was recorded. Review the history and approve again before any retry.</span>}
                  </div>
                )}
              </>
            ) : null}
          </section>
        </div>

        {auth && (
          <section className="history-card card">
            <div className="history-heading"><div><div className="section-mini-title"><History size={15} /> SEND HISTORY</div><h2>Your saved drafts and send activity</h2></div><button className="secondary-button" type="button" onClick={() => refreshHistory().catch((requestError) => setError(requestError.message))}>Refresh</button></div>
            {history.length ? (
              <div className="history-list">
                {history.map((item) => (
                  <article className="history-item" key={item.id}>
                    <div className="history-status"><span className={`status-dot status-${item.status}`} />{statusLabel(item.status)}</div>
                    <div className="history-main"><strong>{item.subject}</strong><span>To: {item.recipient || "Recipient not set"}</span></div>
                    <time>{new Date(item.updated_at).toLocaleString()}</time>
                    {item.provider_message_id && <code className="history-message-id">ID: {item.provider_message_id}</code>}
                    {item.send_authorization && <span className="history-authorization">Authorized by: {item.send_authorization === "auto_policy" ? "automatic policy" : item.send_authorization === "manual_approval" ? "your approval" : item.send_authorization}</span>}
                    {item.policy_decision && <span className="history-authorization">Policy: {item.policy_decision.allowed ? "passed" : item.policy_decision.reasons.join(", ").replaceAll("_", " ")}</span>}
                    {item.send_error && <span className="history-error">{item.send_error}</span>}
                  </article>
                ))}
              </div>
            ) : <p className="history-empty">Generated drafts and send results will appear here.</p>}
          </section>
        )}

        <footer className="page-footer">
          <span>Made for the messages that matter.</span>
          <span><ShieldCheck size={14} /> Auto-send is off by default. Eligible auto-sends require explicit opt-in.</span>
        </footer>
      </main>
      {autoModalOpen && (
        <div className="modal-backdrop" role="presentation">
          <section className="auto-modal card" role="dialog" aria-modal="true" aria-labelledby="auto-modal-title">
            <button type="button" className="modal-close" aria-label="Close dialog" onClick={() => setAutoModalOpen(false)}>×</button>
            <div className="section-mini-title"><ShieldCheck size={15} /> DELIBERATE OPT-IN</div>
            <h2 id="auto-modal-title">{autoSettings?.enabled ? "Update automatic sending permission?" : "Enable automatic sending?"}</h2>
            <p>After you enable this, eligible newly generated emails may be sent immediately without a per-message approval. MailPilot's deterministic policy blocks uncertain, risky, sensitive, placeholder-containing, and out-of-policy drafts for manual review. This policy is a safeguard, not a guarantee.</p>
            <form onSubmit={enableAutoSend} className="auto-opt-form">
              <label className="field">
                <FieldLabel>Exact recipient allowlist (comma-separated)</FieldLabel>
                <textarea required value={autoRecipients} onChange={(event) => setAutoRecipients(event.target.value)} rows={2} placeholder="alex@example.com" />
              </label>
              <fieldset className="category-options">
                <legend>Email categories eligible for policy checks</legend>
                {categories.map((category) => (
                  <label key={category}><input type="checkbox" checked={autoCategories.includes(category.toLowerCase())} onChange={() => toggleAutoCategory(category)} />{category}</label>
                ))}
              </fieldset>
              <label className="field">
                <FieldLabel>Maximum automatic sends per day (1–10)</FieldLabel>
                <input type="number" min="1" max="10" value={autoDailyLimit} onChange={(event) => setAutoDailyLimit(event.target.value)} />
              </label>
              <label className="consent-check"><input type="checkbox" checked={autoAcknowledged} onChange={(event) => setAutoAcknowledged(event.target.checked)} /> I understand eligible messages can be sent without another approval, and I am responsible for the recipient and limits I configure.</label>
              <label className="field">
                <FieldLabel>Type ENABLE AUTO-SEND to confirm</FieldLabel>
                <input required value={autoConfirmPhrase} onChange={(event) => setAutoConfirmPhrase(event.target.value)} autoComplete="off" />
              </label>
              <div className="modal-actions">
                <button className="secondary-button" type="button" onClick={() => setAutoModalOpen(false)}>Cancel</button>
                <button className="approve-button" type="submit" disabled={settingsBusy || !autoAcknowledged || autoConfirmPhrase !== "ENABLE AUTO-SEND" || !autoCategories.length || !autoRecipients.trim()}>
                  {settingsBusy ? <><LoaderCircle className="spin" size={16} /> Saving…</> : autoSettings?.enabled ? "Confirm updated settings" : "Enable auto-send"}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}
