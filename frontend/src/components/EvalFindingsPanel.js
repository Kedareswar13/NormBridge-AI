import React, { useCallback, useEffect, useMemo, useState } from "react";
import axios from "axios";
import "./EvalsPanel.css";

const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

/**
 * Eval-driven development: how the evals found each problem and which change fixed it.
 *  1. Timeline: pass rate per run + the note recorded for "what changed before this run".
 *  2. Heatmap: every case x every run (green pass / red fail / blank = not in that run).
 *  3. Compare two runs: fixed, regressed and still-failing cases with the actual answers before and after.
 */
export default function EvalFindingsPanel({ apiUrl, auth }) {
  const [dataset, setDataset] = useState("golden");
  const [history, setHistory] = useState(null);
  const [onlyFailed, setOnlyFailed] = useState(true);
  const [a, setA] = useState(null);
  const [b, setB] = useState(null);
  const [diff, setDiff] = useState(null);
  const [open, setOpen] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const { data } = await axios.get(`${apiUrl}/admin/evals/history`, { auth, params: { dataset } });
      setHistory(data);
      const full = data.runs.filter((r) => r.total >= Math.max(...data.runs.map((x) => x.total)) * 0.9);
      const pickA = data.runs.find((r) => r.pass_rate !== null && r.pass_rate < 0.8 && r.total >= 40) || full[0] || data.runs[0];
      const pickB = full[full.length - 1] || data.runs[data.runs.length - 1];
      setA(pickA ? pickA.id : null);
      setB(pickB ? pickB.id : null);
      setError("");
    } catch (e) {
      setError("Could not load eval history");
    }
  }, [apiUrl, auth, dataset]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!a || !b || a === b) { setDiff(null); return; }
    axios.get(`${apiUrl}/admin/evals/compare`, { auth, params: { a, b } })
      .then((r) => setDiff(r.data)).catch(() => setDiff(null));
  }, [apiUrl, auth, a, b]);

  const runs = history?.runs || [];
  const cases = useMemo(() => (history?.cases || []).filter((c) => !onlyFailed || c.ever_failed), [history, onlyFailed]);
  const caught = (history?.cases || []).filter((c) => c.ever_failed);
  const fixed = caught.filter((c) => c.now_passing);

  const Item = ({ item, kind }) => (
    <div className="ev-find-item">
      <div className="ev-find-head" onClick={() => setOpen(open === `${kind}-${item.case_id}` ? null : `${kind}-${item.case_id}`)}>
        <code>{item.case_id}</code> <span className="ev-q">{item.question}</span>
      </div>
      {open === `${kind}-${item.case_id}` && (
        <div className="ev-find-body">
          {["before", "after"].map((side) => {
            const r = item[side];
            return (
              <div key={side} className={`ev-find-col ${r.passed ? "pass" : "fail"}`}>
                <div className="ev-find-label">{side === "before" ? `Run #${diff.a.id}` : `Run #${diff.b.id}`} · {r.passed ? "PASS" : "FAIL"} · {r.latency}s</div>
                <div className="ev-answer">{r.answer || "—"}</div>
                <div className="ev-flag">Sources: {(r.source_pdfs || []).join(", ") || "none"}</div>
                {r.missing_facts?.length > 0 && <div className="ev-flag">Missing: <b>{r.missing_facts.join("; ")}</b></div>}
                {r.polarity_ok === false && <div className="ev-flag"><b>Wrong yes/no verdict</b></div>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );

  return (
    <section className="ev-card">
      <div className="ev-card-head">
        <div>
          <h3>Eval-driven fixes: what the evals caught, and what fixed it</h3>
          <p className="ev-sub">
            {caught.length} of {(history?.cases || []).length} cases failed at least once across {runs.length} runs; {fixed.length} of them pass now.
            Every point on the timeline records what changed before that run.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <select className="ev-select" value={dataset} onChange={(e) => setDataset(e.target.value)}>
            <option value="golden">Golden set</option>
            <option value="heldout">Held-out set</option>
          </select>
          <button className="ev-btn ev-btn-ghost" onClick={load}>Refresh</button>
        </div>
      </div>
      {error && <div className="ev-error">{error}</div>}

      {/* ---------------------------------------------------------------- timeline */}
      <div className="ev-timeline">
        {runs.map((r) => (
          <div key={r.id} className="ev-tl-item" title={r.notes || ""} onClick={() => setB(r.id)}>
            <div className="ev-tl-bar-wrap">
              <div className={`ev-tl-bar ${r.pass_rate >= 0.9 ? "good" : r.pass_rate >= 0.8 ? "ok" : "bad"}`} style={{ height: `${Math.round((r.pass_rate || 0) * 100)}%` }} />
            </div>
            <div className="ev-tl-rate">{pct(r.pass_rate)}</div>
            <div className="ev-tl-id">#{r.id}</div>
            <div className="ev-tl-n">{r.passed}/{r.total}</div>
          </div>
        ))}
      </div>
      <div className="ev-notes">
        {runs.filter((r) => r.notes).map((r) => (
          <div key={r.id} className="ev-note"><b>#{r.id}</b> {r.notes}</div>
        ))}
      </div>

      {/* ---------------------------------------------------------------- heatmap */}
      <div className="ev-card-head" style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0 }}>Case history</h3>
        <label className="ev-sub" style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={onlyFailed} onChange={(e) => setOnlyFailed(e.target.checked)} /> only cases that ever failed
        </label>
      </div>
      <div className="ev-table-wrap">
        <table className="ev-heat">
          <thead>
            <tr><th>Case</th>{runs.map((r) => <th key={r.id}>#{r.id}</th>)}<th>Now</th></tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.case_id} title={c.question}>
                <td><code>{c.case_id}</code></td>
                {runs.map((r) => {
                  const v = c.results[String(r.id)];
                  return <td key={r.id} className={v === undefined ? "na" : v ? "p" : "f"}>{v === undefined ? "" : v ? "✓" : "✗"}</td>;
                })}
                <td>{c.now_passing ? <span className="ev-chip ev-chip-pass">{c.fixed_in_run ? `fixed #${c.fixed_in_run}` : "pass"}</span>
                  : <span className="ev-chip ev-chip-fail">open</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* ---------------------------------------------------------------- compare */}
      <div className="ev-card-head" style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0 }}>Compare two runs</h3>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <select className="ev-select" value={a || ""} onChange={(e) => setA(Number(e.target.value))}>
            {runs.map((r) => <option key={r.id} value={r.id}>#{r.id} · {pct(r.pass_rate)}</option>)}
          </select>
          <span className="ev-sub">→</span>
          <select className="ev-select" value={b || ""} onChange={(e) => setB(Number(e.target.value))}>
            {runs.map((r) => <option key={r.id} value={r.id}>#{r.id} · {pct(r.pass_rate)}</option>)}
          </select>
        </div>
      </div>
      {diff && (
        <div className="ev-compare">
          <p className="ev-sub">
            <b>#{diff.a.id}</b> {diff.a.passed}/{diff.a.total} → <b>#{diff.b.id}</b> {diff.b.passed}/{diff.b.total}:
            {" "}<span className="ev-chip ev-chip-pass">{diff.fixed.length} fixed</span>
            {" "}<span className="ev-chip ev-chip-fail">{diff.regressed.length} regressed</span>
            {" "}<span className="ev-chip ev-chip-warn">{diff.still_failing.length} still failing</span>
            {" "}· {diff.unchanged_passing} unchanged passing{diff.only_in_b ? ` · ${diff.only_in_b} new cases in #${diff.b.id}` : ""}.
            Click a case to see both answers.
          </p>
          {[["fixed", "Fixed"], ["regressed", "Regressed"], ["still_failing", "Still failing"]].map(([k, label]) => diff[k].length > 0 && (
            <div key={k} className="ev-find-group">
              <h4>{label}</h4>
              {diff[k].map((item) => <Item key={item.case_id} item={item} kind={k} />)}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
