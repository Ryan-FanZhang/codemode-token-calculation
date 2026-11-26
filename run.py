"""Benchmark runner: Claude Code (harness) x DeepSeek (model) x {MCP, Code Mode, MCP+tool search}.

Modes
  A  plain MCP: all tool definitions loaded up front (ENABLE_TOOL_SEARCH=false), no built-in tools
  B  Code Mode: no MCP tools; a generated JS API in ./api, model writes & runs node scripts via Bash
  C  MCP + Claude Code tool search (ENABLE_TOOL_SEARCH=true), no built-in tools

Examples
  $env:DEEPSEEK_API_KEY="sk-..."
  python run.py --model <deepseek-model-id> --tasks fs_keyword,sql_median --modes A,B,C --sizes s,m --reps 5
  python run.py --dry-run --reps 1            # mock model, checks plumbing + fixed overhead, no cost
"""
import argparse
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from functools import partial
from pathlib import Path

from tasks import ALL_SERVERS, TASKS, parse_answer

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
EXPECTED = json.loads((DATA / "expected.json").read_text(encoding="utf-8"))
SERVERS = json.loads((ROOT / "servers.json").read_text(encoding="utf-8"))
PROXY_PORT, MOCK_PORT, WEB_PORT, BRIDGE_PORT = 8788, 8799, 8765, 8790

CODEMODE_PROMPT = """You have NO direct MCP tools. All data access goes through a JavaScript API generated from the MCP servers, in ./api (read ./api/README.md first; parameter types are in ./api/<server>.d.ts).
Work by writing Node.js ES module scripts (.mjs) in the current directory that import from ./api/<server>.mjs, and run them with `node <file>.mjs` via Bash.
Do filtering, joins and aggregation inside the script and print only what you need to see. Do not read data files directly with shell commands; always go through the API."""
MCP_PROMPT = "Use the available MCP tools to access data."


def find_claude_cli():
    exe = shutil.which("claude")
    if not exe:
        sys.exit("claude CLI not found on PATH")
    cand = Path(exe).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    return ["node", str(cand)] if cand.exists() else [exe]


def resolve_servers(names, run_dir):
    out = {}
    subs = {"{ROOT}": ROOT.as_posix(), "{DATA}": DATA.as_posix(), "{RUN}": run_dir.as_posix()}

    def sub(s):
        for k, v in subs.items():
            s = s.replace(k, v)
        return s
    for n in names:
        d = SERVERS[n]
        out[n] = {"command": d["command"], "args": [sub(a) for a in d["args"]],
                  **({"env": {k: sub(v) for k, v in d["env"].items()}} if d.get("env") else {})}
    return out


