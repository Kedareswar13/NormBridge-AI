import React, { useCallback, useEffect, useState } from "react";
import axios from "axios";
import "./EvalsPanel.css";

const DECISION_TONE = { answered: "pass", cached: "pass", listed: "pass", smalltalk: "abstained", refused: "abstained", blocked: "fail", error: "fail" };

/** RAG security dashboard: audit trail (PII-masked), blocked attempts and document access levels. */
export default function SecurityPanel({ apiUrl, auth }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setData((await axios.get(`${apiUrl}/admin/security/audit`, { auth, params: { limit: 150 } })).data);
      setError("");
    } catch (e) {
      setError("Could not load the audit log");
    }
  }, [apiUrl, auth]);

  useEffect(() => { load(); }, [load]);

  const counts = data?.counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);

  return (
    <div className="evals-panel">
      {error && <div className="ev-error">{error}</div>}

      <section className="ev-card">
        <div className="ev-card-head">
          <div>
            <h3>RAG security layers</h3>
            <p className="ev-sub">
              1 · Query validation blocks prompt-injection and data-exfiltration attempts before retrieval &nbsp;·&nbsp;
              2 · Role + tenant filters inside the vector search and answer cache &nbsp;·&nbsp;
              3 · Fixed prompt template with sanitised slots &nbsp;·&nbsp;
              4 · PII-masked audit trail (document ids only, never full text)
            </p>
          </div>
          <button className="ev-btn ev-btn-ghost" onClick={load}>Refresh</button>
        </div>
        <div className="ev-metrics">
          <div className="ev-metric"><span className="ev-metric-label">Requests audited</span><span className="ev-metric-value">{total}</span></div>
          <div className="ev-metric ev-good"><span className="ev-metric-label">Answered</span><span className="ev-metric-value">{(counts.answered || 0) + (counts.cached || 0)}</span></div>
          <div className="ev-metric"><span className="ev-metric-label">Refused / out of scope</span><span className="ev-metric-value">{counts.refused || 0}</span></div>
          <div className={`ev-metric ${counts.blocked ? "ev-bad" : "ev-good"}`}><span className="ev-metric-label">Blocked attacks</span><span className="ev-metric-value">{counts.blocked || 0}</span></div>
          <div className="ev-metric"><span className="ev-metric-label">Tenant</span><span className="ev-metric-value" style={{ fontSize: "1.05rem" }}>{data?.tenant_id || "—"}</span></div>
        </div>
        {data?.blocked_by?.length > 0 && (
          <div className="ev-cats">
            {data.blocked_by.map((b, i) => (
              <div key={i} className="ev-cat" style={{ gridTemplateColumns: "1fr 44px" }}>
                <span title={b.reason}>{(b.reason || "").split(":")[0]} — <code>{(b.reason || "").split(":").slice(1).join(":").trim().slice(0, 50)}</code></span>
                <span>{b.n}×</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="ev-card">
        <div className="ev-card-head">
          <div>
            <h3>Document access levels</h3>
            <p className="ev-sub">Retrieval only returns documents at or below the asker's role: student → public + student · staff → + staff · admin → everything.</p>
          </div>
        </div>
        <div className="ev-runs">
          {(data?.policy_audiences || []).map(p => (
            <span key={p.name} className="ev-run" title={p.name} style={{ cursor: "default" }}>
              <span style={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.name.replace(/\.pdf$/i, "")}</span>
              <span className={`ev-chip ev-chip-${p.audience === "public" ? "pass" : p.audience === "staff" ? "warn" : "fail"}`}>{p.audience}</span>
            </span>
          ))}
        </div>
      </section>

      <section className="ev-card">
        <div className="ev-card-head">
          <div>
            <h3>Audit trail</h3>
            <p className="ev-sub">Who (pseudonymous id + masked email), when, role, decision and which document pages were used. Emails, phone numbers, Aadhaar, PAN, card and account numbers are masked before storage.</p>
          </div>
        </div>
        <table className="ev-table">
          <thead><tr><th>Time</th><th>User</th><th>Role</th><th>Question (masked)</th><th>Decision</th><th>Documents used</th><th>Latency</th></tr></thead>
          <tbody>
            {(data?.rows || []).map(r => (
              <tr key={r.id}>
                <td style={{ whiteSpace: "nowrap" }}>{r.ts ? new Date(r.ts).toLocaleString() : "—"}</td>
                <td title={`ref ${r.user_ref}`}>{r.user_masked}</td>
                <td>{r.role}</td>
                <td className="ev-q">{r.query_masked}</td>
                <td><span className={`ev-chip ev-chip-${DECISION_TONE[r.decision] || "abstained"}`} title={r.reason || ""}>{r.decision}</span></td>
                <td className="ev-src">{(r.documents || []).map(d => `${(d.pdf || "").replace(/\.pdf$/i, "")} p${(d.page ?? 0) + 1}`).join(", ") || "—"}</td>
                <td>{r.latency_ms != null ? `${(r.latency_ms / 1000).toFixed(1)}s` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
