"""Task definitions and graders.

Each task: servers it needs, whether it scales with a size param, a prompt builder and a grader.
Graders return (score 0..1, detail str). pass == score >= 0.999.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

WEB_BASE = "http://127.0.0.1:8765"
ANSWER_RULE = ("\n\nWhen you are done, put the final answer on the LAST line of your reply, exactly in the form\n"
               "ANSWER: <json>\nwith nothing after it.")


@dataclass
class Task:
    id: str
    servers: list
    sized: bool
    prompt: Callable[[str], str]
    grade: Callable[[Optional[object], dict, Path], tuple]
    note: str = ""


def parse_answer(text: str):
    if not text:
        return None
    m = None
    for m in re.finditer(r"ANSWER:\s*(.+)", text):
        pass
    if not m:
        return None
    raw = m.group(1).strip().strip("`")
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _frac(ok, total):
    return (ok / total) if total else 0.0


# ---------------- graders ----------------
def g_noop(ans, exp, run_dir):
    return (1.0 if ans == "ok" else 0.0), f"got={ans!r}"


def g_first_line(ans, exp, run_dir):
    return (1.0 if isinstance(ans, str) and ans.strip() == exp else 0.0), f"got={ans!r}"


def g_keyword(ans, exp, run_dir):
    if not isinstance(ans, dict):
        return 0.0, "not an object"
    files = set(ans.get("files") or [])
    want = set(exp["files"])
    s_files = _frac(len(files & want), len(want | files))
    s_lines = 1.0 if ans.get("total_lines") == exp["total_lines"] else 0.0
    return 0.7 * s_files + 0.3 * s_lines, f"files {len(files & want)}/{len(want)} (+{len(files - want)} extra), lines={ans.get('total_lines')} want {exp['total_lines']}"


def g_sql_top(ans, exp, run_dir):
    if not isinstance(ans, list):
        return 0.0, "not a list"
    ok = 0
    for got, want in zip(ans, exp):
        if not isinstance(got, dict):
            continue
        if (got.get("name") == want["name"]
                and abs(float(got.get("revenue", -1)) - want["revenue"]) <= 0.05
                and abs(float(got.get("refund_rate", -1)) - want["refund_rate"]) <= 0.0015):
            ok += 1
    return _frac(ok, len(exp)), f"{ok}/{len(exp)} rows correct"


def g_sql_median(ans, exp, run_dir):
    if not isinstance(ans, dict):
        return 0.0, "not an object"
    ok = sum(1 for r, v in exp.items() if isinstance(ans.get(r), (int, float)) and abs(ans[r] - v) <= 0.011)
    return _frac(ok, len(exp)), f"{ok}/{len(exp)} regions correct"


def g_web(ans, exp, run_dir):
    if not isinstance(ans, list):
        return 0.0, "not a list"
    got, want = set(map(str, ans)), set(exp)
    return _frac(len(got & want), len(got | want)), f"{len(got & want)}/{len(want)} found, {len(got - want)} extra"


def _memory(run_dir: Path):
    ents, rels = [], []
    f = run_dir / "memory.jsonl"
    if not f.exists():
        return ents, rels
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        (ents if o.get("type") == "entity" else rels if o.get("type") == "relation" else []).append(o)
    return ents, rels


def g_mem_people(ans, exp, run_dir):
    ents, rels = _memory(run_dir)
    names = {e.get("name") for e in ents}
    want_names = set(exp["people"]) | set(exp["companies"])
    got_pairs = {(r.get("from"), r.get("to")) for r in rels if "work" in str(r.get("relationType", "")).lower()}
    want_pairs = {tuple(p) for p in exp["works_at"]}
    s1 = _frac(len(names & want_names), len(want_names))
    s2 = _frac(len(got_pairs & want_pairs), len(want_pairs))
    return 0.5 * s1 + 0.5 * s2, f"entities {len(names & want_names)}/{len(want_names)}, works_at {len(got_pairs & want_pairs)}/{len(want_pairs)}"


def g_cross(ans, exp, run_dir):
    ents, _ = _memory(run_dir)
    vip = {e.get("name") for e in ents if str(e.get("entityType", "")).lower() == "vip"}
    ok = len(vip & set(exp))
    extra = len(vip - set(exp))
    return max(0.0, _frac(ok, len(exp)) - 0.2 * extra), f"{ok}/{len(exp)} VIPs stored, {extra} extra"


# ---------------- tasks ----------------
TASKS = {t.id: t for t in [
    Task("noop", [], False,
         lambda size: 'Do not use any tools. Just reply.' + ANSWER_RULE.replace("<json>", '"ok"'),
         g_noop, "fixed overhead baseline"),
    Task("fs_first_line", ["fs"], False,
         lambda size: "Using the filesystem server, read readme.txt in its allowed directory and give me its first line. "
                      "Answer JSON: the line as a string." + ANSWER_RULE,
         g_first_line, "single call; Code Mode expected to lose"),
    Task("fs_keyword", ["fs"], False,
         lambda size: "In the filesystem server's allowed directory there is a folder corpus/ with 200 .txt files. "
                      "Find every file whose content contains the word ORCHID, and the total number of lines across those files. "
                      'Answer JSON: {"files": [sorted file names like "doc_001.txt"], "total_lines": <int>}' + ANSWER_RULE,
         g_keyword, "batch / loop"),
    Task("sql_top", ["sqlite"], True,
         lambda size: "The SQLite database has tables customers(id,name,region) and orders(id,customer_id,amount,status,created_at). "
                      "Find the top 5 customers by total amount of orders with status 'paid'. For each give the exact customer name, "
                      "that paid revenue rounded to 2 decimals, and refund_rate = (number of their 'refunded' orders) / (number of all their orders), rounded to 3 decimals. "
                      'Answer JSON: [{"name":..., "revenue":..., "refund_rate":...}, ...] ordered by revenue desc.' + ANSWER_RULE,
         g_sql_top, "aggregation SQL can do"),
    Task("sql_median", ["sqlite"], True,
         lambda size: "The SQLite database has tables customers(id,name,region) and orders(id,customer_id,amount,status,created_at). "
                      "For each customer region, compute the MEDIAN amount of orders with status 'paid' (standard median: mean of the two middle values for even counts), rounded to 2 decimals. "
                      'Answer JSON: {"<region>": <median>, ...} for all regions.' + ANSWER_RULE,
         g_sql_median, "large result set (SQLite has no MEDIAN)"),
    Task("web_cheap_tools", ["web"], True,
         lambda size: f"Open these three catalog pages in the browser: {WEB_BASE}/catalog_{size}_1.html, {WEB_BASE}/catalog_{size}_2.html, {WEB_BASE}/catalog_{size}_3.html. "
                      "List the names (the h3 heading text) of all products in category 'tools' whose price is below $20.00. "
                      'Answer JSON: [sorted list of product names].' + ANSWER_RULE,
         g_web, "huge tool results (page snapshots)"),
    Task("mem_people", ["fs", "memory"], False,
         lambda size: "Read people.csv (columns name,company,city) from the filesystem server's allowed directory. Store it in the memory knowledge graph: "
                      "one entity per person (entityType 'person', with an observation 'lives in <city>'), one entity per company (entityType 'company'), "
                      "and a relation from each person to their company with relationType 'works_at'. "
                      'Answer JSON: {"people": <count>, "companies": <count>}' + ANSWER_RULE,
         g_mem_people, "cross-server data movement, many writes"),
    Task("cross_sql_mem", ["sqlite", "memory"], True,
         lambda size: "The SQLite database has tables customers(id,name,region) and orders(id,customer_id,amount,status,created_at). "
                      "Find the 3 customers with the highest total amount of 'paid' orders and store each in the memory knowledge graph as an entity "
                      "named exactly as the customer name, entityType 'VIP', with an observation 'paid revenue <amount>'. "
                      'Answer JSON: [the 3 names]' + ANSWER_RULE,
         g_cross, "chain across servers"),
]}

ALL_SERVERS = ["fs", "sqlite", "web", "memory"]
