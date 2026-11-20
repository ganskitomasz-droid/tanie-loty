"""Jednorazowe zapytanie na opublikowanych wynikach (results-*.json ze strony).

python -m tanie_loty.query zapytania/plik.json
Łączy dowolne loty z plików wszystkich miast (np. wylot z Krakowa, powrót do Warszawy),
filtruje po datach, godzinach, nocach i krajach, wypisuje najtańsze propozycje.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request

d = dt.date.fromisoformat


def load(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def main(path: str) -> int:
    q = json.load(open(path, encoding="utf-8"))
    base = q["pages_url"].rstrip("/")
    legs, airports = [], {}
    for g in q["groups"]:
        data = load(f"{base}/results-{g}.json")
        legs += data["legs"]
        airports.update(data["airports"])
    uniq = {json.dumps({k: v for k, v in l.items() if k != "h"}, sort_keys=True): l for l in legs}
    legs = list(uniq.values())

    def country_ok(code):
        a = airports.get(code) or {}
        if code in q.get("exclude_airports", []):
            return False
        return a.get("cc") in q["countries"]

    outs = [l for l in legs if l["from"] in q["out_from"] and q["out_dates"][0] <= l["date"] <= q["out_dates"][1]
            and (l.get("time") or "99") <= q["out_latest"] and country_ok(l["to"])]
    rets = [l for l in legs if l["to"] in q["ret_to_any"] + sum(q.get("ret_to_extra", {}).values(), [])
            and l["date"] <= q["ret_latest_date"] and (l.get("time") or "00") >= q["ret_earliest"]]
    res = []
    for o in outs:
        allowed = set(q["ret_to_any"]) | set(q.get("ret_to_extra", {}).get(o["from"], []))
        for r in rets:
            if r["from"] != o["to"] or r["to"] not in allowed:
                continue
            n = (d(r["date"]) - d(o.get("arr_date") or o["date"])).days
            if q["nights"][0] <= n <= q["nights"][1]:
                res.append((o["price"] + r["price"], n, o, r))
    res.sort(key=lambda x: x[0])
    per, out = {}, []
    for t in res:
        k = t[2]["to"]
        if per.get(k, 0) < q.get("per_dest", 2):
            per[k] = per.get(k, 0) + 1
            out.append(t)
    lines = []
    for total, n, o, r in out[: q.get("limit", 40)]:
        a = airports.get(o["to"]) or {}
        lines.append(f"{total} zł | {a.get('city') or o['to']} ({o['to']}, {a.get('country','')}) | {n} n. | "
                     f"{o['carrier']} {o['from']}→{o['to']} {o['date']} {o.get('time') or '?'}→{o.get('arr') or '?'} {o['price']} zł | "
                     f"{r['carrier']} {r['from']}→{r['to']} {r['date']} {r.get('time') or '?'}→{r.get('arr') or '?'} {r['price']} zł")
    print(f"Znaleziono {len(res)} kombinacji, kierunków: {len(per)}")
    print("\n".join(lines))
    if os.environ.get("GITHUB_ACTIONS"):
        chunk = []
        for ln in [f"Kombinacji: {len(res)}, kierunków: {len(per)}"] + lines:
            chunk.append(ln)
            if sum(len(x) + 1 for x in chunk) > 3000:
                print("::notice title=Zapytanie::" + "%0A".join(x.replace("%", "%25") for x in chunk[:-1]))
                chunk = [ln]
        if chunk:
            print("::notice title=Zapytanie::" + "%0A".join(x.replace("%", "%25") for x in chunk))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
