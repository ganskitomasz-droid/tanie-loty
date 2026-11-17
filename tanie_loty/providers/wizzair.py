"""Wizz Air — publiczne (nieoficjalne) endpointy używane przez wizzair.com."""
from __future__ import annotations

import datetime as dt
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from ..fx import Fx
from ..http import Http
from ..models import Airport, Fare
from . import ProviderResult, in_window

log = logging.getLogger(__name__)

NAME = "Wizz Air"
TAG = "wizzair"
CHUNK_DAYS = 30


def booking_url(o: str, d: str, date: str) -> str:
    return f"https://wizzair.com/pl-pl/booking/select-flight/{o}/{d}/{date}/null/1/0/0/null"


class WizzAir:
    def __init__(self, http: Http, fx: Fx, workers: int = 3):
        self.http, self.fx, self.workers = http, fx, workers
        self.api: str | None = None
        self.headers = {
            "Content-Type": "application/json",
            "Origin": "https://wizzair.com",
            "Referer": "https://wizzair.com/pl-pl",
        }

    def _init_api(self) -> bool:
        r = self.http.request("GET", "https://wizzair.com/buildnumber", TAG)
        m = re.search(r"(\d+\.\d+\.\d+)", r.text if r is not None and r.ok else "")
        if not m:
            return False
        self.api = f"https://be.wizzair.com/{m.group(1)}/Api"
        # Ciasteczka sesji (część endpointów wymaga tokenu z ciasteczka).
        self.http.request("GET", "https://wizzair.com/pl-pl", TAG, headers={"Accept": "text/html"})
        tok = self.http.session.cookies.get("RequestVerificationToken")
        if tok:
            self.headers["X-RequestVerificationToken"] = tok
        return True

    def network(self) -> tuple[dict[str, Airport], dict[str, set[str]]]:
        data = self.http.json("GET", f"{self.api}/asset/map", TAG,
                              params={"languageCode": "pl-pl"}, headers=self.headers) or {}
        airports: dict[str, Airport] = {}
        conns: dict[str, set[str]] = {}
        for c in data.get("cities", []):
            if c.get("isFakeStation"):
                continue
            code = c.get("iata")
            if not code:
                continue
            airports[code] = Airport(code, c.get("shortName", ""), c.get("shortName", ""),
                                     c.get("countryName", ""), c.get("latitude"), c.get("longitude"))
            conns[code] = {x["iata"] for x in c.get("connections", []) if x.get("iata")}
        return airports, conns

    def _parse(self, flights: list[dict], start: dt.date, end: dt.date) -> list[Fare]:
        out: list[Fare] = []
        for f in flights or []:
            price = f.get("price") or {}
            amount = price.get("amount")
            if f.get("priceType") != "price" or not amount:
                continue
            day = (f.get("departureDate") or "")[:10]
            if not in_window(day, start, end):
                continue
            cur = price.get("currencyCode", "PLN")
            pln = self.fx.to_pln(float(amount), cur)
            if pln is None:
                continue
            times = sorted(f.get("departureDates") or [])
            t = times[0][11:16] if times else None
            o, d = f.get("departureStation"), f.get("arrivalStation")
            out.append(Fare(NAME, o, d, day, t, pln, float(amount), cur, booking_url(o, d, day)))
        return out

    def timetable(self, o: str, d: str, start: dt.date, end: dt.date) -> list[Fare]:
        fares: list[Fare] = []
        cur = start
        while cur <= end:
            stop = min(end, cur + dt.timedelta(days=CHUNK_DAYS - 1))
            body = {
                "flightList": [
                    {"departureStation": o, "arrivalStation": d,
                     "from": cur.isoformat(), "to": stop.isoformat()},
                    {"departureStation": d, "arrivalStation": o,
                     "from": cur.isoformat(), "to": stop.isoformat()},
                ],
                "priceType": "regular", "adultCount": 1, "childCount": 0, "infantCount": 0,
            }
            data = self.http.json("POST", f"{self.api}/search/timetable", TAG,
                                  json=body, headers=self.headers)
            if data:
                fares += self._parse(data.get("outboundFlights"), start, end)
                fares += self._parse(data.get("returnFlights"), start, end)
            cur = stop + dt.timedelta(days=1)
        return fares

    def fetch(self, origins: list[str], start: dt.date, end: dt.date) -> ProviderResult:
        res = ProviderResult(NAME)
        if not self._init_api():
            res.notes.append("nie udało się ustalić wersji API Wizz Air (blokada?)")
            return res
        res.airports, conns = self.network()
        if not conns:
            res.notes.append("brak siatki połączeń Wizz Air (blokada?)")
            return res
        pairs = [(o, d) for o in origins for d in sorted(conns.get(o, ()))]
        res.routes = len(pairs)
        log.info("Wizz Air: %d tras", len(pairs))
        with ThreadPoolExecutor(self.workers) as ex:
            for fares in ex.map(lambda p: self.timetable(*p, start, end), pairs):
                res.fares.extend(fares)
        return res
