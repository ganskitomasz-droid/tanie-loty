from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass
class Airport:
    code: str
    name: str = ""
    city: str = ""
    country: str = ""
    lat: float | None = None
    lon: float | None = None
    cc: str = ""          # kod kraju ISO 3166-1 alpha-2, wielkimi literami

    def merge(self, other: "Airport") -> None:
        """Uzupełnia brakujące pola danymi z innego źródła."""
        for f in ("name", "city", "country", "lat", "lon", "cc"):
            if not getattr(self, f) and getattr(other, f):
                setattr(self, f, getattr(other, f))


@dataclass
class Fare:
    carrier: str          # "Ryanair" / "Wizz Air"
    origin: str           # IATA
    dest: str             # IATA
    date: str             # YYYY-MM-DD (data wylotu, czas lokalny)
    time: str | None      # HH:MM lub None
    price: float          # cena w PLN
    price_orig: float     # cena w walucie oryginalnej
    currency: str
    url: str
    arr_date: str | None = None   # YYYY-MM-DD przylotu (czas lokalny)
    arr_time: str | None = None   # HH:MM przylotu

    def to_dict(self) -> dict:
        d = asdict(self)
        d["price"] = round(self.price, 2)
        return d


def distance_km(a: Airport, b: Airport) -> float | None:
    if None in (a.lat, a.lon, b.lat, b.lon):
        return None
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
