"""Wizz Air — publiczne (nieoficjalne) endpointy używane przez wizzair.com."""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from ..fx import Fx
from ..http import Http
from ..models import Airport, Fare
from . import ProviderResult, in_window

log = logging.getLogger(__name__)

NAME = "Wizz Air"
TAG = "wizzair"
CHUNK_DAYS = 42
DEFAULT_VERSION = "29.14.0"


def booking_url(o: str, d: str, date: str) -> str:
    return f"https://wizzair.com/pl-pl/booking/select-flight/{o}/{d}/{date}/null/1/0/0/null"


class WizzAir:
    def __init__(self, http: Http, fx: Fx, workers: int = 1):
        # Wizz Air szybko ogranicza liczbę zapytań: osobny, wolniejszy klient bez automatycznych ponowień.
        self.http = Http(min_interval=1.5, timeout=20, retries=0)
        self.shared = http
        self.fx, self.workers = fx, workers
        self.api: str | None = None
        self.headers = {
            "Content-Type": "application/json",
            "Origin": "https://www.wizzair.com",
            "Referer": "https://www.wizzair.com/pl-pl",
        }
        self.version_source = ""
        self.ok = 0
        self.failed = 0
        self.consecutive = 0
        self.deadline = time.monotonic() + 15 * 60   # maks. 15 minut na Wizz Air
        self.stopped = ""

    def _init_api(self) -> bool:
        # Strona główna osadza adres API: apiUrl:"https://be.wizzair.com/29.14.0/Api"
        version = None
        for url in ("https://www.wizzair.com/pl-pl", "https://www.wizzair.com/en-gb"):
            r = self.http.request("GET", url, TAG, headers={"Accept": "text/html"})
            m = re.search(r"be\.wizzair\.com/(\d+\.\d+\.\d+)/Api", r.text if r is not None else "")
            if m:
                version = m.group(1)
                break
        self.api = f"https://be.wizzair.com/{version or DEFAULT_VERSION}/Api"
        self.version_source = "strona" if version else f"domyślna {DEFAULT_VERSION}"
        # Nowa sesja ustawia ciasteczko z tokenem weryfikacyjnym.
        self.http.request("GET", f"{self.api}/userSession/new", TAG, headers=self.headers)
        return True

    def _hdrs(self) -> dict:
        h = dict(self.headers)
        tok = self.http.session.cookies.get("RequestVerificationToken")
        if tok:  # token rotuje — czytamy go świeżo przy każdym zapytaniu
            h["X-RequestVerificationToken"] = tok
        return h

    def network(self) -> tuple[dict[str, Airport], dict[str, set[str]]]:
        data = self.http.json("GET", f"{self.api}/asset/map", TAG,
                              params={"languageCode": "pl-pl"}, headers=self._hdrs()) or {}
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
            if f.get("priceType") in ("soldOut", "checkPrice") or not amount:
                continue
            day = (f.get("departureDate") or "")[:10]
            if not in_window(day, start, end):
                continue
            cur = price.get("currencyCode", "PLN")
            pln = self.fx.to_pln(float(amount), cur)
            if pln is None:
                continue
            times = sorted((x.get("date") if isinstance(x, dict) else x) or ""
                           for x in f.get("departureDates") or [])
            times = [x for x in times if x]
            t = times[0][11:16] if times else None
            o, d = f.get("departureStation"), f.get("arrivalStation")
            out.append(Fare(NAME, o, d, day, t, pln, float(amount), cur, booking_url(o, d, day)))
        return out

    def timetable(self, o: str, d: str, start: dt.date, end: dt.date) -> list[Fare]:
        fares: list[Fare] = []
        cur = start
        while cur <= end:
            # bezpieczniki: seria odmów albo przekroczony czas — nie męczymy dalej API
            if self.consecutive >= 4:
                self.stopped = self.stopped or "przerwano po serii odmów (limit zapytań)"
                return fares
            if time.monotonic() > self.deadline:
                self.stopped = self.stopped or "przerwano po 15 min (limit czasu)"
                return fares
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
            data = None
            for attempt in range(3):
                data = self.http.json("POST", f"{self.api}/search/timetableV2", TAG,
                                      json=body, headers=self._hdrs())
                if data is not None:
                    break
                # Prawdopodobnie limit zapytań: odczekaj i odnów sesję.
                time.sleep(20 * (attempt + 1))
                self.http.request("GET", f"{self.api}/userSession/new", TAG, headers=self.headers)
            if data is None:
                self.failed += 1
                self.consecutive += 1
            else:
                self.ok += 1
                self.consecutive = 0
                fares += self._parse(data.get("outboundFlights"), start, end)
                fares += self._parse(data.get("returnFlights"), start, end)
            cur = stop + dt.timedelta(days=1)
        return fares

    def fetch(self, origins: list[str], start: dt.date, end: dt.date) -> ProviderResult:
        res = ProviderResult(NAME)
        self._init_api()
        res.notes.append(f"wersja API: {self.version_source}")
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
        # statystyki HTTP Wizz Air do wspólnego podsumowania
        self.shared.stats.update(self.http.stats)
        for k, v in self.http.samples.items():
            self.shared.samples.setdefault(k, v)
        if self.stopped:
            res.notes.append(self.stopped)
        if self.failed and not self.ok:
            res.notes.append("wszystkie zapytania o ceny odrzucone (blokada?)")
        elif self.failed:
            res.notes.append(f"{self.failed} z {self.ok + self.failed} zapytań o ceny nieudanych")
        return res
