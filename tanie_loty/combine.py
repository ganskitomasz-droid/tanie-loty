"""Składanie pojedynczych lotów w podróże: w obie strony, open-jaw, w jedną stronę."""
from __future__ import annotations

import datetime as dt
import heapq
from collections import defaultdict

from .models import Airport, Fare, distance_km


def _best_by_day(fares: list[Fare], key) -> dict[tuple[str, str], Fare]:
    best: dict[tuple[str, str], Fare] = {}
    for f in fares:
        k = (key(f), f.date)
        if k not in best or f.price < best[k].price:
            best[k] = f
    return best


def _leg(f: Fare) -> dict:
    # Link do rezerwacji składa strona (oszczędza miejsce w results.json).
    d = {"carrier": f.carrier, "from": f.origin, "to": f.dest, "date": f.date,
         "time": f.time, "price": round(f.price)}
    if f.currency != "PLN":
        d["orig"] = f"{f.price_orig:g} {f.currency}"
    return d


def _best_per_day(cands: list[tuple], k: int) -> list[tuple]:
    """Zostawia k najtańszych kandydatów dla każdej daty wylotu (c[1])."""
    by_day: dict[str, list[tuple]] = defaultdict(list)
    for c in cands:
        by_day[c[1]].append(c)
    out: list[tuple] = []
    for lst in by_day.values():
        out += heapq.nsmallest(k, lst, key=lambda c: c[0])
    return out


def combine(
    fares: list[Fare],
    airports: dict[str, Airport],
    origins: list[str],
    nights_min: int = 2,
    nights_max: int = 5,
    openjaw_km: int = 350,
    max_total: float | None = None,
    per_day: int = 2,
    cap: int = 20000,
) -> dict[str, list[dict]]:
    O = set(origins)
    out_f = [f for f in fares if f.origin in O and f.dest not in O]
    in_f = [f for f in fares if f.dest in O and f.origin not in O]
    best_out = _best_by_day(out_f, lambda f: f.dest)    # (X, data) -> najtańszy wylot
    best_in = _best_by_day(in_f, lambda f: f.origin)    # (X, data) -> najtańszy powrót

    out_by_dest: dict[str, list[Fare]] = defaultdict(list)
    for (x, _), f in best_out.items():
        out_by_dest[x].append(f)
    in_dests = {x for (x, _) in best_in}

    def returns(x: str, d: str):
        day = dt.date.fromisoformat(d)
        for n in range(nights_min, nights_max + 1):
            r = best_in.get((x, (day + dt.timedelta(days=n)).isoformat()))
            if r:
                yield n, r

    def ok(total: float) -> bool:
        return max_total is None or max_total <= 0 or total <= max_total

    # --- w obie strony ---------------------------------------------------
    rt: list[dict] = []
    for x, outs in out_by_dest.items():
        cands = []
        for f in outs:
            for n, r in returns(x, f.date):
                total = f.price + r.price
                if ok(total):
                    cands.append((total, f.date, n, f, r))
        for total, _, n, f, r in _best_per_day(cands, per_day):
            rt.append({"type": "rt", "dest": x, "back": x, "nights": n, "total": round(total),
                       "out": _leg(f), "ret": _leg(r)})

    # --- open-jaw: przylot do X, powrót z pobliskiego Y -------------------
    near: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for x in out_by_dest:
        for y in in_dests:
            if x == y or x not in airports or y not in airports:
                continue
            km = distance_km(airports[x], airports[y])
            if km is not None and km <= openjaw_km:
                near[x].append((y, km))
    oj: list[dict] = []
    for x, ys in near.items():
        cands = []
        for y, km in ys:
            for f in out_by_dest[x]:
                for n, r in returns(y, f.date):
                    total = f.price + r.price
                    if ok(total):
                        cands.append((total, f.date, n, f, r, y, km))
        for total, _, n, f, r, y, km in _best_per_day(cands, per_day):
            oj.append({"type": "oj", "dest": x, "back": y, "km": round(km), "nights": n,
                       "total": round(total), "out": _leg(f), "ret": _leg(r)})

    # --- w jedną stronę --------------------------------------------------
    ow: list[dict] = []
    for x, outs in out_by_dest.items():
        for f in outs:  # out_by_dest ma już najtańszy lot na każdy dzień
            if ok(f.price):
                ow.append({"type": "ow", "dest": x, "total": round(f.price), "out": _leg(f)})

    key = lambda r: (r["total"], r["out"]["date"])
    return {"rt": sorted(rt, key=key)[:cap], "oj": sorted(oj, key=key)[:cap],
            "ow": sorted(ow, key=key)[:cap]}
