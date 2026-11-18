"""Prawdziwe ceny noclegów z Xotelo (dane TripAdvisora, ceny z wielu serwisów rezerwacyjnych).

Darmowe, bez klucza. Kryteria: do X km od centrum, bez hosteli, ocena TripAdvisora ≥ min_rating/2
(skala 1–5, czyli 7/10 → 3,5/5). Dla każdego kierunku: najtańsza oferta (najniższa cena spośród
serwisów) dla najtańszego terminu lotu. Wyniki trzymane w pamięci podręcznej `refresh_hours`.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re

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
        self.candidates = int(criteria.get("candidates", 15))
        self.max_new = int(criteria.get("max_new_per_run", 30))
        self.notes: list[str] = []
        self.types: dict[str, int] = {}
        self.rates_ok = 0
        self.stat: dict[str, int] = {}
        self.wd_samples: list[str] = []
        self.samples: list[str] = []
        self.rates_empty = 0

    def _get(self, path: str, params: dict):
        data = self.http.json("GET", f"{API}/{path}", TAG, params=params, timeout=40)
        if not isinstance(data, dict) or data.get("error"):
            if isinstance(data, dict) and data.get("error") and len(self.notes) < 3:
                self.notes.append(f"{path}: {str(data['error'])[:80]}")
            return None
        return data.get("result")

    def _wikidata_geo(self, centre: dict) -> str | None:
        """Identyfikator miejsca TripAdvisora (gNNNN) z Wikidata (właściwość P3134) dla
        najludniejszego obiektu w promieniu 10 km od centrum."""
        q = f"""SELECT ?ta ?pop ?dist WHERE {{
          SERVICE wikibase:around {{ ?item wdt:P625 ?loc .
            bd:serviceParam wikibase:center "Point({centre['lon']} {centre['lat']})"^^geo:wktLiteral .
            bd:serviceParam wikibase:radius "25" . bd:serviceParam wikibase:distance ?dist . }}
          ?item wdt:P3134 ?ta . OPTIONAL {{ ?item wdt:P1082 ?pop }}
        }} ORDER BY DESC(?pop) ASC(?dist) LIMIT 40"""
        data = self.http.json("GET", "https://query.wikidata.org/sparql", "wikidata",
                              params={"query": q, "format": "json"},
                              headers={"Accept": "application/sparql-results+json",
                                       "User-Agent": "tanie-loty/1.0 (github.com/ganskitomasz-droid/tanie-loty)"},
                              timeout=60)
        rows = ((data or {}).get("results") or {}).get("bindings") or []
        if len(self.wd_samples) < 3:
            self.wd_samples.append(f"{len(rows)}:{rows[0]['ta']['value'][:40] if rows else '-'}")
        if not rows and centre.get("name"):
            # zapas: wyszukanie miasta po nazwie (pl/en) z identyfikatorem TripAdvisora
            name = centre["name"].replace('"', "")
            q2 = f"""SELECT ?ta ?pop WHERE {{ VALUES ?lang {{ "pl" "en" }}
              ?item rdfs:label ?l . FILTER(?l = STRLANG("{name}", ?lang))
              ?item wdt:P3134 ?ta . OPTIONAL {{ ?item wdt:P1082 ?pop }} }} ORDER BY DESC(?pop) LIMIT 10"""
            data = self.http.json("GET", "https://query.wikidata.org/sparql", "wikidata",
                                  params={"query": q2, "format": "json"},
                                  headers={"Accept": "application/sparql-results+json",
                                           "User-Agent": "tanie-loty/1.0 (github.com/ganskitomasz-droid/tanie-loty)"},
                                  timeout=60)
            rows = ((data or {}).get("results") or {}).get("bindings") or []
        # najpierw obiekty z populacją (miasta), potem najczęstszy identyfikator miejsca
        counts: dict[str, int] = {}
        for r in rows:
            v = r.get("ta", {}).get("value", "")
            m = re.fullmatch(r"g?(\d+)", v) or re.search(r"-g(\d+)-", v)
            if not m:
                continue
            g = "g" + m.group(1)
            if "pop" in r:  # miasto (ma populację) — jego identyfikator to identyfikator miejsca
                return g
            if v.startswith("g") or "-g" in v:
                counts[g] = counts.get(g, 0) + 1
        return max(counts, key=counts.get) if counts else None

    def _location_key(self, city: str, country: str, cache: dict, centre: dict | None = None) -> str | None:
        k = f"{city}|{country}"
        if cache.get(k):
            return cache[k]
        res = self._wikidata_geo(centre) if centre else None
        if res:
            cache[k] = res
        return res

    def _hotels(self, loc: str, centre: dict, cache: dict) -> list[dict]:
        hit = cache.get(loc)
        if hit and hit.get("fetched", "") >= (dt.date.today() - dt.timedelta(days=30)).isoformat():
            return hit["hotels"]
        # Dwie listy: „best_value” (dobry stosunek jakości do ceny — tu są tanie, dobrze oceniane
        # obiekty) i „distance” (najbliżej centrum). Kolejność best_value decyduje o wyborze kandydatów.
        rows, ok, seen = [], False, set()
        for sort in ("best_value", "distance"):
            r = self._get("list", {"location_key": loc, "limit": 100, "offset": 0, "sort": sort})
            if r is None:
                continue
            ok = True
            for h in r.get("list") or []:
                if h.get("key") not in seen:
                    seen.add(h.get("key"))
                    rows.append(h)
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
                           "url": h.get("url"), "min": pr.get("minimum"), "max": pr.get("maximum")})
        if ok:
            cache[loc] = {"fetched": dt.date.today().isoformat(), "hotels": hotels}
        return hotels

    def _cheapest(self, hotels: list[dict], checkin: str, checkout: str) -> dict | None:
        nights = (dt.date.fromisoformat(checkout) - dt.date.fromisoformat(checkin)).days
        rooms = max(1, math.ceil(self.adults / 2))
        # najpierw obiekty z najniższą typową ceną (jeśli znana), dalej w kolejności „best value”
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
            rate = cheapest["rate"]
            typical_max = h.get("max") or 0
            # Jeśli „cena za noc” jest wyraźnie wyższa niż typowy przedział obiektu, a pobyt ma kilka nocy,
            # to w praktyce jest to cena za cały pobyt — dzielimy przez liczbę nocy.
            if nights > 1 and typical_max and rate > 1.6 * typical_max and rate / nights <= 1.6 * typical_max:
                rate = rate / nights
                self.stat["cena za pobyt → za noc"] = self.stat.get("cena za pobyt → za noc", 0) + 1
            if len(self.samples) < 6:
                self.samples.append(f"{h['name'][:25]}: {cheapest['rate']}€ ({nights} n., typowo {h.get('min')}–{typical_max})")
            per_night = self.fx.to_pln(rate, "EUR") * rooms   # cena za pokój za noc × pokoje
            total = per_night * nights
            if best is None or total < best["total"]:
                best = {"name": h["name"], "rating": round(h["rating"] * 2, 1), "rating5": h["rating"],
                        "reviews": h["reviews"], "km": h["km"], "type": h["type"], "url": h["url"],
                        "site": cheapest.get("name"), "total": round(total), "per_night": round(per_night),
                        "nights": nights, "rooms": rooms}
        return best

    def fetch(self, targets: dict[str, dict], airports: dict[str, Airport], centres: dict, cache: dict) -> dict:
        for old in ("xotelo_locations", "xotelo_offers", "xotelo_offers2", "xotelo_offers3"):  # wpisy z wersji z płatną wyszukiwarką
            cache.pop(old, None)
        locs = cache.setdefault("xotelo_geo", {})
        for old in ("xotelo_lists", "xotelo_lists2"):  # starsze wersje list (sortowanie po odległości)
            cache.pop(old, None)
        lists = cache.setdefault("xotelo_lists3", {})
        offers = cache.setdefault("xotelo_offers4", {})
        now = dt.datetime.now(dt.timezone.utc)
        out = {}
        new = 0
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
            if new >= self.max_new:  # ogranicz czas przebiegu; reszta w kolejnych przebiegach
                self.stat["odłożone do następnego przebiegu"] = self.stat.get("odłożone do następnego przebiegu", 0) + 1
                continue
            new += 1
            loc = self._location_key(cen.get("name") or a.city, a.country, locs, cen)
            if not loc:
                self.stat["bez identyfikatora miasta"] = self.stat.get("bez identyfikatora miasta", 0) + 1
                continue  # nie zapisujemy porażki — spróbujemy w następnym przebiegu
            hotels = self._hotels(loc, cen, lists)
            if not hotels:
                self.stat["brak obiektów po filtrach"] = self.stat.get("brak obiektów po filtrach", 0) + 1
            offer = self._cheapest(hotels, t["checkin"], t["checkout"]) if hotels else None
            if hotels and not offer:
                self.stat["brak cen na te daty"] = self.stat.get("brak cen na te daty", 0) + 1
            if offer:
                offer.update({"checkin": t["checkin"], "checkout": t["checkout"], "adults": self.adults,
                              "source": "Xotelo/TripAdvisor", "fetched": now.isoformat(timespec="minutes")})
                out[code] = offer
            offers[k] = {"fetched": now.isoformat(timespec="minutes"), "offer": offer}
        today = dt.date.today().isoformat()
        for k in [k for k, v in offers.items() if k.split(":")[1] < today]:
            del offers[k]
        return out
