"""Aggregate a batch into summary.csv + report.md (+ charts if matplotlib is available).

  python report.py runs/<batch>
  uv run --with matplotlib report.py runs/<batch>     # with charts
  prices (optional, per 1M tokens): --price-in 0.0 --price-cache 0.0 --price-out 0.0
"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

METRICS = ["total_input_tokens", "cache_read_tokens", "output_tokens", "peak_context", "requests",
           "tool_calls", "codemode_mcp_calls", "wall_ms", "model_ms", "first_req_tools_chars"]


def pct(vals, p):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("batch")
    ap.add_argument("--price-in", type=float, default=None, help="USD or CNY per 1M uncached input tokens")
    ap.add_argument("--price-cache", type=float, default=None, help="per 1M cache-hit input tokens")
    ap.add_argument("--price-out", type=float, default=None, help="per 1M output tokens")
    a = ap.parse_args()
    bdir = Path(a.batch)
    rows = [json.loads(l) for l in (bdir / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    def cost(r):
        if a.price_in is None or a.price_out is None:
            return None
        pc = a.price_cache if a.price_cache is not None else a.price_in
        return (r["input_tokens"] + r["cache_creation_tokens"]) / 1e6 * a.price_in + r["cache_read_tokens"] / 1e6 * pc + r["output_tokens"] / 1e6 * a.price_out

    groups = defaultdict(list)
    for r in rows:
        r["cost"] = cost(r)
        groups[(r["task"], r["size"], r["toolset"], r["mode"])].append(r)

    out_rows = []
    for (task, size, toolset, mode), rs in sorted(groups.items()):
        row = {"task": task, "size": size, "toolset": toolset, "mode": mode, "n": len(rs),
               "pass_rate": round(sum(r["pass"] for r in rs) / len(rs), 3),
               "mean_score": round(statistics.mean(r["score"] for r in rs), 3),
               "timeouts": sum(r["timed_out"] for r in rs)}
        for m in METRICS + (["cost"] if rs[0]["cost"] is not None else []):
            vals = [r.get(m) for r in rs if isinstance(r.get(m), (int, float))]
            row[f"{m}_med"] = round(statistics.median(vals), 4) if vals else None
            row[f"{m}_p90"] = round(pct(vals, 0.9), 4) if vals else None
        out_rows.append(row)

    with open(bdir / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    # markdown: one table per (task,size,toolset) comparing modes, plus B/A ratio
    lines = [f"# Benchmark report: {bdir.name}", "",
             "Medians over reps. `in` = total input tokens incl. cache hits; `peak` = largest single-request context; "
             "`req` = model round-trips (excluding Claude Code side requests); `calls` = MCP tool calls.", ""]
    by_case = defaultdict(dict)
    for r in out_rows:
        by_case[(r["task"], r["size"], r["toolset"])][r["mode"]] = r
    lines.append("| task | size | toolset | mode | pass | in | cached | out | peak | req | calls | wall s | in vs A |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for (task, size, toolset), modes in by_case.items():
        base = modes.get("A", {}).get("total_input_tokens_med")
        for mode in sorted(modes):
            r = modes[mode]
            calls = r["tool_calls_med"] if mode != "B" else r["codemode_mcp_calls_med"]
            ratio = f"{r['total_input_tokens_med'] / base:.2f}x" if base else "-"
            lines.append(f"| {task} | {size} | {toolset} | {mode} | {r['pass_rate']:.0%} | {r['total_input_tokens_med']:,.0f} | "
                         f"{r['cache_read_tokens_med']:,.0f} | {r['output_tokens_med']:,.0f} | {r['peak_context_med']:,.0f} | "
                         f"{r['requests_med']:.0f} | {calls if calls is not None else '-'} | {r['wall_ms_med']/1000:.1f} | {ratio} |")
    (bdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not installed: skip charts; run with `uv run --with matplotlib report.py ...`)")
        return
    colors = {"A": "#4C78A8", "B": "#F58518", "C": "#54A24B"}
    # 1) scaling with size for sized tasks
    sized = sorted({r["task"] for r in out_rows if r["size"] != "-"})
    order = {"s": 0, "m": 1, "l": 2}
    for ts in sorted({r["toolset"] for r in out_rows}):
        if not sized:
            break
        fig, axes = plt.subplots(1, len(sized), figsize=(4.2 * len(sized), 3.6), squeeze=False)
        for ax, task in zip(axes[0], sized):
            for mode in "ABC":
                pts = sorted([r for r in out_rows if r["task"] == task and r["toolset"] == ts and r["mode"] == mode],
                             key=lambda r: order.get(r["size"], 9))
                if pts:
                    ax.plot([p["size"] for p in pts], [p["total_input_tokens_med"] for p in pts], "o-", color=colors[mode], label=mode)
            ax.set_title(task, fontsize=10)
            ax.set_xlabel("data size")
            ax.set_ylabel("input tokens (median)")
            ax.grid(alpha=.3)
            ax.legend()
        fig.suptitle(f"Input tokens vs data size  (toolset={ts})")
        fig.tight_layout()
        fig.savefig(bdir / f"scaling_{ts}.png", dpi=130)
    # 2) toolset overhead: minimal vs all per mode (all tasks summed medians)
    fig, ax = plt.subplots(figsize=(6, 3.6))
    tsets = sorted({r["toolset"] for r in out_rows})
    w = 0.25
    for i, mode in enumerate("ABC"):
        vals = [sum(r["total_input_tokens_med"] for r in out_rows if r["mode"] == mode and r["toolset"] == t) for t in tsets]
        ax.bar([x + i * w for x in range(len(tsets))], vals, w, color=colors[mode], label=mode)
    ax.set_xticks([x + w for x in range(len(tsets))], tsets)
    ax.set_ylabel("sum of median input tokens")
    ax.set_title("Tool-definition overhead: minimal vs all servers")
    ax.legend()
    fig.tight_layout()
    fig.savefig(bdir / "toolset_overhead.png", dpi=130)
    print(f"\ncharts written to {bdir}")


if __name__ == "__main__":
    main()
