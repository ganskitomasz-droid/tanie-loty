"""Uruchomienie: python -m tanie_loty [opcje]  (szczegóły: --help)"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import shutil
import sys
import time
from pathlib import Path

from . import enrich, hotels as hotels_mod
from .xotelo import XoteloHotels
from .combine import combine
from .fx import Fx
from .http import Http
from .models import Airport, Fare
from .providers import ProviderResult
from .providers.ryanair import Ryanair
from .providers.wizzair import WizzAir

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("tanie_loty")


def gh_annotation(level: str, title: str, msg: str) -> None:
    """Adnotacja w GitHub Actions (widoczna w podsumowaniu uruchomienia)."""
    if os.environ.get("GITHUB_ACTIONS"):
        msg = msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::{level} title={title}::{msg}", flush=True)


def load_config(args) -> dict:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    for key in ("origins", "months", "nights_min", "nights_max", "openjaw_km", "max_total", "providers"):
        val = getattr(args, key)
        if val not in (None, ""):
            cfg[key] = val
    for key in ("origins", "providers"):
        if isinstance(cfg[key], str):
            cfg[key] = [x.strip() for x in cfg[key].replace(";", ",").split(",") if x.strip()]
    cfg["origins"] = [o.upper() for o in cfg["origins"]]
    for key in ("months", "nights_min", "nights_max", "openjaw_km"):
        cfg[key] = int(cfg[key])
    cfg["max_total"] = float(cfg.get("max_total") or 0)
    return cfg


def demo_data(origins: list[str], start: dt.date, end: dt.date) -> list[ProviderResult]:
    """Losowe dane do testowania strony bez dostępu do sieci."""
    rnd = random.Random(42)
    ap = {
        "WAW": Airport("WAW", "Chopina", "Warszawa", "Polska", 52.17, 20.97),
        "WMI": Airport("WMI", "Modlin", "Warszawa", "Polska", 52.45, 20.65),
        "BCN": Airport("BCN", "El Prat", "Barcelona", "Hiszpania", 41.30, 2.08),
        "GRO": Airport("GRO", "Girona", "Barcelona", "Hiszpania", 41.90, 2.76),
        "REU": Airport("REU", "Reus", "Barcelona", "Hiszpania", 41.15, 1.17),
        "BGY": Airport("BGY", "Bergamo", "Mediolan", "Włochy", 45.67, 9.70),
        "MXP": Airport("MXP", "Malpensa", "Mediolan", "Włochy", 45.63, 8.72),
        "VCE": Airport("VCE", "Marco Polo", "Wenecja", "Włochy", 45.50, 12.35),
        "LIS": Airport("LIS", "Lizbona", "Lizbona", "Portugalia", 38.77, -9.13),
        "OPO": Airport("OPO", "Porto", "Porto", "Portugalia", 41.24, -8.68),
        "TFS": Airport("TFS", "Teneryfa Płd.", "Teneryfa", "Hiszpania", 28.04, -16.57),
        "ATH": Airport("ATH", "Ateny", "Ateny", "Grecja", 37.94, 23.94),
    }
    ccs = {"Polska": "PL", "Hiszpania": "ES", "Włochy": "IT", "Portugalia": "PT", "Grecja": "GR"}
    for a in ap.values():
        a.cc = ccs.get(a.country, "")
    res = []
    for carrier in ("Ryanair", "Wizz Air"):
        r = ProviderResult(carrier, airports=ap, routes=8)
        for o in origins:
            for d in ap:
                if d in ("WAW", "WMI") or rnd.random() < 0.3:
                    continue
                day = start
                while day <= end:
                    if rnd.random() < 0.5:
                        for a, b in ((o, d), (d, o)):
                            p = rnd.choice([39, 59, 79, 99, 129, 149, 199, 249, 349])
                            h = rnd.randint(6, 21)
                            arr_h = h + rnd.randint(2, 4)
                            arr_day = day + dt.timedelta(days=arr_h // 24)
                            r.fares.append(Fare(carrier, a, b, day.isoformat(), f"{h:02d}:05",
                                                p, p, "PLN", "https://example.com",
                                                arr_day.isoformat(), f"{arr_h % 24:02d}:40"))
                    day += dt.timedelta(days=1)
        res.append(r)
    return res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Wyszukiwarka tanich lotów (Ryanair + Wizz Air)")
    p.add_argument("--origins", help="lotniska startowe, np. WAW,WMI")
    p.add_argument("--months", help="ile miesięcy do przodu przeszukać")
    p.add_argument("--nights-min", dest="nights_min")
    p.add_argument("--nights-max", dest="nights_max")
    p.add_argument("--openjaw-km", dest="openjaw_km", help="maks. odległość między lotniskami open-jaw")
    p.add_argument("--max-total", dest="max_total", help="maks. cena całej podróży w PLN (0 = bez limitu)")
    p.add_argument("--providers", help="ryanair,wizzair")
    p.add_argument("--out", default="public", help="katalog wynikowy")
    p.add_argument("--demo", action="store_true", help="dane losowe, bez sieci")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = load_config(args)
    start = dt.date.today() + dt.timedelta(days=1)
    end = start + dt.timedelta(days=30 * cfg["months"])
    log.info("Szukam z %s, %s – %s, %d–%d nocy", cfg["origins"], start, end,
             cfg["nights_min"], cfg["nights_max"])
    t0 = time.time()

    http = Http()
    results: list[ProviderResult] = []
    if args.demo:
        results = demo_data(cfg["origins"], start, end)
        fx_source = "demo"
    else:
        fx = Fx(http)
        fx_source = fx.source
        classes = {"ryanair": Ryanair, "wizzair": WizzAir}
        for name in cfg["providers"]:
            cls = classes.get(name.lower())
            if not cls:
                log.warning("Nieznany przewoźnik: %s", name)
                continue
            try:
                results.append(cls(http, fx).fetch(cfg["origins"], start, end))
            except Exception as e:  # jeden przewoźnik nie może wywrócić całości
                log.exception("Błąd przewoźnika %s", name)
                results.append(ProviderResult(name, notes=[f"wyjątek: {e}"]))

    airports: dict[str, Airport] = {}
    fares: list[Fare] = []
    for r in results:
        for code, a in r.airports.items():
            airports.setdefault(code, Airport(code)).merge(a)
        fares.extend(r.fares)

    trips = combine(fares, airports, cfg["origins"], cfg["nights_min"], cfg["nights_max"],
                    cfg["openjaw_km"], cfg["max_total"])
    used = {c for lst in trips.values() for t in lst for c in (t["dest"], t.get("back"),
            t["out"]["from"], t.get("ret", {}).get("from"))} - {None}

    # --- wzbogacenie: centrum, pogoda, poziom cen, historia --------------------------------
    dests = sorted({t[k] for lst in trips.values() for t in lst for k in ("dest", "back") if t.get(k)})
    extra: dict[str, dict] = {c: {} for c in dests}
    pli, pli_src, hist = {}, "-", None
    hotel_offers: dict[str, dict] = {}
    centres: dict = {}
    months = sorted({dt.date.fromisoformat(m + "-01") for m in
                     {t["out"]["date"][:7] for lst in trips.values() for t in lst}})
    enrich_notes = []
    if args.demo:
        rnd = random.Random(1)
        for c in dests:
            extra[c]["centre"] = {"name": airports[c].city, "km": round(rnd.uniform(3, 45), 1)}
            extra[c]["climate"] = {f"{m.month:02d}": {"tmax": round(rnd.uniform(8, 26), 1),
                                   "tmin": round(rnd.uniform(0, 15), 1), "rain": round(rnd.uniform(1, 14), 1),
                                   "sun": round(rnd.uniform(2, 9), 1)} for m in months}
            extra[c]["forecast"] = {(start + dt.timedelta(days=i)).isoformat():
                                    {"tmax": rnd.randint(10, 25), "tmin": rnd.randint(2, 14),
                                     "pp": rnd.randint(0, 90), "code": rnd.choice([0, 1, 2, 3, 61, 80])}
                                    for i in range(16)}
        pli = dict(enrich.PLI_FALLBACK)
        pli_src = "demo"
        for i, (c, tg) in enumerate(hotels_mod.hotel_targets(trips).items()):
            if i % 2 == 0:
                n = (dt.date.fromisoformat(tg["checkout"]) - dt.date.fromisoformat(tg["checkin"])).days
                pn = rnd.randint(150, 320)
                hotel_offers[c] = {"name": f"Hotel Demo {c}", "rating": round(rnd.uniform(7, 9.4), 1),
                                   "reviews": rnd.randint(80, 2000), "stars": rnd.choice([2, 3, 3, 4]),
                                   "km": round(rnd.uniform(0.3, 2.9), 1), "total": pn * n, "per_night": pn,
                                   "room": "Pokój dwuosobowy z łazienką", "board": "Bez wyżywienia",
                                   "nights": n, "adults": 2, **tg}
        hinfo = {enrich.fare_key(t[k]): {"n": 5, "since": start.isoformat(), "avg": t[k]["price"] + rnd.randint(-30, 40),
                                         "min": t[k]["price"] - rnd.randint(0, 20), "chg": rnd.choice([-40, -10, 0, 15, 30])}
                 for lst in trips.values() for t in lst for k in ("out", "ret") if k in t}
    else:
        cache = enrich.Cache(ROOT / "cache" / "enrich.json")
        hinfo = {}
        try:
            centres = enrich.city_centres(http, airports, dests, cache)
            points = {}
            for c in dests:
                cen, a = centres.get(c), airports.get(c)
                if cen:
                    extra[c]["centre"] = {"name": cen["name"], "km": cen["km"]}
                    points[c] = (cen["lat"], cen["lon"])
                elif a and a.lat is not None:
                    points[c] = (a.lat, a.lon)
            for c, v in enrich.climate(http, points, months, cache).items():
                if v:
                    extra[c]["climate"] = v
            soon = (dt.date.today() + dt.timedelta(days=15)).isoformat()
            near = {t["dest"] for lst in trips.values() for t in lst if t["out"]["date"] <= soon}
            for c, v in enrich.forecast(http, {c: p for c, p in points.items() if c in near}).items():
                extra[c]["forecast"] = v
        except Exception as e:  # dodatki nie mogą wywrócić wyszukiwania
            log.exception("Błąd pogody/centrów")
            enrich_notes.append(f"pogoda/centra: {e}")
        try:
            pli, pli_src = enrich.price_levels(http, cache)
        except Exception as e:
            log.exception("Błąd Eurostat")
            pli, pli_src = dict(enrich.PLI_FALLBACK), f"tabela zapasowa ({e})"
        key = hotels_mod.get_key()
        if key:
            try:
                lh = hotels_mod.LiteApiHotels(http, fx, key, cfg.get("hotels", {}))
                hotel_offers = lh.fetch(hotels_mod.hotel_targets(trips), airports, centres, cache.data)
                if key.startswith("sand"):  # klucz testowy: dane przykładowe, nie prawdziwe ceny
                    for o in hotel_offers.values():
                        o["sandbox"] = True
                    enrich_notes.append("UWAGA: klucz testowy LiteAPI (sandbox) — ceny noclegów przykładowe")
                r = sorted(lh.ratings)
                enrich_notes.append(f"noclegi LiteAPI: {len(hotel_offers)} ofert; {lh.sample}; "
                                    f"oceny: {r[0] if r else '-'}–{r[-1] if r else '-'} (n={len(r)})")
            except Exception as e:
                log.exception("Błąd LiteAPI")
                enrich_notes.append(f"noclegi LiteAPI: błąd {e}")
        else:
            try:
                xh = XoteloHotels(Http(min_interval=1.2, timeout=40, retries=1), fx, cfg.get("hotels", {}))
                hotel_offers = xh.fetch(hotels_mod.hotel_targets(trips), airports, centres, cache.data)
                types = ", ".join(f"{k}={v}" for k, v in sorted(xh.types.items(), key=lambda x: -x[1])[:8])
                enrich_notes.append(f"noclegi Xotelo: {len(hotel_offers)} ofert; ceny: {xh.rates_ok} ok, "
                                    f"{xh.rates_empty} pustych; {xh.stat}; wikidata: {xh.wd_samples}; próbki cen: {xh.samples}; typy: {types}" +
                                    (f"; błędy: {'; '.join(xh.notes)}" if xh.notes else ""))
                for k, v in xh.http.stats.items():
                    http.stats[k] += v
                for k, v in xh.http.samples.items():
                    http.samples.setdefault(k, v)
            except Exception as e:
                log.exception("Błąd Xotelo")
                enrich_notes.append(f"noclegi Xotelo: błąd {e}")
        try:
            hist, hinfo = enrich.update_history(http, cfg.get("pages_url"), fares)
        except Exception as e:
            log.exception("Błąd historii cen")
            enrich_notes.append(f"historia: {e}")
        cache.save()

    medians = enrich.destination_medians(trips)
    for kind, lst in trips.items():
        for t in lst:
            med = medians[kind].get(t["dest"])
            if med:
                t["vs"] = round((t["total"] - med) / med * 100)
            for k in ("out", "ret"):
                h = hinfo.get(enrich.fare_key(t[k])) if k in t else None
                if h and h.get("n", 0) >= 2:
                    t[k]["h"] = h
    with_clim = sum(1 for c in dests if extra[c].get("climate"))
    with_cen = sum(1 for c in dests if extra[c].get("centre"))
    enrich_notes.append(f"kierunki: {len(dests)}, klimat: {with_clim}, centrum: {with_cen}, "
                        f"prognoza: {sum(1 for c in dests if extra[c].get('forecast'))}, poziom cen: {pli_src}, "
                        f"historia: {len(hist['runs']) if hist else 0} przebiegów")

    providers = [{"name": r.name, "routes": r.routes, "fares": len(r.fares), "notes": r.notes}
                 for r in results]
    meta = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window": [start.isoformat(), end.isoformat()],
        "config": cfg, "fx": fx_source, "providers": providers,
        "http": http.summary(), "seconds": round(time.time() - t0),
        "enrich": enrich_notes, "pli_source": pli_src, "medians_note": "mediana cen kierunku w przeszukanym okresie",
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    transit = json.loads((ROOT / "tanie_loty" / "transit.json").read_text(encoding="utf-8"))
    ap_out = {}
    for c in sorted(used):
        if c in airports:
            ap_out[c] = {**vars(airports[c]), **extra.get(c, {})}
            if c in transit:
                mode, eur_price, minutes, to = transit[c]
                ap_out[c]["transit"] = {"mode": mode, "eur": eur_price, "min": minutes, "to": to}
    used_cc = {a.get("cc") for a in ap_out.values() if a.get("cc")}
    eur = 4.25 if args.demo else (fx.rates.get("EUR") or 4.25)
    payload = {"meta": meta, **trips, "airports": ap_out,
               "pli": {cc: pli[cc] for cc in sorted(used_cc) if cc in pli},
               "cost": {k: v for k, v in cfg.get("cost", {}).items() if not k.startswith("_")}, "eur": eur,
               "hotels": hotel_offers, "hotel_criteria": {k: v for k, v in cfg.get("hotels", {}).items()
                                                          if not k.startswith("_")}}
    (out / "results.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                                      encoding="utf-8")
    shutil.copy(ROOT / "site" / "index.html", out / "index.html")
    if hist:
        (out / "history.json").write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")

    summary = "\n".join(
        [f"{pr['name']}: {pr['routes']} tras, {pr['fares']} cen" +
         (f" — {'; '.join(pr['notes'])}" if pr["notes"] else "") for pr in providers]
        + [f"Kombinacje: {len(trips['rt'])} w obie strony, {len(trips['oj'])} open-jaw, "
           f"{len(trips['ow'])} w jedną stronę", "Dodatki: " + "; ".join(enrich_notes),
           f"HTTP: {json.dumps(meta['http'])}"]
        + [f"Pierwszy błąd {k}: {v}" for k, v in http.samples.items()]
        + [
           f"Kursy: {fx_source}, czas: {meta['seconds']} s"])
    log.info("\n%s", summary)
    gh_annotation("notice" if fares else "warning", "Wynik wyszukiwania", summary)
    return 0 if fares or args.demo else 1


if __name__ == "__main__":
    sys.exit(main())
