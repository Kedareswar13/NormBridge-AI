import React, { useCallback, useEffect, useState } from "react";
import axios from "axios";
import "./EvalsPanel.css";

const COLORS = { rewrite: "#a78bfa", embed: "#38bdf8", answer_cache: "#94a3b8", retrieve: "#22d3ee", rerank: "#f59e0b",
                 llm: "#818cf8", evaluate: "#34d399" };

/** Request tracing: where each answer spent its time (stage spans; no question text is stored). */
export default function TracesPanel({ apiUrl, auth }) {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setData((await axios.get(`${apiUrl}/admin/traces`, { auth, params: { limit: 60 } })).data);
      setError("");
    } catch (e) {
      setError("Could not load traces");
    }
  }, [apiUrl, auth]);

  useEffect(() => { load(); }, [load]);

  const stages = data?.stages || {};
  const traces = data?.traces || [];

  return (
    <div className="evals-panel">
      {error && <div className="ev-error">{error}</div>}
      <section className="ev-card">
        <div className="ev-card-head">
          <div>
            <h3>Latency by pipeline stage</h3>
            <p className="ev-sub">p50 / p95 over the last {traces.length} requests. Stage timings, model and document ids only, never the question text.</p>
          </div>
          <button className="ev-btn" onClick={load}>Refresh</button>
        </div>
        <div className="ev-metrics">
          {Object.entries(stages).map(([name, s]) => (
            <div className="ev-metric" key={name}>
              <span className="ev-metric-label">{name.replace("_", " ")}</span>
              <span className="ev-metric-value">{(s.p50_ms / 1000).toFixed(2)}s</span>
              <span className="ev-sub">p95 {(s.p95_ms / 1000).toFixed(2)}s · {s.count}×</span>
            </div>
          ))}
          {!Object.keys(stages).length && <p className="ev-sub">No traces yet: ask a question in the chat.</p>}
        </div>
      </section>

      <section className="ev-card">
        <h3>Recent requests</h3>
        <div className="ev-table-wrap">
          <table className="ev-table">
            <thead><tr><th>Time</th><th>Decision</th><th>Role</th><th>Model</th><th>Total</th><th>Waterfall</th></tr></thead>
            <tbody>
              {traces.map(t => {
                const total = Math.max(t.duration_ms || 1, 1);
                return (
                  <React.Fragment key={t.id}>
                    <tr onClick={() => setOpen(open === t.id ? null : t.id)} style={{ cursor: "pointer" }}>
                      <td>{t.created_at ? new Date(t.created_at).toLocaleTimeString() : ""}</td>
                      <td>{t.decision}</td>
                      <td>{t.role}</td>
                      <td>{t.model}</td>
                      <td>{(total / 1000).toFixed(2)}s</td>
                      <td style={{ minWidth: 260 }}>
                        <div style={{ position: "relative", height: 12, background: "rgba(148,163,184,0.12)", borderRadius: 6 }}>
                          {t.spans.map((s, i) => (
                            <div key={i} title={`${s.name} ${(s.duration_ms / 1000).toFixed(2)}s`} style={{
                              position: "absolute", top: 0, height: 12, borderRadius: 4,
                              left: `${(s.start_ms / total) * 100}%`, width: `${Math.max((s.duration_ms / total) * 100, 0.8)}%`,
                              background: COLORS[s.name] || "#64748b" }} />
                          ))}
                        </div>
                      </td>
                    </tr>
                    {open === t.id && (
                      <tr><td colSpan={6}>
                        {t.spans.map((s, i) => (
                          <div key={i} className="ev-sub" style={{ display: "flex", gap: 12 }}>
                            <span style={{ color: COLORS[s.name] || "#94a3b8", minWidth: 110 }}>{s.name}</span>
                            <span style={{ minWidth: 150 }}>+{(s.start_ms / 1000).toFixed(2)}s · {(s.duration_ms / 1000).toFixed(2)}s</span>
                            <span>{Object.entries(s.attrs || {}).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ")}</span>
                          </div>
                        ))}
                        <div className="ev-sub">trace id {t.id}</div>
                      </td></tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