def start_web_server():
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(DATA / "web"))
    handler.log_message = lambda *a, **k: None
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", WEB_PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def http_post(url, obj=None, timeout=5):
    req = urllib.request.Request(url, data=json.dumps(obj or {}).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def wait_http(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def kill_tree(p):
    if p and p.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True) if os.name == "nt" else p.kill()


def summarize_requests(path: Path):
    recs = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    all_msgs = [r for r in recs if r.get("path", "").split("?")[0].endswith("/v1/messages")]
    # Claude Code fires small tool-less side requests (topic/title checks). Count them apart.
    is_aux = lambda r: not r.get("n_tools") and (r.get("system_chars") or 0) < 6000
    aux = [r for r in all_msgs if is_aux(r)]
    msgs = [r for r in all_msgs if not is_aux(r)]
    aux_in = sum(((r.get("usage") or {}).get("input_tokens") or 0) + ((r.get("usage") or {}).get("cache_read_input_tokens") or 0) for r in aux)
    s = {"aux_requests": len(aux), "aux_input_tokens": aux_in,
         "first_req_deferred_tools": msgs[0].get("n_deferred_tools") if msgs else None,
         "requests": len(msgs), "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
         "cache_creation_tokens": 0, "peak_context": 0, "model_ms": 0, "http_errors": 0, "tool_calls": 0,
         "first_req_tools": msgs[0].get("n_tools") if msgs else None,
         "first_req_tools_chars": msgs[0].get("tools_chars") if msgs else None,
         "first_req_system_chars": msgs[0].get("system_chars") if msgs else None}
    for r in msgs:
        u = r.get("usage") or {}
        i, o = u.get("input_tokens") or 0, u.get("output_tokens") or 0
        cr, cc = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
        s["input_tokens"] += i
        s["output_tokens"] += o
        s["cache_read_tokens"] += cr
        s["cache_creation_tokens"] += cc
        s["peak_context"] = max(s["peak_context"], i + cr + cc)
        s["model_ms"] += r.get("ms") or 0
        s["http_errors"] += 1 if (r.get("status") or 200) >= 400 else 0
        s["tool_calls"] += len(r.get("tool_uses") or [])
    s["total_input_tokens"] = s["input_tokens"] + s["cache_read_tokens"] + s["cache_creation_tokens"]
    return s, msgs


def run_one(cfg, batch_dir, task, mode, toolset, size, rep, cli):
    run_id = f"{task.id}__{mode}__{toolset}__{size or '-'}__r{rep}"
    run_dir = batch_dir / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    work = run_dir / "work"
    work.mkdir(parents=True)
    names = ALL_SERVERS if toolset == "all" else task.servers
    if "sqlite" in names:
        shutil.copy(DATA / f"shop_{size or 's'}.db", run_dir / "shop.db")
    resolved = resolve_servers(names, run_dir)
    (run_dir / "servers.resolved.json").write_text(json.dumps(resolved, indent=2), encoding="utf-8")

    env = {**os.environ,
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PROXY_PORT}",
           "ANTHROPIC_AUTH_TOKEN": cfg.api_key or "dry-run",
           "ANTHROPIC_API_KEY": "",
           "ANTHROPIC_MODEL": cfg.model,
           "ANTHROPIC_SMALL_FAST_MODEL": cfg.model,
           "ANTHROPIC_DEFAULT_HAIKU_MODEL": cfg.model,
           "ANTHROPIC_DEFAULT_SONNET_MODEL": cfg.model,
           "ANTHROPIC_DEFAULT_OPUS_MODEL": cfg.model,
           "CLAUDE_CONFIG_DIR": str(ROOT / ".claude_home"),
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
           "DISABLE_AUTOUPDATER": "1",
           "MCP_TIMEOUT": "60000",
           "ENABLE_TOOL_SEARCH": "true" if mode == "C" else "false"}
    if cfg.max_mcp_output_tokens:
        env["MAX_MCP_OUTPUT_TOKENS"] = str(cfg.max_mcp_output_tokens)

    args = cli + ["-p", "--output-format", "json", "--model", cfg.model,
                  "--setting-sources", "project", "--strict-mcp-config"]
    bridge = None
    if mode in ("A", "C"):
        mcp_file = run_dir / "mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": resolved}, indent=2), encoding="utf-8")
        if resolved:
            args += ["--mcp-config", str(mcp_file), "--allowedTools", *[f"mcp__{n}" for n in resolved]]
        args += ["--tools", "", "--append-system-prompt", MCP_PROMPT]
    else:  # B
        if resolved:
            bridge = subprocess.Popen(
                ["node", str(ROOT / "codemode" / "daemon.mjs"), "--servers", str(run_dir / "servers.resolved.json"),
                 "--port", str(BRIDGE_PORT), "--api-dir", str(work / "api"), "--log", str(run_dir / "codemode_calls.jsonl")],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if not wait_http(f"http://127.0.0.1:{BRIDGE_PORT}/health", 120):
                kill_tree(bridge)
                raise RuntimeError("code-mode bridge failed to start")
        args += ["--tools", "Bash,Read,Write", "--allowedTools", "Bash", "Read", "Write",
                 "--append-system-prompt", CODEMODE_PROMPT]

    http_post(f"http://127.0.0.1:{PROXY_PORT}/_tag", {"tag": run_id, "file": str(run_dir / "requests.jsonl")})
    prompt = task.prompt(size)
    t0 = time.time()
    timed_out = False
    try:
        cp = subprocess.run(args, input=prompt, cwd=work, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=cfg.timeout)
        stdout, stderr, code = cp.stdout, cp.stderr, cp.returncode
    except subprocess.TimeoutExpired as e:
        stdout, stderr, code, timed_out = (e.stdout or ""), (e.stderr or ""), -1, True
        stdout = stdout if isinstance(stdout, str) else stdout.decode("utf-8", "replace")
    wall_ms = int((time.time() - t0) * 1000)
    if bridge:
        try:
            http_post(f"http://127.0.0.1:{BRIDGE_PORT}/shutdown", timeout=10)
        except Exception:
            pass
        time.sleep(0.5)
        kill_tree(bridge)

    (run_dir / "claude_stdout.json").write_text(stdout or "", encoding="utf-8")
    (run_dir / "claude_stderr.txt").write_text(stderr or "", encoding="utf-8")
    try:
        cj = json.loads(stdout)
    except Exception:
        cj = {}
    result_text = cj.get("result", "")
    ans = parse_answer(result_text)
    exp_key = f"{task.id}@{size}" if task.sized else task.id
    exp = EXPECTED.get(exp_key, "ok")
    try:
        score, detail = task.grade(ans, exp, run_dir)
    except Exception as e:
        score, detail = 0.0, f"grader error: {e}"

    req_sum, _ = summarize_requests(run_dir / "requests.jsonl")
    cm_calls = 0
    if (run_dir / "codemode_calls.jsonl").exists():
        cm_calls = len((run_dir / "codemode_calls.jsonl").read_text(encoding="utf-8").splitlines())

    rec = {"batch": batch_dir.name, "run_id": run_id, "task": task.id, "mode": mode, "toolset": toolset,
           "size": size or "-", "rep": rep, "model": cfg.model, "dry_run": cfg.dry_run,
           "exit_code": code, "timed_out": timed_out, "wall_ms": wall_ms,
           "cc_num_turns": cj.get("num_turns"), "cc_duration_ms": cj.get("duration_ms"),
           "cc_duration_api_ms": cj.get("duration_api_ms"), "cc_is_error": cj.get("is_error"),
           **req_sum, "codemode_mcp_calls": cm_calls,
           "score": round(score, 4), "pass": score >= 0.999, "grade_detail": detail,
           "answer": ans if not isinstance(ans, str) else ans[:500]}
    (run_dir / "result.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(batch_dir / "results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", ""))
    ap.add_argument("--upstream", default=os.environ.get("UPSTREAM", "https://api.deepseek.com/anthropic"))
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--modes", default="A,B,C")
    ap.add_argument("--toolsets", default="minimal,all")
    ap.add_argument("--sizes", default="s,m,l")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--max-mcp-output-tokens", type=int, default=0, help="0 = Claude Code default (25000)")
    ap.add_argument("--batch", default="")
    ap.add_argument("--dry-run", action="store_true", help="use a mock model instead of DeepSeek")
    cfg = ap.parse_args()
    cfg.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if cfg.dry_run:
        cfg.model = cfg.model or "mock-model"
        cfg.upstream = f"http://127.0.0.1:{MOCK_PORT}"
    elif not cfg.api_key or not cfg.model:
        sys.exit("set DEEPSEEK_API_KEY and --model (or DEEPSEEK_MODEL)")

    batch_dir = ROOT / "runs" / (cfg.batch or datetime.now().strftime("%Y%m%d-%H%M%S") + ("-dry" if cfg.dry_run else ""))
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "config.json").write_text(json.dumps({k: v for k, v in vars(cfg).items() if k != "api_key"}, indent=2), encoding="utf-8")

    procs = []
    if cfg.dry_run:
        procs.append(subprocess.Popen(["node", str(ROOT / "mock_upstream.mjs")], cwd=ROOT, stdout=subprocess.DEVNULL))
    procs.append(subprocess.Popen(["node", str(ROOT / "proxy.mjs")], cwd=ROOT, stdout=subprocess.DEVNULL,
                                  env={**os.environ, "UPSTREAM": cfg.upstream, "PROXY_PORT": str(PROXY_PORT),
                                       "PROXY_LOG": str(batch_dir / "proxy_untagged.jsonl")}))
    time.sleep(1.5)
    web = start_web_server()
    cli = find_claude_cli()

    plan = []
    for tid in cfg.tasks.split(","):
        t = TASKS[tid]
        for mode in cfg.modes.split(","):
            for ts in cfg.toolsets.split(","):
                for size in (cfg.sizes.split(",") if t.sized else [None]):
                    for rep in range(1, cfg.reps + 1):
                        plan.append((t, mode, ts, size, rep))
    print(f"{len(plan)} runs -> {batch_dir}")
    try:
        for i, (t, mode, ts, size, rep) in enumerate(plan, 1):
            try:
                r = run_one(cfg, batch_dir, t, mode, ts, size, rep, cli)
                print(f"[{i}/{len(plan)}] {r['run_id']}: pass={r['pass']} score={r['score']} "
                      f"in={r['total_input_tokens']} out={r['output_tokens']} req={r['requests']} "
                      f"tools0={r['first_req_tools']} {r['wall_ms']/1000:.1f}s  {r['grade_detail']}", flush=True)
            except Exception as e:
                print(f"[{i}/{len(plan)}] {t.id} {mode} {ts} {size} r{rep}: ERROR {e}", flush=True)
    finally:
        web.shutdown()
        for p in procs:
            kill_tree(p)


if __name__ == "__main__":
    main()
