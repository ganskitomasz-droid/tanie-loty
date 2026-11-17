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

    providers = [{"name": r.name, "routes": r.routes, "fares": len(r.fares), "notes": r.notes}
                 for r in results]
    meta = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window": [start.isoformat(), end.isoformat()],
        "config": cfg, "fx": fx_source, "providers": providers,
        "http": http.summary(), "seconds": round(time.time() - t0),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, **trips,
               "airports": {c: vars(airports[c]) for c in sorted(used) if c in airports}}
    (out / "results.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                                      encoding="utf-8")
    shutil.copy(ROOT / "site" / "index.html", out / "index.html")

    summary = "\n".join(
        [f"{pr['name']}: {pr['routes']} tras, {pr['fares']} cen" +
         (f" — {'; '.join(pr['notes'])}" if pr["notes"] else "") for pr in providers]
        + [f"Kombinacje: {len(trips['rt'])} w obie strony, {len(trips['oj'])} open-jaw, "
           f"{len(trips['ow'])} w jedną stronę", f"HTTP: {json.dumps(meta['http'])}"]
        + [f"Pierwszy błąd {k}: {v}" for k, v in http.samples.items()]
        + [
           f"Kursy: {fx_source}, czas: {meta['seconds']} s"])
    log.info("\n%s", summary)
    gh_annotation("notice" if fares else "warning", "Wynik wyszukiwania", summary)
    return 0 if fares or args.demo else 1


if __name__ == "__main__":
    sys.exit(main())
