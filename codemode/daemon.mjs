// Code Mode bridge: keeps MCP server connections alive for one run, exposes them
// over local HTTP, and generates a typed JS API (./api) the model imports from scripts.
//
// node codemode/daemon.mjs --servers <resolved.json> --port <n> --api-dir <dir> --log <calls.jsonl>
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport, getDefaultEnvironment } from "@modelcontextprotocol/sdk/client/stdio.js";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const argv = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, arr) => (a.startsWith("--") ? [...acc, [a.slice(2), arr[i + 1]]] : acc), [])
);
const servers = JSON.parse(fs.readFileSync(argv.servers, "utf8"));
const port = Number(argv.port || 8790);
const apiDir = argv["api-dir"];
const logFile = argv.log;

const clients = {};
const toolsByServer = {};

for (const [name, def] of Object.entries(servers)) {
  const transport = new StdioClientTransport({
    command: def.command,
    args: def.args,
    env: { ...getDefaultEnvironment(), ...(def.env || {}) },
    stderr: "pipe",
  });
  let errBuf = "";
  transport.stderr?.on("data", (d) => { errBuf = (errBuf + d).slice(-4000); });
  const client = new Client({ name: "codemode-bridge", version: "1.0.0" });
  try {
    await client.connect(transport);
  } catch (e) {
    console.error(`server "${name}" failed to start: ${e?.message}\n--- stderr ---\n${errBuf}`);
    process.exit(1);
  }
  clients[name] = client;
  toolsByServer[name] = (await client.listTools()).tools;
}

// ---------- API generation ----------
const ident = (s) => s.replace(/[^A-Za-z0-9_$]/g, "_");

function tsType(schema, indent = "  ") {
  if (!schema || typeof schema !== "object") return "any";
  if (schema.enum) return schema.enum.map((v) => JSON.stringify(v)).join(" | ");
  const t = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  if (schema.anyOf || schema.oneOf) return (schema.anyOf || schema.oneOf).map((s) => tsType(s, indent)).join(" | ");
  switch (t) {
    case "string": return "string";
    case "number": case "integer": return "number";
    case "boolean": return "boolean";
    case "array": return `Array<${tsType(schema.items, indent)}>`;
    case "object": {
      const props = schema.properties || {};
      const req = new Set(schema.required || []);
      const keys = Object.keys(props);
      if (!keys.length) return "Record<string, any>";
      const lines = keys.map((k) => {
        const p = props[k];
        const doc = p.description ? `${indent}/** ${p.description.replace(/\*\//g, "").replace(/\s+/g, " ")} */\n` : "";
        return `${doc}${indent}${JSON.stringify(k)}${req.has(k) ? "" : "?"}: ${tsType(p, indent + "  ")};`;
      });
      return `{\n${lines.join("\n")}\n${indent.slice(2)}}`;
    }
    default: return "any";
  }
}

if (apiDir) {
  fs.mkdirSync(apiDir, { recursive: true });
  fs.writeFileSync(
    path.join(apiDir, "_client.mjs"),
    `const URL_ = "http://127.0.0.1:${port}/call";
export async function call(server, tool, args = {}) {
  const r = await fetch(URL_, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ server, tool, args }) });
  const res = await r.json();
  if (res.bridgeError) throw new Error(res.bridgeError);
  const text = (res.content || []).filter((c) => c.type === "text").map((c) => c.text).join("\\n");
  if (res.isError) throw new Error(\`\${server}.\${tool} failed: \${text}\`);
  if (res.structuredContent) return res.structuredContent;
  try { return JSON.parse(text); } catch { return text; }
}
`
  );
  const index = [
    "# Tool API (Code Mode)",
    "",
    "Each MCP server is a module in this folder. Every function is async, takes ONE object argument,",
    "and resolves to the tool result: a parsed JSON value when the tool returns JSON, otherwise a string.",
    "A tool error throws. Full parameter types and descriptions: `<server>.d.ts`.",
    "",
    "Example (save as script.mjs next to the api folder, run with `node script.mjs`):",
    "```js",
    `import * as ${Object.keys(toolsByServer)[0]} from "./api/${Object.keys(toolsByServer)[0]}.mjs";`,
    "// ...call functions, filter/aggregate in code, console.log only what you need",
    "```",
    "",
  ];
  for (const [server, tools] of Object.entries(toolsByServer)) {
    index.push(`## ${server}  (api/${server}.mjs, types in api/${server}.d.ts)`);
    const impl = [`import { call } from "./_client.mjs";`];
    const dts = [];
    for (const t of tools) {
      const fn = ident(t.name);
      const first = (t.description || "").split(/\n|(?<=\.)\s/)[0].slice(0, 140);
      index.push(`- \`${fn}(args)\` — ${first}`);
      impl.push(`export const ${fn} = (args = {}) => call(${JSON.stringify(server)}, ${JSON.stringify(t.name)}, args);`);
      dts.push(`/**\n * ${(t.description || "").replace(/\*\//g, "").split("\n").join("\n * ")}\n */`);
      dts.push(`export function ${fn}(args: ${tsType(t.inputSchema)}): Promise<any>;\n`);
    }
    index.push("");
    fs.writeFileSync(path.join(apiDir, `${server}.mjs`), impl.join("\n") + "\n");
    fs.writeFileSync(path.join(apiDir, `${server}.d.ts`), dts.join("\n"));
  }
  fs.writeFileSync(path.join(apiDir, "README.md"), index.join("\n"));
}

// ---------- HTTP bridge ----------
const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") return res.end("ok");
  if (req.method === "GET" && req.url === "/tools") {
    res.setHeader("content-type", "application/json");
    return res.end(JSON.stringify(toolsByServer));
  }
  if (req.method === "POST" && req.url === "/shutdown") {
    res.end("bye");
    for (const c of Object.values(clients)) await c.close().catch(() => {});
    process.exit(0);
  }
  if (req.method !== "POST" || req.url !== "/call") { res.statusCode = 404; return res.end(); }
  let body = "";
  for await (const chunk of req) body += chunk;
  const t0 = Date.now();
  let out;
  try {
    const { server: s, tool, args } = JSON.parse(body);
    if (!clients[s]) throw new Error(`unknown server ${s}`);
    out = await clients[s].callTool({ name: tool, arguments: args || {} });
    if (logFile) {
      const bytes = JSON.stringify(out).length;
      fs.appendFileSync(logFile, JSON.stringify({ ts: t0, server: s, tool, ms: Date.now() - t0, bytes, isError: !!out.isError }) + "\n");
    }
  } catch (e) {
    out = { bridgeError: String(e?.message || e) };
  }
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(out));
});
server.listen(port, "127.0.0.1", () => console.log(`READY ${port}`));
