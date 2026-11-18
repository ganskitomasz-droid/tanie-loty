"""Prawdziwe ceny noclegów z LiteAPI (Nuitée) — najtańsza oferta spełniająca kryteria:
do X km od centrum, bez hosteli, ocena gości ≥ min, łazienka w pokoju.

Działa tylko, gdy ustawiony jest sekret LITEAPI_KEY. Żeby nie nadużywać darmowego API
(dobry stosunek wyszukiwań do rezerwacji), pytamy tylko o najlepszy termin na kierunek,
a wyniki trzymamy w pamięci podręcznej przez `refresh_hours`.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import os

from .fx import Fx
from .http import Http
from .models import Airport, distance_km

log = logging.getLogger(__name__)
API = "https://api.liteapi.travel/v3.0"
TAG = "liteapi"
HOSTEL_TYPES = {203, 211}  # hostele, schroniska
SHARED = ("shared bath", "shared bathroom", "shared toilet", "shared facilities", "dorm", "bed in",
          "łazienka wspólna", "wspólna łazienka", "capsule", "pod ")


def _num(*vals):
    for v in vals:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                pass
    return None


def _amount(x):
    """Wyciąga kwotę z {amount, currency} albo [{amount, currency}]."""
    if isinstance(x, list):
        x = x[0] if x else None
    if isinstance(x, dict):
        return _num(x.get("amount")), x.get("currency")
    return None, None


class LiteApiHotels:
    def __init__(self, http: Http, fx: Fx, key: str, criteria: dict):
        self.http, self.fx, self.key = http, fx, key
        self.radius_km = float(criteria.get("max_km", 3))
        self.min_rating = float(criteria.get("min_rating", 7))
        self.adults = int(criteria.get("adults", 2))
        self.refresh = float(criteria.get("refresh_hours", 24))
        self.max_dest = int(criteria.get("max_destinations", 80))
        self.headers = {"X-API-Key": key, "accept": "application/json"}
        self.sample = ""
        self.errors = 0
        self.ratings: list[float] = []

    def _hotels_near(self, centre: dict, cc: str, cache: dict) -> list[dict]:
        key = f"{cc}:{centre['lat']:.3f},{centre['lon']:.3f}"
        hit = cache.get(key)
        if hit and hit.get("fetched", "") >= (dt.date.today() - dt.timedelta(days=30)).isoformat():
            return hit["hotels"]
        data = self.http.json("GET", f"{API}/data/hotels", TAG, headers=self.headers, params={
            "countryCode": cc, "latitude": centre["lat"], "longitude": centre["lon"],
            "radius": int(self.radius_km * 1000), "limit": 300,
        }, timeout=40)
        rows = (data or {}).get("data") or []
        if rows and not self.sample:
            self.sample = "hotel: " + ",".join(sorted(rows[0].keys()))
        hotels = []
        for h in rows:
            name = h.get("name") or ""
            htype = _num(h.get("hotelTypeId"), h.get("hotel_type_id"))
            if (htype is not None and int(htype) in HOSTEL_TYPES) or "hostel" in name.lower():
                continue
            rating = _num(h.get("rating"), h.get("reviewScore"), h.get("guestRating"))  # skala 0–10
            if rating is not None:
                self.ratings.append(rating)
            if rating is None or rating < self.min_rating:
                continue
            lat, lon = _num(h.get("latitude")), _num(h.get("longitude"))
            km = distance_km(Airport("", lat=lat, lon=lon), Airport("", lat=centre["lat"], lon=centre["lon"]))
            if km is None or km > self.radius_km:
                continue
            hotels.append({"id": h.get("id"), "name": name, "rating": round(rating, 1),
                           "reviews": h.get("reviewCount"), "stars": h.get("stars"), "km": round(km, 1)})
        if data is not None:
            cache[key] = {"fetched": dt.date.today().isoformat(), "hotels": hotels}
        return hotels

    def _cheapest(self, hotels: list[dict], checkin: str, checkout: str) -> dict | None:
        if not hotels:
            return None
        rooms = max(1, math.ceil(self.adults / 2))
        occ = [{"adults": self.adults // rooms + (1 if i < self.adults % rooms else 0)} for i in range(rooms)]
        body = {"hotelIds": [h["id"] for h in hotels[:200]], "checkin": checkin, "checkout": checkout,
                "currency": "EUR", "guestNationality": "PL", "occupancies": occ, "timeout": 8}
        data = self.http.json("POST", f"{API}/hotels/rates", TAG, headers=self.headers, json=body, timeout=60)
        if data is None:
            self.errors += 1
            return None
        by_id = {h["id"]: h for h in hotels}
        best = None
        for item in data.get("data") or []:
            h = by_id.get(item.get("hotelId"))
            if not h:
                continue
            for rt in item.get("roomTypes") or []:
                rates = rt.get("rates") or []
                names = " ".join(str(r.get("name", "")) for r in rates).lower()
                if any(s in names for s in SHARED):
                    continue  # bez łazienki w pokoju / sale wieloosobowe
                amt, cur = _amount(rt.get("offerRetailRate"))
                if amt is None:
                    parts = [_amount((r.get("retailRate") or {}).get("total")) for r in rates]
                    if parts and all(p[0] is not None for p in parts):
                        amt, cur = sum(p[0] for p in parts), parts[0][1]
                if not amt:
                    continue
                pln = self.fx.to_pln(amt, cur or "EUR")
                if pln and (best is None or pln < best["total"]):
                    room = (rates[0].get("name") if rates else "") or ""
                    best = {**h, "total": round(pln), "room": room[:80],
                            "board": (rates[0].get("boardName") if rates else "") or ""}
        return best

    def fetch(self, targets: dict[str, dict], airports: dict[str, Airport], centres: dict, cache: dict) -> dict:
        """targets: {kod: {"checkin", "checkout"}} → {kod: oferta}"""
        hcache = cache.setdefault("hotel_lists", {})
        offers = cache.setdefault("hotel_offers", {})
        now = dt.datetime.now(dt.timezone.utc)
        out = {}
        for code, t in list(targets.items())[: self.max_dest]:
            cen, a = centres.get(code), airports.get(code)
            if not cen or not a or not a.cc:
                continue
            k = f"{code}:{t['checkin']}:{t['checkout']}:{self.adults}"
            old = offers.get(k)
            if old and (now - dt.datetime.fromisoformat(old["fetched"])).total_seconds() < self.refresh * 3600:
                if old.get("offer"):
                    out[code] = old["offer"]
                continue
            offer = self._cheapest(self._hotels_near(cen, a.cc, hcache), t["checkin"], t["checkout"])
            if offer:
                nights = (dt.date.fromisoformat(t["checkout"]) - dt.date.fromisoformat(t["checkin"])).days
                offer.update({"checkin": t["checkin"], "checkout": t["checkout"], "adults": self.adults,
                              "nights": nights, "per_night": round(offer["total"] / max(1, nights)),
                              "fetched": now.isoformat(timespec="minutes")})
                out[code] = offer
            offers[k] = {"fetched": now.isoformat(timespec="minutes"), "offer": offer}
        # sprzątanie starych wpisów
        today = dt.date.today().isoformat()
        for k in [k for k, v in offers.items() if k.split(":")[1] < today]:
            del offers[k]
        return out


def hotel_targets(trips: dict[str, list[dict]]) -> dict[str, dict]:
    """Dla każdego kierunku: najtańszy wyjazd w obie strony → daty noclegu."""
    best: dict[str, dict] = {}
    for t in trips.get("rt", []):
        if t["dest"] not in best or t["total"] < best[t["dest"]]["total"]:
            best[t["dest"]] = t
    out = {}
    for code, t in sorted(best.items(), key=lambda x: x[1]["total"]):
        cin = t["out"].get("arr_date") or t["out"]["date"]
        if t["ret"]["date"] > cin:
            out[code] = {"checkin": cin, "checkout": t["ret"]["date"]}
    return out


def get_key() -> str:
    return os.environ.get("LITEAPI_KEY", "").strip()
