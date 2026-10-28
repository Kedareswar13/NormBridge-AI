import { authHeaders } from "./auth";

const API = process.env.REACT_APP_API_URL || "http://localhost:5001";

/**
 * POST /chat/stream and parse its Server-Sent Events (EventSource can't POST, so fetch + a stream reader).
 *   onStage({stage, model, documents}) - pipeline progress
 *   onToken(text)                      - every generated text delta
 * Resolves with {status, data} where data is exactly what /chat would have returned.
 */
export async function streamChat(body, { onStage, onToken } = {}) {
  const res = await fetch(`${API}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    const err = new Error(`stream failed: ${res.status}`);
    err.response = { status: res.status };
    throw err;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let done = null;

  const handle = (frame) => {
    let event = "message";
    const dataLines = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    let data;
    try { data = JSON.parse(dataLines.join("\n")); } catch { return; }
    if (event === "token") onToken && onToken(data);
    else if (event === "stage") onStage && onStage(data);
    else if (event === "done") done = data;
  };

  for (;;) {
    const { value, done: finished } = await reader.read();
    if (finished) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      handle(buffer.slice(0, idx));
      buffer = buffer.slice(idx + 2);
    }
  }
  if (buffer.trim()) handle(buffer);
  if (!done) throw new Error("stream ended without a final answer");
  return { status: done.status, data: done.payload || {} };
}
