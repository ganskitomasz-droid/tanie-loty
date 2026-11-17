from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..models import Airport, Fare


@dataclass
class ProviderResult:
    name: str
    fares: list[Fare] = field(default_factory=list)
    airports: dict[str, Airport] = field(default_factory=dict)
    routes: int = 0
    notes: list[str] = field(default_factory=list)


def month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    out, cur = [], start.replace(day=1)
    while cur <= end:
        out.append(cur)
        cur = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


def in_window(date_str: str, start: dt.date, end: dt.date) -> bool:
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return False
    return start <= d <= end
