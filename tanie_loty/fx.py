"""Kursy walut z NBP (tabele A i B), przeliczanie na PLN."""
from __future__ import annotations

import logging

from .http import Http

log = logging.getLogger(__name__)

# Awaryjne, przybliżone kursy na wypadek niedostępności API NBP.
FALLBACK = {
    "PLN": 1.0, "EUR": 4.25, "GBP": 5.0, "USD": 3.9, "CHF": 4.6, "HUF": 0.0108,
    "CZK": 0.17, "SEK": 0.37, "NOK": 0.36, "DKK": 0.57, "RON": 0.85, "BGN": 2.17,
    "ILS": 1.05, "TRY": 0.1, "ISK": 0.03, "MAD": 0.39, "ALL": 0.044, "MKD": 0.069,
    "BAM": 2.17, "RSD": 0.036, "GEL": 1.43, "MDL": 0.22, "AED": 1.06, "SAR": 1.04,
    "AZN": 2.3, "KZT": 0.0078, "UAH": 0.094, "JOD": 5.5, "EGP": 0.08,
}


class Fx:
    def __init__(self, http: Http):
        self.rates = dict(FALLBACK)
        self.source = "fallback"
        got = 0
        for table in ("B", "A"):  # A na końcu, żeby nadpisała B tam, gdzie się pokrywają
            data = http.json("GET", f"https://api.nbp.pl/api/exchangerates/tables/{table}?format=json", "nbp")
            if data:
                for r in data[0].get("rates", []):
                    self.rates[r["code"]] = float(r["mid"])
                    got += 1
        if got:
            self.source = "NBP"
        else:
            log.warning("Brak kursów NBP — używam przybliżonych kursów awaryjnych")

    def to_pln(self, amount: float, currency: str) -> float | None:
        rate = self.rates.get((currency or "PLN").upper())
        if rate is None:
            log.warning("Nieznana waluta %s", currency)
            return None
        return amount * rate
