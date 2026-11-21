"""Sprawdzenie na żywo wybranych tras w dowolnym okresie (np. marzec), bez pełnego wyszukiwania.

Wywoływane z query.py, gdy plik zapytania ma "analysis": "probe":
  {"routes": [["WMI","STN"], ["WAW","LTN"]], "from": "2027-03-01", "to": "2027-03-31",
   "nights": [2,4], "home": ["WAW","WMI"], "limit": 30}
Każda trasa sprawdzana w obie strony (Ryanair i Wizz Air), wynik: najtańsze kombinacje
oraz najtańszy wyjazd w każdym tygodniu.
"""
from __future__ import annotations

import datetime as dt

from .fx import Fx
from .http import Http
from .providers import month_starts
from .providers.ryanair import Ryanair
from .providers.wizzair import WizzAir


def run(q: dict) -> list[str]:
    start, end = dt.date.fromisoformat(q["from"]), dt.date.fromisoformat(q["to"])
    end_ret = end + dt.timedelta(days=max(q.get("nights", [2, 4])))
    http = Http()
    fx = Fx(http)
    fr, wz = Ryanair(http, fx), WizzAir(http, fx)
    wz._init_api()
    fares = []
    for o, d in q["routes"]:
        for m in month_starts(start, end_ret):
            fares += fr.month_fares(o, d, m, start, end_ret)
            fares += fr.month_fares(d, o, m, start, end_ret)
        fares += wz.timetable(o, d, start, end_ret)
    home = set(q.get("home", ["WAW", "WMI"]))
    outs = [f for f in fares if f.origin in home and f.date <= end.isoformat()]
    rets = [f for f in fares if f.dest in home]
    n0, n1 = q.get("nights", [2, 4])
    combos = []
    for a in outs:
        for b in rets:
            if b.origin != a.dest and not q.get("any_return_airport"):
                continue
            n = (dt.date.fromisoformat(b.date) - dt.date.fromisoformat(a.arr_date or a.date)).days
            if n0 <= n <= n1:
                combos.append((round(a.price + b.price), n, a, b))
    combos.sort(key=lambda c: c[0])
    days = ["pn", "wt", "śr", "cz", "pt", "sb", "nd"]
    fmt = lambda f: (f"{f.carrier} {f.origin}→{f.dest} {days[dt.date.fromisoformat(f.date).weekday()]} {f.date} "
                     f"{f.time or '?'}→{f.arr_time or '?'} {round(f.price)} zł")
    lines = [f"Ceny: {len(fares)}, kombinacji: {len(combos)} (HTTP: {http.summary()})", "NAJTAŃSZE:"]
    lines += [f"{c[0]} zł | {c[1]} n. | {fmt(c[2])} | {fmt(c[3])}" for c in combos[: q.get("limit", 25)]]
    lines.append("NAJTAŃSZY WYJAZD W KAŻDYM TYGODNIU (wg daty wylotu):")
    best = {}
    for c in combos:
        wk = dt.date.fromisoformat(c[2].date).isocalendar()[1]
        if wk not in best:
            best[wk] = c
    for wk in sorted(best):
        c = best[wk]
        lines.append(f"tydz. {wk}: {c[0]} zł | {c[1]} n. | {fmt(c[2])} | {fmt(c[3])}")
    if q.get("by_carrier"):
        lines += _by_carrier(fares, home, combos, fmt)
    return lines


def _by_carrier(fares, home, combos, fmt) -> list[str]:
    """Porównanie przewoźników: ceny w jedną stronę per kierunek i miesiąc + najtańszy powrót tą samą linią."""
    import statistics
    out = ["PORÓWNANIE PRZEWOŹNIKÓW (w jedną stronę, zł):"]
    groups = {}
    for f in fares:
        kier = "tam" if f.origin in home else "powrót"
        groups.setdefault((f.carrier, f"{f.origin}-{f.dest}", f.date[:7]), []).append(f.price)
    for (c, r, m), ps in sorted(groups.items()):
        out.append(f"{c} {r} {m}: dni {len(ps)}, min {round(min(ps))}, mediana {round(statistics.median(ps))}")
    out.append("NAJTAŃSZE TAM-I-Z-POWROTEM TĄ SAMĄ LINIĄ:")
    for c in sorted({f.carrier for f in fares}):
        same = [x for x in combos if x[2].carrier == c and x[3].carrier == c]
        if same:
            x = same[0]
            out.append(f"{c}: {x[0]} zł | {x[1]} n. | {fmt(x[2])} | {fmt(x[3])}")
            meds = statistics.median([x[0] for x in same])
            out.append(f"{c}: mediana kombinacji {round(meds)} zł z {len(same)}")
    return out
