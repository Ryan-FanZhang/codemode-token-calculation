# Code Mode vs MCP benchmark (Claude Code harness + DeepSeek)

| file | role |
|---|---|
| `gen_data.py` | deterministic mock data (200 txt files, SQLite s/m/l = 1k/10k/50k orders, catalog pages) + `data/expected.json` |
| `servers.json` | the 4 MCP servers: fs, sqlite, web (Playwright), memory |
| `codemode/daemon.mjs` | Code Mode bridge: holds MCP connections, generates `./api/*.mjs` + `.d.ts` + README for the model |
| `proxy.mjs` | metering proxy between Claude Code and DeepSeek, one JSON line per model request |
| `mock_upstream.mjs` | fake model for `--dry-run` |
| `tasks.py` | 8 tasks + graders |
| `run.py` | runs the matrix task x mode x toolset x size x rep |
| `report.py` | summary.csv, report.md, charts |

Modes: **A** plain MCP (all tool schemas up front) · **B** Code Mode (Bash + generated JS API) · **C** MCP with Claude Code tool search (`defer_loading`).

## Run

```powershell
npm install
python gen_data.py
python run.py --dry-run --reps 1                      # plumbing check, no cost
$env:DEEPSEEK_API_KEY = "sk-..."
python run.py --model <deepseek-model-id> --tasks noop,fs_first_line,sql_top --modes A,B,C --toolsets minimal --sizes s --reps 2 --batch pilot
python run.py --model <deepseek-model-id> --reps 5 --batch full
uv run --with matplotlib report.py runs/full --price-in <p> --price-cache <p> --price-out <p>
```

Per-run artifacts live in `runs/<batch>/<run_id>/`: `requests.jsonl` (every model call with usage),
`claude_stdout.json`, `codemode_calls.jsonl` (B), `memory.jsonl`, `result.json`.

## Known caveats
- Claude Code adds ~2 small side requests per run; counted separately as `aux_*`, excluded from main metrics.
- Mode B carries Claude Code's Bash/Read/Write tool schemas (~3.5k tokens) as a fixed cost.
- Mode C only saves tokens if the upstream honors `defer_loading`; if DeepSeek ignores it, C ≈ A (that is itself a finding).
- `MAX_MCP_OUTPUT_TOKENS` defaults to 25000 in Claude Code; big MCP results are truncated in A/C (`--max-mcp-output-tokens` to change).
- In B the model has Bash and could read data files directly; check `requests.jsonl` tool inputs if a result looks suspicious.
