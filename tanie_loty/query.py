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
import math
import re
import urllib.request


def clean(t: str) -> str:
    """Usuwa znaki sterujące i separatory linii, które ucinają adnotacje GitHub Actions."""
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029\u0085]", " ", str(t))

d = dt.date.fromisoformat


def km(a: dict, b: dict) -> float:
    if not a or not b or None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return 1e9
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b["lon"] - a["lon"]) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def load(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def routes(q: dict) -> int:
    """Porównanie siatki połączeń w dwóch miesiącach: nowe / znikające trasy z danych lotnisk."""
    base = q["pages_url"].rstrip("/")
    legs, airports = [], {}
    for g in q["groups"]:
        data = load(f"{base}/results-{g}.json")
        legs += data["legs"]
        airports.update(data["airports"])
    m1, m2 = q["months"]
    days = {}
    for l in legs:
        if l["from"] in q["origins"]:
            k = (l["carrier"], l["from"], l["to"])
            days.setdefault(k, {}).setdefault(l["date"][:7], set()).add(l["date"])
    city = lambda c: clean((airports.get(c) or {}).get("city") or c)
    new, gone, more = [], [], []
    for (car, o, dst), by in sorted(days.items()):
        a, b = len(by.get(m1, ())), len(by.get(m2, ()))
        first = min(by.get(m2, {"-"}))
        label = f"{car} {o}→{city(dst)} ({dst})"
        if not a and b:
            new.append(f"{label}: od {first}, {b} dni z lotami w {m2}")
        elif a and not b:
            gone.append(f"{label}: ostatni {max(by[m1])}, {a} dni w {m1}")
        elif a and b and abs(b - a) >= q.get("min_diff_days", 3):
            more.append((b - a, f"{label}: {a} → {b} dni z lotami ({'+' if b > a else ''}{b - a})"))
    # trasy w obie strony nieobecne w jednym miesiącu u żadnego przewoźnika (z dowolnego lotniska grupy)
    lines = [f"NOWE w {m2} (brak lotów w {m1}): {len(new)}"] + new + \
            [f"ZNIKAJĄ w {m2} (były w {m1}): {len(gone)}"] + gone + \
            [f"ZMIANA CZĘSTOTLIWOŚCI (dni z lotami w miesiącu): {len(more)}"] + \
            [t for _, t in sorted(more, key=lambda x: -x[0])]
    print("\n".join(lines))
    if os.environ.get("GITHUB_ACTIONS"):
        chunk = []
        for ln in lines:
            chunk.append(ln)
            if sum(len(x) + 1 for x in chunk) > 3000:
                print("::notice title=Siatka::" + "%0A".join(x.replace("%", "%25") for x in chunk[:-1]))
                chunk = [ln]
        if chunk:
            print("::notice title=Siatka::" + "%0A".join(x.replace("%", "%25") for x in chunk))
    return 0


def main(path: str) -> int:
    q = json.load(open(path, encoding="utf-8"))
    if q.get("analysis") == "routes":
        return routes(q)
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
            and q.get("ret_earliest_date", "0000") <= l["date"] <= q["ret_latest_date"] and (l.get("time") or "00") >= q["ret_earliest"]
            and country_ok(l["from"])]
    oj_km = q.get("openjaw_km", 0)
    res = []
    for o in outs:
        allowed = set(q["ret_to_any"]) | set(q.get("ret_to_extra", {}).get(o["from"], []))
        for r in rets:
            if r["to"] not in allowed:
                continue
            dist = 0 if r["from"] == o["to"] else km(airports.get(o["to"]), airports.get(r["from"]))
            if dist > oj_km or (q.get("openjaw_only") and not dist):
                continue
            if dist and q.get("openjaw_same_country") and \
                    (airports.get(o["to"]) or {}).get("cc") != (airports.get(r["from"]) or {}).get("cc"):
                continue
            n = (d(r["date"]) - d(o.get("arr_date") or o["date"])).days
            if q["nights"][0] <= n <= q["nights"][1]:
                res.append((o["price"] + r["price"], n, o, r, round(dist)))
    res.sort(key=lambda x: x[0])
    per, out = {}, []
    for t in res:
        k = (t[2]["to"], t[3]["from"])
        if per.get(k, 0) < q.get("per_dest", 2):
            per[k] = per.get(k, 0) + 1
            out.append(t)
    lines = []
    for total, n, o, r, dist in out[: q.get("limit", 40)]:
        a = airports.get(o["to"]) or {}
        b = airports.get(r["from"]) or {}
        place = clean(f"{a.get('city') or o['to']} ({o['to']}, {a.get('country','')})")
        if dist:
            place += clean(f" → powrót z {b.get('city') or r['from']} ({r['from']}), {dist} km")
        lines.append(f"{total} zł | {place} | {n} n. | "
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
