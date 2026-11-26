// Fake Anthropic Messages endpoint for dry runs (no API key, no cost).
// Always answers with a final text; usage.input_tokens ~= request chars / 4 so the
// fixed overhead of each mode (tool definitions, system prompt) is still visible.
import http from "node:http";
const PORT = Number(process.env.MOCK_PORT || 8799);

http.createServer(async (req, res) => {
  let body = "";
  for await (const c of req) body += c;
  const approx = Math.round(body.length / 4);
  if (req.url.includes("count_tokens")) {
    res.setHeader("content-type", "application/json");
    return res.end(JSON.stringify({ input_tokens: approx }));
  }
  let j = {};
  try { j = JSON.parse(body); } catch {}
  const text = 'Dry run.\nANSWER: "ok"';
  const usage = { input_tokens: approx, output_tokens: 8, cache_read_input_tokens: 0 };
  if (!j.stream) {
    res.setHeader("content-type", "application/json");
    return res.end(JSON.stringify({ id: "msg_mock", type: "message", role: "assistant", model: j.model,
      content: [{ type: "text", text }], stop_reason: "end_turn", usage }));
  }
  res.writeHead(200, { "content-type": "text/event-stream" });
  const send = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
  send("message_start", { message: { id: "msg_mock", type: "message", role: "assistant", model: j.model, content: [], stop_reason: null, usage: { ...usage, output_tokens: 1 } } });
  send("content_block_start", { index: 0, content_block: { type: "text", text: "" } });
  send("content_block_delta", { index: 0, delta: { type: "text_delta", text } });
  send("content_block_stop", { index: 0 });
  send("message_delta", { delta: { stop_reason: "end_turn" }, usage: { output_tokens: 8 } });
  send("message_stop", {});
  res.end();
}).listen(PORT, "127.0.0.1", () => console.log(`mock upstream on ${PORT}`));
