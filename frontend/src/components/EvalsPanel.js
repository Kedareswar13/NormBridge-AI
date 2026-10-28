import React, { useCallback, useEffect, useRef, useState } from "react";
import axios from "axios";
import "./EvalsPanel.css";
import EvalFindingsPanel from "./EvalFindingsPanel";

const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const tone = (v, good = 0.8, ok = 0.6) => (v === null || v === undefined ? "muted" : v >= good ? "good" : v >= ok ? "ok" : "bad");

const VERDICT_LABEL = { pass: "Pass", warn: "Warn", fail: "Fail", abstained: "Abstained" };

function Metric({ label, value, hint, toneClass }) {
  return (
    <div className={`ev-metric ev-${toneClass || "muted"}`} title={hint}>
      <span className="ev-metric-label">{label}</span>
      <span className="ev-metric-value">{value}</span>
    </div>
  );
}

/**
 * Deterministic evaluation dashboard.
 *  - Live: every real answer is scored against the chunks it was generated from (groundedness, numeric faithfulness, abstention).
 *  - Golden set: replay curated questions with known answers and score document hit, fact recall and refusal behaviour.
 */
export default function EvalsPanel({ apiUrl, auth }) {
  const [live, setLive] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState(null);
  const [goldenCount, setGoldenCount] = useState(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(null);
  const pollRef = useRef(null);

  const get = useCallback((path, opts = {}) => axios.get(`${apiUrl}${path}`, { auth, ...opts }), [apiUrl, auth]);

  const loadLive = useCallback(async () => {
    try { setLive((await get("/admin/evals/live")).data); } catch (e) { setError("Could not load live evals"); }
  }, [get]);

  const loadRun = useCallback(async (id) => {
    try { setSelectedRun((await get(`/admin/evals/runs/${id}`)).data); } catch (e) { setError("Could not load run"); }
  }, [get]);

  const loadRuns = useCallback(async (selectLatest = false) => {
    try {
      const data = (await get("/admin/evals/runs")).data.runs || [];
      setRuns(data);
      if (selectLatest && data.length) loadRun(data[0].id);
      return data;
    } catch (e) { setError("Could not load eval runs"); return []; }
  }, [get, loadRun]);

  useEffect(() => {
    loadLive();
    loadRuns(true);
    get("/admin/evals/golden").then((r) => setGoldenCount(r.data.cases.length)).catch(() => {});
  }, [loadLive, loadRuns, get]);

  // Poll while a run is in progress
  useEffect(() => {
    const running = runs.find((r) => r.status === "running");
    clearInterval(pollRef.current);
    if (running) {
      pollRef.current = setInterval(async () => {
        const data = await loadRuns(false);
        const r = data.find((x) => x.id === running.id);
        if (selectedRun?.id === running.id || !selectedRun) loadRun(running.id);
        if (r && r.status !== "running") { loadLive(); loadRun(r.id); }
      }, 4000);
    }
    return () => clearInterval(pollRef.current);
  }, [runs, selectedRun, loadRuns, loadRun, loadLive]);

  const startRun = async () => {
    setStarting(true); setError("");
    try {
      const { data } = await axios.post(`${apiUrl}/admin/evals/runs`, {}, { auth });
      await loadRuns(false);
      loadRun(data.run_id);
    } catch (e) {
      setError(e.response?.data?.error || "Could not start eval run");
    } finally { setStarting(false); }
  };

  const downloadReport = async (id) => {
    const res = await get(`/admin/evals/runs/${id}`, { params: { format: "md" }, responseType: "blob" });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url; a.download = `normbridge-eval-run-${id}.md`; a.click();
    URL.revokeObjectURL(url);
  };

  const s = selectedRun?.summary;
  const running = runs.find((r) => r.status === "running");

  return (
    <div className="evals-panel">
      {error && <div className="ev-error">{error}</div>}

      {/* what the evals caught and what fixed it: the timeline, heatmap and before/after comparison come first */}
      <EvalFindingsPanel apiUrl={apiUrl} auth={auth} />

      {/* ------------------------------------------------------------- Live answer quality */}
      <section className="ev-card">
        <div className="ev-card-head">
          <div>
            <h3>Live answer quality</h3>
            <p className="ev-sub">Every answer is scored against the policy excerpts it was generated from — deterministic, no LLM judge.</p>
          </div>
          <button className="ev-btn ev-btn-ghost" onClick={loadLive}>Refresh</button>
        </div>

        {live && live.total > 0 ? (
          <>
            <div className="ev-metrics">
              <Metric label="Answers scored" value={live.total} />
              <Metric label="Pass rate" value={pct(live.pass_rate)} toneClass={tone(live.pass_rate)} hint="Grounded answers with no unsupported numbers" />
              <Metric label="Groundedness" value={pct(live.avg_groundedness)} toneClass={tone(live.avg_groundedness, 0.75, 0.6)} hint="Share of answer content words found in the retrieved excerpts" />
              <Metric label="Numeric faithfulness" value={pct(live.avg_numeric_faithfulness)} toneClass={tone(live.avg_numeric_faithfulness, 0.95, 0.8)} hint="Numbers in answers that appear in the sources" />
              <Metric label="Context relevance" value={pct(live.avg_context_relevance)} toneClass={tone(live.avg_context_relevance, 0.6, 0.4)} hint="Question terms found in the retrieved excerpts" />
              <Metric label="Abstention rate" value={pct(live.abstention_rate)} hint="Refusals / 'not mentioned' answers" />
            </div>

            <div className="ev-verdict-bar">
              {["pass", "warn", "fail", "abstained"].map((v) => {
                const n = live.verdicts[v] || 0;
                return n ? <div key={v} className={`ev-seg ev-seg-${v}`} style={{ flex: n }} title={`${VERDICT_LABEL[v]}: ${n}`}>{VERDICT_LABEL[v]} {n}</div> : null;
              })}
            </div>

            <table className="ev-table">
              <thead><tr><th>Question</th><th>Verdict</th><th>Grounded</th><th>Numbers</th><th>Source</th><th>Latency</th></tr></thead>
              <tbody>
                {live.items.map((it) => (
                  <React.Fragment key={it.chat_id}>
                    <tr className="ev-row" onClick={() => setExpanded(expanded === it.chat_id ? null : it.chat_id)}>
                      <td className="ev-q">{it.question}</td>
                      <td><span className={`ev-chip ev-chip-${it.verdict}`}>{VERDICT_LABEL[it.verdict] || it.verdict}</span></td>
                      <td>{pct(it.groundedness)}</td>
                      <td>{pct(it.numeric_faithfulness)}</td>
                      <td className="ev-src">{(it.source_pdfs || []).join(", ") || "—"}</td>
                      <td>{it.latency ? `${it.latency.toFixed(1)}s` : "—"}</td>
                    </tr>
                    {expanded === it.chat_id && (
                      <tr className="ev-detail"><td colSpan={6}>
                        <div className="ev-answer">{it.answer}</div>
                        {it.unsupported_numbers?.length > 0 && <div className="ev-flag">Numbers not found in sources: <b>{it.unsupported_numbers.join(", ")}</b></div>}
                        {it.unsupported_sentences?.length > 0 && (
                          <div className="ev-flag">Weakly supported sentences:<ul>{it.unsupported_sentences.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
                        )}
                      </td></tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <p className="ev-empty">No answers scored yet — ask a few questions in the chat.</p>
        )}
      </section>

      {/* ------------------------------------------------------------- Golden set */}
      <section className="ev-card">
        <div className="ev-card-head">
          <div>
            <h3>Golden-set regression evals</h3>
            <p className="ev-sub">
              {goldenCount ?? "…"} curated questions with verified answers from the policy PDFs (incl. out-of-scope questions that must be refused).
            </p>
          </div>
          <button className="ev-btn" onClick={startRun} disabled={starting || !!running}>
            {running ? `Running… ${running.completed}/${running.total}` : starting ? "Starting…" : "Run golden set"}
          </button>
        </div>

        {running && (
          <div className="ev-progress"><div style={{ width: `${(running.completed / Math.max(running.total, 1)) * 100}%` }} /></div>
        )}

        {runs.length > 0 && (
          <div className="ev-runs">
            {runs.map((r) => (
              <button key={r.id} className={`ev-run ${selectedRun?.id === r.id ? "active" : ""}`} onClick={() => loadRun(r.id)}>
                <span>#{r.id}</span>
                <span className={`ev-chip ev-chip-${r.status === "completed" ? (r.passed / Math.max(r.total, 1) >= 0.8 ? "pass" : "warn") : r.status === "failed" ? "fail" : "abstained"}`}>
                  {r.status === "running" ? `${r.completed}/${r.total}` : `${r.passed}/${r.total}`}
                </span>
              </button>
            ))}
          </div>
        )}

        {selectedRun && (
          <>
            <div className="ev-run-head">
              <span>Run #{selectedRun.id} · model <code>{selectedRun.model}</code> · {selectedRun.status}</span>
              {selectedRun.status === "completed" && <button className="ev-btn ev-btn-ghost" onClick={() => downloadReport(selectedRun.id)}>Download report (.md)</button>}
            </div>
            {s && (
              <div className="ev-metrics">
                <Metric label="Pass rate" value={pct(s.pass_rate)} toneClass={tone(s.pass_rate)} />
                <Metric label="Right document" value={pct(s.doc_hit_rate)} toneClass={tone(s.doc_hit_rate)} hint="Answer grounded in the expected policy PDF" />
                <Metric label="Fact recall" value={pct(s.fact_recall)} toneClass={tone(s.fact_recall)} hint="Expected facts present in the answer" />
                <Metric label="Groundedness" value={pct(s.groundedness)} toneClass={tone(s.groundedness, 0.75, 0.6)} />
                <Metric label="Numeric faithfulness" value={pct(s.numeric_faithfulness)} toneClass={tone(s.numeric_faithfulness, 0.95, 0.8)} />
                <Metric label="Correct refusals" value={pct(s.correct_refusal_rate)} toneClass={tone(s.correct_refusal_rate)} hint="Out-of-scope questions that were refused" />
                <Metric label="False refusals" value={pct(s.false_refusal_rate)} toneClass={tone(s.false_refusal_rate === null ? null : 1 - s.false_refusal_rate)} hint="Answerable questions that were refused" />
                <Metric label="Latency avg / p95" value={`${s.avg_latency ?? "—"}s / ${s.p95_latency ?? "—"}s`} />
              </div>
            )}
            {s?.by_category && (
              <div className="ev-cats">
                {Object.entries(s.by_category).map(([cat, v]) => (
                  <div key={cat} className="ev-cat">
                    <span>{cat}</span>
                    <div className="ev-cat-bar"><div style={{ width: `${(v.passed / v.total) * 100}%` }} /></div>
                    <span>{v.passed}/{v.total}</span>
                  </div>
                ))}
              </div>
            )}
            <table className="ev-table">
              <thead><tr><th>Case</th><th>Question</th><th>Result</th><th>Doc</th><th>Facts</th><th>Grounded</th><th>Latency</th></tr></thead>
              <tbody>
                {(selectedRun.results || []).map((r) => (
                  <React.Fragment key={r.case_id}>
                    <tr className="ev-row" onClick={() => setExpanded(expanded === r.case_id ? null : r.case_id)}>
                      <td><code>{r.case_id}</code></td>
                      <td className="ev-q">{r.question}</td>
                      <td><span className={`ev-chip ev-chip-${r.passed ? "pass" : "fail"}`}>{r.passed ? "Pass" : "Fail"}</span></td>
                      <td>{r.metrics.doc_hit === null || r.metrics.doc_hit === undefined ? "—" : r.metrics.doc_hit ? "✓" : "✗"}</td>
                      <td>{pct(r.metrics.fact_recall)}</td>
                      <td>{pct(r.metrics.groundedness)}</td>
                      <td>{r.latency}s</td>
                    </tr>
                    {expanded === r.case_id && (
                      <tr className="ev-detail"><td colSpan={7}>
                        <div className="ev-answer">{r.answer}</div>
                        <div className="ev-flag">Expected: {(r.metrics.expected_pdfs || []).join(" or ") || "a refusal"} · Got: {(r.metrics.source_pdfs || []).join(", ") || "no sources"}</div>
                        {r.metrics.missing_facts?.length > 0 && <div className="ev-flag">Missing facts: <b>{r.metrics.missing_facts.join("; ")}</b></div>}
                        {r.metrics.unsupported_numbers?.length > 0 && <div className="ev-flag">Numbers not in sources: <b>{r.metrics.unsupported_numbers.join(", ")}</b></div>}
                      </td></tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>
    </div>
  );
}
