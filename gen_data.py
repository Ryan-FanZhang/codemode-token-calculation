"""Generate deterministic mock data + expected answers for the benchmark.

Usage: python gen_data.py
Outputs into ./data and ./data/expected.json
"""
import csv
import json
import random
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SEED = 20261126
SIZES = {"s": 1_000, "m": 10_000, "l": 50_000}          # sqlite order rows
WEB_SIZES = {"s": 40, "m": 250, "l": 800}               # products per page
REGIONS = ["north", "south", "east", "west", "central"]
WORDS = ("alpha beta gamma delta river stone cloud maple ember quartz lumen "
         "harbor vector signal meadow copper falcon cedar prism orbit").split()
FIRST = "Ada Ben Cleo Dara Eli Fay Gus Hana Ivo Juno Kai Lena Milo Nia Omar Pia Quin Rhea Sami Tova".split()
LAST = "Park Moss Reed Hale Voss Lund Cruz Ito Nash Okoye Berg Kahn Diaz Wolfe Shaw".split()
CATS = ["tools", "garden", "kitchen", "toys", "office"]

expected = {}


def gen_fs(rng):
    fs = DATA / "fs"
    corpus = fs / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    (fs / "readme.txt").write_text("Benchmark corpus v1 - do not edit\nSecond line.\n", encoding="utf-8")
    expected["fs_first_line"] = "Benchmark corpus v1 - do not edit"

    hits, total = [], 0
    for i in range(200):
        n_lines = rng.randint(20, 120)
        lines = [" ".join(rng.choices(WORDS, k=rng.randint(6, 14))) for _ in range(n_lines)]
        name = f"doc_{i:03d}.txt"
        if rng.random() < 0.12:
            lines[rng.randrange(n_lines)] += " ORCHID"
            hits.append(name)
            total += n_lines
        (corpus / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    expected["fs_keyword"] = {"files": sorted(hits), "total_lines": total}

    companies = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay"]
    people = []
    used = set()
    while len(people) < 30:
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if name in used:
            continue
        used.add(name)
        people.append({"name": name, "company": rng.choice(companies), "city": rng.choice(["Oslo", "Lima", "Pune", "Kyiv"])})
    with open(fs / "people.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "company", "city"])
        w.writeheader()
        w.writerows(people)
    expected["mem_people"] = {
        "people": sorted(p["name"] for p in people),
        "companies": sorted({p["company"] for p in people}),
        "works_at": sorted([p["name"], p["company"]] for p in people),
    }


def gen_sqlite(rng):
    for tag, n_orders in SIZES.items():
        path = DATA / f"shop_{tag}.db"
        path.unlink(missing_ok=True)
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, region TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL,
                                status TEXT, created_at TEXT);
        """)
        n_cust = max(50, n_orders // 20)
        custs = [(i, f"{rng.choice(FIRST)} {rng.choice(LAST)} #{i}", rng.choice(REGIONS)) for i in range(1, n_cust + 1)]
        con.executemany("INSERT INTO customers VALUES(?,?,?)", custs)
        orders = []
        for oid in range(1, n_orders + 1):
            cid = rng.randint(1, n_cust)
            amt = round(rng.lognormvariate(3.5, 0.8), 2)
            status = "refunded" if rng.random() < 0.08 else "paid"
            orders.append((oid, cid, amt, status, f"2026-{rng.randint(1,10):02d}-{rng.randint(1,28):02d}"))
        con.executemany("INSERT INTO orders VALUES(?,?,?,?,?)", orders)
        con.commit()

        cname = {c[0]: c[1] for c in custs}
        cregion = {c[0]: c[2] for c in custs}
        paid = {}
        cnt = {}
        ref = {}
        per_region = {r: [] for r in REGIONS}
        for _, cid, amt, st, _ in orders:
            cnt[cid] = cnt.get(cid, 0) + 1
            if st == "paid":
                paid[cid] = paid.get(cid, 0) + amt
                per_region[cregion[cid]].append(amt)
            else:
                ref[cid] = ref.get(cid, 0) + 1
        top = sorted(paid, key=lambda c: -paid[c])[:5]
        expected[f"sql_top@{tag}"] = [
            {"name": cname[c], "revenue": round(paid[c], 2), "refund_rate": round(ref.get(c, 0) / cnt[c], 3)} for c in top
        ]
        expected[f"sql_median@{tag}"] = {r: round(statistics.median(v), 2) for r, v in per_region.items()}
        expected[f"cross_sql_mem@{tag}"] = [cname[c] for c in top[:3]]
        con.close()


def gen_web(rng):
    web = DATA / "web"
    web.mkdir(parents=True, exist_ok=True)
    for tag, n in WEB_SIZES.items():
        hits = []
        for page in (1, 2, 3):
            rows = []
            for i in range(n):
                name = f"{rng.choice(WORDS).title()} {rng.choice(WORDS).title()} {page}{i:03d}"
                cat = rng.choice(CATS)
                price = round(rng.uniform(3, 120), 2)
                if cat == "tools" and price < 20:
                    hits.append(name)
                rows.append(
                    f'<div class="card"><h3>{name}</h3><p class="cat">Category: {cat}</p>'
                    f'<p class="price">${price:.2f}</p><p class="desc">{" ".join(rng.choices(WORDS, k=12))}</p>'
                    f'<button>Add to cart</button></div>'
                )
            nav = " | ".join(f'<a href="catalog_{tag}_{p}.html">Page {p}</a>' for p in (1, 2, 3))
            html = (f"<!doctype html><html><head><title>Catalog {tag} page {page}</title></head><body>"
                    f"<nav>{nav}</nav><h1>Catalog page {page}</h1>{''.join(rows)}</body></html>")
            (web / f"catalog_{tag}_{page}.html").write_text(html, encoding="utf-8")
        expected[f"web_cheap_tools@{tag}"] = sorted(hits)


def main():
    rng = random.Random(SEED)
    DATA.mkdir(exist_ok=True)
    gen_fs(rng)
    gen_sqlite(rng)
    gen_web(rng)
    (DATA / "expected.json").write_text(json.dumps(expected, indent=2, ensure_ascii=False), encoding="utf-8")
    print("data written to", DATA)
    print({k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in expected.items()})


if __name__ == "__main__":
    main()
