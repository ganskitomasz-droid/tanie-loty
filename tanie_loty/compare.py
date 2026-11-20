"""Porównanie miejsc wylotu (np. Warszawa vs Kraków): najtańszy wyjazd w obie strony na każdy kierunek.

Uruchamiane po wyszukiwaniach: python -m tanie_loty.compare public
Zapisuje public/compare.json i wypisuje podsumowanie (adnotacja w GitHub Actions).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def cheapest(path: Path) -> tuple[dict, dict, dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    legs = d.get("legs") or []
    best: dict[str, int] = {}
    for row in d.get("rt", []):
        dest, total = (row[2], row[5]) if isinstance(row, list) else (row["dest"], row["total"])
        if dest not in best or total < best[dest]:
            best[dest] = total
    return best, d["airports"], d["meta"]["config"].get("group", {})


def main(folder: str) -> int:
    out = Path(folder)
    groups = {}
    airports = {}
    for f in sorted(out.glob("results-*.json")):
        code = f.stem.split("-", 1)[1]
        best, ap, g = cheapest(f)
        groups[code] = {"best": best, "access": g.get("access_pln", 0), "name": g.get("name", code)}
        airports.update(ap)
    if len(groups) < 2:
        return 0
    dests = sorted(set().union(*(g["best"] for g in groups.values())))
    table = {}
    for dest in dests:
        row = {}
        for code, g in groups.items():
            if dest in g["best"]:
                row[code] = {"flights": g["best"][dest], "with_access": g["best"][dest] + g["access"]}
        table[dest] = row
    (out / "compare.json").write_text(json.dumps({"groups": {k: {"name": v["name"], "access": v["access"]}
                                                             for k, v in groups.items()}, "dests": table},
                                                 ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    codes = list(groups)
    lines = []
    city = lambda d: (airports.get(d) or {}).get("city") or d
    for code in codes:
        only = sorted((d for d in dests if set(table[d]) == {code}), key=lambda d: table[d][code]["flights"])
        lines.append(f"Tylko z: {groups[code]['name']} ({len(only)}): " +
                     ", ".join(f"{city(d)} ({d}) od {table[d][code]['flights']} zł" for d in only))
    for code in codes:  # gdzie dane miasto wylotu jest tańsze mimo dojazdu
        wins = []
        for d in dests:
            if code in table[d] and len(table[d]) > 1:
                rest = min(v["with_access"] for k, v in table[d].items() if k != code)
                diff = rest - table[d][code]["with_access"]
                if diff > 0:
                    wins.append((diff, d))
        wins.sort(reverse=True)
        lines.append(f"Taniej z: {groups[code]['name']} (z dojazdem) — {len(wins)} kierunków, np. " +
                     ", ".join(f"{city(d)} o {diff} zł" for diff, d in wins[:15]))
    msg = "\n".join(lines)
    print(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice title=Porównanie miejsc wylotu::" + msg.replace("%", "%25").replace("\n", "%0A"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "public"))
