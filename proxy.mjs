// Metering proxy: Claude Code -> this proxy -> DeepSeek's Anthropic-compatible API.
// Logs one JSON line per model request with exact usage, latency and tool calls.
//
//   UPSTREAM=https://api.deepseek.com/anthropic PROXY_PORT=8788 node proxy.mjs
//   POST /_tag  {"tag":"<run id>","file":"<path to requests.jsonl>"}  -> sets where logs go
import http from "node:http";
import fs from "node:fs";

const UPSTREAM = (process.env.UPSTREAM || "https://api.deepseek.com/anthropic").replace(/\/$/, "");
const PORT = Number(process.env.PROXY_PORT || 8788);
let current = { tag: "untagged", file: process.env.PROXY_LOG || "proxy_untagged.jsonl" };

function summarizeRequest(body) {
  try {
    const j = JSON.parse(body);
    return {
      model: j.model,
      stream: !!j.stream,
      n_messages: (j.messages || []).length,
      n_tools: (j.tools || []).length,
      n_deferred_tools: (j.tools || []).filter((t) => t.defer_loading).length,
      server_tool_types: (j.tools || []).filter((t) => t.type && t.type !== "custom").map((t) => t.type),
      anthropic_beta: undefined,
      tool_names: (j.tools || []).map((t) => t.name),
      tools_chars: JSON.stringify(j.tools || []).length,
      system_chars: JSON.stringify(j.system || "").length,
      messages_chars: JSON.stringify(j.messages || []).length,
    };
  } catch {
    return {};
  }
}

function parseResponse(text, isStream) {
  const out = { usage: {}, tool_uses: [], stop_reason: null, text_chars: 0 };
  const merge = (u) => { if (u) for (const [k, v] of Object.entries(u)) if (v != null) out.usage[k] = v; };
  if (!isStream) {
    try {
      const j = JSON.parse(text);
      merge(j.usage);
      out.stop_reason = j.stop_reason;
      for (const b of j.content || []) {
        if (b.type === "tool_use") out.tool_uses.push({ name: b.name, input: b.input });
        if (b.type === "text") out.text_chars += b.text.length;
      }
    } catch { out.raw = text.slice(0, 500); }
    return out;
  }
  const blocks = {};
  for (const line of text.split("\n")) {
    if (!line.startsWith("data:")) continue;
    let ev;
    try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
    if (ev.type === "message_start") merge(ev.message?.usage);
    else if (ev.type === "message_delta") { merge(ev.usage); out.stop_reason = ev.delta?.stop_reason ?? out.stop_reason; }
    else if (ev.type === "content_block_start") blocks[ev.index] = { ...ev.content_block, _json: "" };
    else if (ev.type === "content_block_delta") {
      const b = blocks[ev.index];
      if (!b) continue;
      if (ev.delta?.type === "input_json_delta") b._json += ev.delta.partial_json || "";
      if (ev.delta?.type === "text_delta") out.text_chars += (ev.delta.text || "").length;
    } else if (ev.type === "error") out.error = ev.error;
  }
  for (const b of Object.values(blocks)) {
    if (b.type !== "tool_use") continue;
    let input = b._json;
    try { input = JSON.parse(b._json || "{}"); } catch {}
    out.tool_uses.push({ name: b.name, input });
  }
  return out;
}

const server = http.createServer(async (req, res) => {
  let body = "";
  for await (const c of req) body += c;

  if (req.url === "/_tag" && req.method === "POST") {
    current = JSON.parse(body);
    return res.end("ok");
  }

  const t0 = Date.now();
  const headers = { ...req.headers };
  delete headers.host;
  delete headers["content-length"];
  delete headers["accept-encoding"];
  let upstream;
  try {
    upstream = await fetch(UPSTREAM + req.url, { method: req.method, headers, body: req.method === "GET" ? undefined : body });
  } catch (e) {
    res.statusCode = 502;
    res.end(JSON.stringify({ type: "error", error: { type: "proxy_error", message: String(e) } }));
    fs.appendFileSync(current.file, JSON.stringify({ tag: current.tag, ts: t0, path: req.url, proxy_error: String(e) }) + "\n");
    return;
  }
  const respHeaders = {};
  upstream.headers.forEach((v, k) => { if (!["content-encoding", "content-length", "transfer-encoding"].includes(k)) respHeaders[k] = v; });
  res.writeHead(upstream.status, respHeaders);

  let ttfb = null;
  const chunks = [];
  const decoder = new TextDecoder();
  for await (const chunk of upstream.body) {
    if (ttfb === null) ttfb = Date.now() - t0;
    chunks.push(decoder.decode(chunk, { stream: true }));
    res.write(chunk);
  }
  res.end();

  const text = chunks.join("");
  const isMessages = req.url.split("?")[0].endsWith("/v1/messages");
  const reqSum = summarizeRequest(body);
  const rec = {
    tag: current.tag,
    ts: t0,
    path: req.url,
    status: upstream.status,
    ms: Date.now() - t0,
    ttfb_ms: ttfb,
    ...reqSum,
    tool_names: undefined,
    ...(isMessages ? parseResponse(text, reqSum.stream) : { raw: text.slice(0, 300) }),
  };
  rec.tool_names_hash = (reqSum.tool_names || []).join(",");
  if (upstream.status >= 400) rec.error_body = text.slice(0, 1000);
  fs.appendFileSync(current.file, JSON.stringify(rec) + "\n");
});

server.listen(PORT, "127.0.0.1", () => console.log(`proxy on http://127.0.0.1:${PORT} -> ${UPSTREAM}`));
