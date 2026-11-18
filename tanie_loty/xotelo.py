"""Prawdziwe ceny noclegów z Xotelo (dane TripAdvisora, ceny z wielu serwisów rezerwacyjnych).

Darmowe, bez klucza. Kryteria: do X km od centrum, bez hosteli, ocena TripAdvisora ≥ min_rating/2
(skala 1–5, czyli 7/10 → 3,5/5). Dla każdego kierunku: najtańsza oferta (najniższa cena spośród
serwisów) dla najtańszego terminu lotu. Wyniki trzymane w pamięci podręcznej `refresh_hours`.
"""
from __future__ import annotations

import datetime as dt
import logging
import math

from .fx import Fx
from .http import Http
from .models import Airport, distance_km

log = logging.getLogger(__name__)
API = "https://data.xotelo.com/api"
TAG = "xotelo"
EXCLUDE_TYPES = ("hostel", "camp", "dorm")
EXCLUDE_NAMES = ("hostel", "dorm", "capsule", "backpack")


class XoteloHotels:
    def __init__(self, http: Http, fx: Fx, criteria: dict):
        self.http, self.fx = http, fx
        self.radius_km = float(criteria.get("max_km", 3))
        self.min_rating = float(criteria.get("min_rating", 7)) / 2   # 10 → 5 (skala TripAdvisora)
        self.adults = int(criteria.get("adults", 2))
        self.refresh = float(criteria.get("refresh_hours", 24))
        self.max_dest = int(criteria.get("max_destinations", 80))
        self.candidates = int(criteria.get("candidates", 10))
        self.notes: list[str] = []
        self.types: dict[str, int] = {}
        self.rates_ok = 0
        self.rates_empty = 0

    def _get(self, path: str, params: dict):
        data = self.http.json("GET", f"{API}/{path}", TAG, params=params, timeout=40)
        if not isinstance(data, dict) or data.get("error"):
            if isinstance(data, dict) and data.get("error") and len(self.notes) < 3:
                self.notes.append(f"{path}: {str(data['error'])[:80]}")
            return None
        return data.get("result")

    def _location_key(self, city: str, country: str, cache: dict) -> str | None:
        k = f"{city}|{country}"
        if k in cache:
            return cache[k]
        res = None
        for q in (f"{city} {country}".strip(), city):
            r = self._get("search", {"query": q, "location_type": "geo"})
            items = (r or {}).get("list") or []
            for it in items:
                key = it.get("location_key") or ""
                if key.startswith("g"):
                    res = key.split("-")[0]
                    break
            if res or r is not None:
                break
        cache[k] = res
        return res

    def _hotels(self, loc: str, centre: dict, cache: dict) -> list[dict]:
        hit = cache.get(loc)
        if hit and hit.get("fetched", "") >= (dt.date.today() - dt.timedelta(days=30)).isoformat():
            return hit["hotels"]
        rows, ok = [], False
        for offset in (0, 100):
            r = self._get("list", {"location_key": loc, "limit": 100, "offset": offset, "sort": "distance"})
            if r is None:
                break
            ok = True
            rows += r.get("list") or []
            if len(r.get("list") or []) < 100:
                break
        hotels = []
        for h in rows:
            t = (h.get("accommodation_type") or "").lower()
            self.types[t] = self.types.get(t, 0) + 1
            name = h.get("name") or ""
            if any(x in t for x in EXCLUDE_TYPES) or any(x in name.lower() for x in EXCLUDE_NAMES):
                continue
            rs = h.get("review_summary") or {}
            rating = rs.get("rating")
            if not isinstance(rating, (int, float)) or rating < self.min_rating:
                continue
            g = h.get("geo") or {}
            km = distance_km(Airport("", lat=g.get("latitude"), lon=g.get("longitude")),
                             Airport("", lat=centre["lat"], lon=centre["lon"]))
            if km is None or km > self.radius_km:
                continue
            pr = h.get("price_ranges") or {}
            hotels.append({"key": h.get("key"), "name": name, "type": h.get("accommodation_type"),
                           "rating": rating, "reviews": rs.get("count"), "km": round(km, 1),
                           "url": h.get("url"), "min": pr.get("minimum")})
        if ok:
            cache[loc] = {"fetched": dt.date.today().isoformat(), "hotels": hotels}
        return hotels

    def _cheapest(self, hotels: list[dict], checkin: str, checkout: str) -> dict | None:
        nights = (dt.date.fromisoformat(checkout) - dt.date.fromisoformat(checkin)).days
        rooms = max(1, math.ceil(self.adults / 2))
        # najpierw obiekty z najniższą typową ceną
        cands = sorted(hotels, key=lambda h: (h["min"] is None, h["min"] or 0))[: self.candidates]
        best = None
        for h in cands:
            r = self._get("rates", {"hotel_key": h["key"], "chk_in": checkin, "chk_out": checkout,
                                    "currency": "EUR", "rooms": rooms, "adults": self.adults})
            rates = [x for x in (r or {}).get("rates") or [] if isinstance(x.get("rate"), (int, float)) and x["rate"] > 0]
            if not rates:
                self.rates_empty += 1
                continue
            self.rates_ok += 1
            cheapest = min(rates, key=lambda x: x["rate"])
            per_night = self.fx.to_pln(cheapest["rate"], "EUR") * rooms   # cena za pokój za noc × pokoje
            total = per_night * nights
            if best is None or total < best["total"]:
                best = {"name": h["name"], "rating": round(h["rating"] * 2, 1), "rating5": h["rating"],
                        "reviews": h["reviews"], "km": h["km"], "type": h["type"], "url": h["url"],
                        "site": cheapest.get("name"), "total": round(total), "per_night": round(per_night),
                        "nights": nights, "rooms": rooms}
        return best

    def fetch(self, targets: dict[str, dict], airports: dict[str, Airport], centres: dict, cache: dict) -> dict:
        locs = cache.setdefault("xotelo_locations", {})
        lists = cache.setdefault("xotelo_lists", {})
        offers = cache.setdefault("xotelo_offers", {})
        now = dt.datetime.now(dt.timezone.utc)
        out = {}
        for code, t in list(targets.items())[: self.max_dest]:
            cen, a = centres.get(code), airports.get(code)
            if not cen or not a:
                continue
            k = f"{code}:{t['checkin']}:{t['checkout']}:{self.adults}"
            old = offers.get(k)
            if old and (now - dt.datetime.fromisoformat(old["fetched"])).total_seconds() < self.refresh * 3600:
                if old.get("offer"):
                    out[code] = old["offer"]
                continue
            loc = self._location_key(cen.get("name") or a.city, a.country, locs)
            offer = self._cheapest(self._hotels(loc, cen, lists), t["checkin"], t["checkout"]) if loc else None
            if offer:
                offer.update({"checkin": t["checkin"], "checkout": t["checkout"], "adults": self.adults,
                              "source": "Xotelo/TripAdvisor", "fetched": now.isoformat(timespec="minutes")})
                out[code] = offer
            offers[k] = {"fetched": now.isoformat(timespec="minutes"), "offer": offer}
        today = dt.date.today().isoformat()
        for k in [k for k, v in offers.items() if k.split(":")[1] < today]:
            del offers[k]
        return out
