"""Ryanair — publiczne (nieoficjalne) endpointy używane przez ryanair.com."""
from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor

from ..fx import Fx
from ..http import Http
from ..models import Airport, Fare
from . import ProviderResult, in_window, month_starts

log = logging.getLogger(__name__)

NAME = "Ryanair"
TAG = "ryanair"
WEB = "https://www.ryanair.com/api"
FARFND_BASES = [
    "https://services-api.ryanair.com/farfnd/v4",
    "https://www.ryanair.com/api/farfnd/v4",
    "https://services-api.ryanair.com/farfnd/3",
]


def booking_url(o: str, d: str, date: str) -> str:
    return (
        "https://www.ryanair.com/pl/pl/trip/flights/select?adults=1&teens=0&children=0&infants=0"
        f"&dateOut={date}&isConnectedFlight=false&isReturn=false&discount=0"
        f"&originIata={o}&destinationIata={d}"
    )


def _airport(a: dict) -> Airport:
    coords = a.get("coordinates") or {}
    return Airport(
        code=a.get("code") or a.get("iataCode") or "",
        name=a.get("name", ""),
        city=(a.get("city") or {}).get("name", ""),
        country=(a.get("country") or {}).get("name", "") or a.get("countryName", ""),
        lat=coords.get("latitude"),
        lon=coords.get("longitude"),
    )


class Ryanair:
    def __init__(self, http: Http, fx: Fx, workers: int = 4):
        self.http, self.fx, self.workers = http, fx, workers
        self._base: str | None = None

    # --- trasy -----------------------------------------------------------
    def airports(self) -> dict[str, Airport]:
        data = self.http.json("GET", f"{WEB}/views/locate/5/airports/pl/active", TAG) or []
        return {a.code: a for a in map(_airport, data) if a.code}

    def destinations(self, origin: str, start: dt.date, end: dt.date) -> dict[str, Airport]:
        data = self.http.json("GET", f"{WEB}/views/locate/searchWidget/routes/pl/airport/{origin}", TAG)
        out: dict[str, Airport] = {}
        if data:
            for r in data:
                if r.get("connectingAirport"):
                    continue
                a = _airport(r.get("arrivalAirport") or {})
                if a.code:
                    out[a.code] = a
        if out:
            return out
        # Zapas: wyszukiwarka „dokądkolwiek” zwraca najtańszy lot na każdy kierunek.
        for base in FARFND_BASES:
            data = self.http.json("GET", f"{base}/oneWayFares", TAG, params={
                "departureAirportIataCode": origin,
                "outboundDepartureDateFrom": start.isoformat(),
                "outboundDepartureDateTo": end.isoformat(),
                "currency": "PLN", "market": "pl-pl", "language": "pl",
            })
            if data and data.get("fares") is not None:
                for f in data["fares"]:
                    arr = (f.get("outbound") or {}).get("arrivalAirport") or {}
                    a = Airport(code=arr.get("iataCode", ""), name=arr.get("name", ""),
                                city=(arr.get("city") or {}).get("name", ""),
                                country=arr.get("countryName", ""))
                    if a.code:
                        out[a.code] = a
                break
        return out

    # --- ceny ------------------------------------------------------------
    def _cheapest_per_day(self, o: str, d: str, month: dt.date):
        bases = [self._base] if self._base else FARFND_BASES
        for base in bases:
            url = f"{base}/oneWayFares/{o}/{d}/cheapestPerDay"
            data = self.http.json("GET", url, TAG, params={
                "outboundMonthOfDate": month.isoformat(), "currency": "PLN",
            })
            if data and "outbound" in data:
                self._base = base
                return data
        return None

    def month_fares(self, o: str, d: str, month: dt.date, start: dt.date, end: dt.date) -> list[Fare]:
        data = self._cheapest_per_day(o, d, month)
        fares: list[Fare] = []
        if not data:
            return fares
        for f in (data.get("outbound") or {}).get("fares") or []:
            price = f.get("price") or {}
            if f.get("unavailable") or f.get("soldOut") or not price.get("value"):
                continue
            day = f.get("day") or (f.get("departureDate") or "")[:10]
            if not in_window(day, start, end):
                continue
            cur = price.get("currencyCode", "PLN")
            pln = self.fx.to_pln(float(price["value"]), cur)
            if pln is None:
                continue
            dep = f.get("departureDate") or ""
            arr = f.get("arrivalDate") or ""
            fares.append(Fare(NAME, o, d, day, dep[11:16] or None, pln,
                              float(price["value"]), cur, booking_url(o, d, day),
                              arr[:10] or None, arr[11:16] or None))
        return fares

    def fetch(self, origins: list[str], start: dt.date, end: dt.date) -> ProviderResult:
        res = ProviderResult(NAME)
        res.airports = self.airports()
        pairs: set[tuple[str, str]] = set()
        for o in origins:
            dests = self.destinations(o, start, end)
            if not dests:
                res.notes.append(f"{o}: brak tras Ryanair (albo blokada)")
            for code, a in dests.items():
                res.airports.setdefault(code, a).merge(a)
                pairs.add((o, code))
                pairs.add((code, o))
        res.routes = len(pairs) // 2
        months = month_starts(start, end)
        jobs = [(o, d, m) for (o, d) in sorted(pairs) for m in months]
        log.info("Ryanair: %d tras, %d zapytań o ceny", res.routes, len(jobs))
        with ThreadPoolExecutor(self.workers) as ex:
            for fares in ex.map(lambda j: self.month_fares(*j, start, end), jobs):
                res.fares.extend(fares)
        return res
