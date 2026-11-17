"""Wspólny klient HTTP: ponawianie, dławienie zapytań per host, statystyki."""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class Http:
    def __init__(self, min_interval: float = 0.3, timeout: float = 25):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        })
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=None,  # ponawiaj także POST (zapytania są idempotentne)
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.timeout = timeout
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.stats: Counter = Counter()   # (tag, status) -> liczba

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            wait = self._last.get(host, 0) + self.min_interval - now
            self._last[host] = max(now, self._last.get(host, 0) + self.min_interval)
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, url: str, tag: str, **kw) -> requests.Response | None:
        self._throttle(url)
        kw.setdefault("timeout", self.timeout)
        try:
            r = self.session.request(method, url, **kw)
        except requests.RequestException as e:
            self.stats[(tag, type(e).__name__)] += 1
            log.debug("%s %s -> %s", method, url, e)
            return None
        self.stats[(tag, r.status_code)] += 1
        if r.status_code >= 400:
            log.debug("%s %s -> HTTP %s: %s", method, url, r.status_code, r.text[:200])
        return r

    def json(self, method: str, url: str, tag: str, **kw):
        r = self.request(method, url, tag, **kw)
        if r is None or r.status_code >= 400:
            return None
        try:
            return r.json()
        except ValueError:
            self.stats[(tag, "bad-json")] += 1
            return None

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for (tag, status), n in sorted(self.stats.items(), key=lambda x: (x[0][0], str(x[0][1]))):
            out.setdefault(tag, {})[str(status)] = n
        return out
