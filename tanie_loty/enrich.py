"""Dodatkowe informacje o kierunkach: centrum miasta, klimat, prognoza, poziom cen, historia cen.

Źródła (darmowe, bez kluczy):
- Open-Meteo: geokodowanie, archiwum pogody (średnie z 3 lat), prognoza 16-dniowa,
- Eurostat: wskaźnik poziomu cen „restauracje i hotele” (UE27 = 100).
Wyniki wolno zmieniających się danych są trzymane w katalogu cache/ (actions/cache).
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
import statistics
import time
from collections import defaultdict
from pathlib import Path

from .http import Http
from .models import Airport, Fare, distance_km

log = logging.getLogger(__name__)
TAG = "open-meteo"
BATCH = 25  # lokalizacji w jednym zapytaniu do Open-Meteo

# Przybliżone wskaźniki poziomu cen restauracji i hoteli (UE27 = 100) — zapas, gdy Eurostat
# nie odpowie lub nie ma danego kraju.
PLI_FALLBACK = {
    "AT": 117, "BE": 112, "BG": 52, "CH": 160, "CY": 97, "CZ": 72, "DE": 108, "DK": 140, "EE": 95,
    "ES": 90, "FI": 128, "FR": 112, "GB": 125, "GR": 88, "HR": 90, "HU": 66, "IE": 132, "IS": 165,
    "IT": 102, "LT": 76, "LU": 117, "LV": 80, "MT": 94, "NL": 116, "NO": 150, "PL": 62, "PT": 86,
    "RO": 57, "SE": 125, "SI": 90, "SK": 76, "AL": 50, "ME": 65, "MK": 48, "RS": 56, "BA": 52,
    "TR": 45, "MA": 45, "EG": 30, "IL": 140, "GE": 45, "JO": 70, "AE": 110, "AM": 50, "AZ": 45,
    "MD": 45, "UA": 40, "KZ": 45, "SA": 95, "UZ": 35, "TN": 40, "XK": 45,
}


class Cache:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def section(self, name: str) -> dict:
        return self.data.setdefault(name, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _om_get(http: Http, url: str, params: dict, timeout: float = 60):
    """Zapytanie do Open-Meteo; przy przekroczeniu limitu na minutę odczekuje i ponawia raz."""
    data = http.json("GET", url, TAG, params=params, timeout=timeout)
    if data is None and http.stats.get((TAG, 429)):
        time.sleep(61)
        data = http.json("GET", url, TAG, params=params, timeout=timeout)
    return data


def _as_list(data):
    return data if isinstance(data, list) else [data]


# --- centrum miasta ------------------------------------------------------------------
def city_centres(http: Http, airports: dict[str, Airport], codes: list[str], cache: Cache) -> dict:
    """{kod lotniska: {"lat", "lon", "name", "km"}} — najbliższe dużemu miastu centrum."""
    sec = cache.section("centres")
    for code in codes:
        a = airports.get(code)
        if not a or a.lat is None or code in sec:
            continue
        best = None
        names = [a.city, a.city.split(" ")[0], a.name]
        for name in dict.fromkeys(n for n in names if n):
            data = http.json("GET", "https://geocoding-api.open-meteo.com/v1/search", TAG,
                             params={"name": name, "count": 10, "language": "pl", "format": "json"}) or {}
            for r in data.get("results") or []:
                if a.cc and r.get("country_code", "").upper() != a.cc:
                    continue
                km = distance_km(a, Airport("", lat=r["latitude"], lon=r["longitude"]))
                if km is None or km > 80:
                    continue
                score = (r.get("population") or 0) / (1 + km)
                if best is None or score > best[0]:
                    best = (score, {"lat": r["latitude"], "lon": r["longitude"],
                                    "name": r.get("name", name), "km": round(km, 1)})
            if best:
                break
        sec[code] = best[1] if best else None
    return {c: sec.get(c) for c in codes}


# --- klimat (średnie z 3 ostatnich lat) -------------------------------------------------
def climate(http: Http, points: dict[str, tuple[float, float]], months: list[dt.date], cache: Cache) -> dict:
    """{kod: {"MM": {"tmax", "tmin", "rain", "sun"}}}; rain = dni z opadem ≥1 mm w miesiącu,
    sun = średnio godzin słońca dziennie."""
    sec = cache.section("climate")
    need = sorted({f"{m.month:02d}" for m in months})
    todo = [c for c in points if any(mm not in (sec.get(c) or {}) for mm in need)]
    if todo:
        this_year = dt.date.today().year
        start = min(months)
        last = max(months)
        end = (last.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        acc: dict[str, dict[str, dict[str, list]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        for back in (1, 2, 3):
            s = start.replace(year=start.year - back)
            ey = end.year - back
            e = dt.date(ey, end.month, min(end.day, calendar.monthrange(ey, end.month)[1]))
            if e.year >= this_year:
                e = min(e, dt.date.today() - dt.timedelta(days=7))
            for chunk in _chunks(todo, BATCH):
                data = _om_get(http, "https://archive-api.open-meteo.com/v1/archive", params={
                    "latitude": ",".join(str(points[c][0]) for c in chunk),
                    "longitude": ",".join(str(points[c][1]) for c in chunk),
                    "start_date": s.isoformat(), "end_date": e.isoformat(),
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,sunshine_duration",
                    "timezone": "auto",
                })
                if not data:
                    continue
                for code, loc in zip(chunk, _as_list(data)):
                    d = loc.get("daily") or {}
                    for i, day in enumerate(d.get("time") or []):
                        mm = day[5:7]
                        if mm not in need:
                            continue
                        for key, src in (("tmax", "temperature_2m_max"), ("tmin", "temperature_2m_min"),
                                         ("rain", "precipitation_sum"), ("sun", "sunshine_duration")):
                            v = (d.get(src) or [None] * (i + 1))[i]
                            if v is not None:
                                acc[code][mm][key].append(v)
        for code, by_m in acc.items():
            out = sec.setdefault(code, {})
            for mm, vals in by_m.items():
                if not vals["tmax"]:
                    continue
                n_days = len(vals["rain"]) or 1
                out[mm] = {
                    "tmax": round(statistics.mean(vals["tmax"]), 1),
                    "tmin": round(statistics.mean(vals["tmin"]), 1),
                    "rain": round(sum(1 for r in vals["rain"] if r >= 1.0) / n_days * 30.4, 1),
                    "sun": round(statistics.mean(vals["sun"]) / 3600, 1) if vals["sun"] else None,
                }
    return {c: {mm: v for mm, v in (sec.get(c) or {}).items() if mm in need} for c in points}


# --- prognoza (16 dni) -----------------------------------------------------------------
def forecast(http: Http, points: dict[str, tuple[float, float]]) -> dict:
    """{kod: {"YYYY-MM-DD": {"tmax", "tmin", "pp", "code"}}}"""
    out: dict[str, dict] = {}
    codes = list(points)
    for chunk in _chunks(codes, BATCH):
        data = _om_get(http, "https://api.open-meteo.com/v1/forecast", params={
            "latitude": ",".join(str(points[c][0]) for c in chunk),
            "longitude": ",".join(str(points[c][1]) for c in chunk),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "forecast_days": 16, "timezone": "auto",
        })
        if not data:
            continue
        for code, loc in zip(chunk, _as_list(data)):
            d = loc.get("daily") or {}
            days = {}
            for i, day in enumerate(d.get("time") or []):
                g = lambda k: (d.get(k) or [None] * (i + 1))[i]
                if g("temperature_2m_max") is None:
                    continue
                days[day] = {"tmax": round(g("temperature_2m_max")), "tmin": round(g("temperature_2m_min")),
                             "pp": g("precipitation_probability_max"), "code": g("weather_code")}
            out[code] = days
    return out


# --- poziom cen (Eurostat) ---------------------------------------------------------------
def _jsonstat_values(data: dict, dim: str) -> dict[str, float]:
    """Dekoduje JSON-stat: zwraca najnowszą dostępną wartość dla każdej kategorii wymiaru `dim`."""
    ids, sizes = data["id"], data["size"]
    cats = {d: data["dimension"][d]["category"]["index"] for d in ids}
    inv = {d: {i: k for k, i in (cats[d].items() if isinstance(cats[d], dict) else enumerate(cats[d]))}
           for d in ids}
    out: dict[str, tuple[str, float]] = {}
    for flat, val in (data.get("value") or {}).items():
        flat = int(flat)
        idx = {}
        for d, n in zip(reversed(ids), reversed(sizes)):
            idx[d] = inv[d][flat % n]
            flat //= n
        key, t = idx[dim], idx.get("time", "")
        if val is not None and (key not in out or t > out[key][0]):
            out[key] = (t, float(val))
    return {k: v for k, (_, v) in out.items()}


def price_levels(http: Http, cache: Cache) -> tuple[dict[str, float], str]:
    sec = cache.section("pli")
    fresh = sec.get("fetched", "") >= (dt.date.today() - dt.timedelta(days=30)).isoformat()
    if not fresh:
        data = http.json("GET", "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_ppp_ind",
                         "eurostat", params={"na_item": "PLI_EU27_2020", "ppp_cat": "A0111",
                                             "lastTimePeriod": 3, "format": "JSON", "lang": "EN"}, timeout=60)
        try:
            vals = _jsonstat_values(data, "geo") if data else {}
        except (KeyError, TypeError, ValueError) as e:
            log.warning("Eurostat: nie udało się odczytać danych: %s", e)
            vals = {}
        vals = {("GR" if k == "EL" else "GB" if k == "UK" else k): v for k, v in vals.items() if len(k) == 2}
        if vals:
            sec.clear()
            sec.update({"fetched": dt.date.today().isoformat(), "values": vals})
    vals = sec.get("values") or {}
    merged = {**PLI_FALLBACK, **vals}
    return merged, (f"Eurostat ({len(vals)} krajów) + tabela zapasowa" if vals else "tabela zapasowa")


# --- historia cen ------------------------------------------------------------------------
def update_history(http: Http, url: str | None, fares: list[Fare], group: str = "WAW") -> tuple[dict, dict]:
    """Pobiera poprzednią historię z opublikowanej strony, dopisuje bieżące ceny.
    Zwraca (historia do zapisania, {klucz lotu: info dla strony})."""
    hist = {"runs": [], "fares": {}}
    if url:
        for name in (f"history-{group}.json",) + (("history.json",) if group == "WAW" else ()):
            prev = http.json("GET", url.rstrip("/") + "/" + name, "history", timeout=30)
            if isinstance(prev, dict) and "fares" in prev:
                hist = prev
                break
    today = dt.date.today().isoformat()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    hist["runs"] = (hist.get("runs") or [])[-200:] + [now]
    H = {k: v for k, v in hist["fares"].items() if k[-10:] >= today}  # usuń loty z przeszłości
    info = {}
    for f in fares:
        k = f"{f.carrier[:2]}-{f.origin}-{f.dest}-{f.date}"
        p = round(f.price)
        # [pierwsze_widzenie, min, max, suma, liczba, ostatnia, poprzednia]
        h = H.get(k)
        if h is None:
            h = [now[:10], p, p, 0, 0, p, None]
        else:
            h[6] = h[5]
            h[1], h[2] = min(h[1], p), max(h[2], p)
        h[3] += p
        h[4] += 1
        h[5] = p
        H[k] = h
        i = {"n": h[4], "since": h[0]}
        if h[4] >= 2:
            i["avg"] = round(h[3] / h[4])
            i["min"] = h[1]
            if h[6] is not None and h[6] != p:
                i["chg"] = p - h[6]
        info[k] = i
    hist["fares"] = H
    return hist, info


def fare_key(leg: dict) -> str:
    return f"{leg['carrier'][:2]}-{leg['from']}-{leg['to']}-{leg['date']}"


def destination_medians(trips: dict[str, list[dict]]) -> dict[str, dict[str, int]]:
    """Mediana ceny (za osobę) dla kierunku — punkt odniesienia „poniżej/powyżej średniej”."""
    out: dict[str, dict[str, int]] = {}
    for kind in ("rt", "oj", "ow"):
        by: dict[str, list[int]] = defaultdict(list)
        for t in trips[kind]:
            by[t["dest"]].append(t["total"])
        out[kind] = {d: round(statistics.median(v)) for d, v in by.items() if len(v) >= 5}
    return out
