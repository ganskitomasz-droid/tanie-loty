# Tanie loty z Warszawy

Wyszukiwarka tanich połączeń Ryanair i Wizz Air, uruchamiana automatycznie w GitHub Actions.
Wyniki trafiają na stronę GitHub Pages z filtrami.

## Co znajduje

- **W obie strony, dokądkolwiek** – najtańsze wyjazdy z WAW/WMI na 2–5 nocy, także z mieszaniem linii
  (np. tam Ryanairem z Modlina, z powrotem Wizz Airem na Okęcie).
- **Open-jaw** – przylot do jednego miasta, powrót z innego lotniska w promieniu 350 km
  (np. przylot do Wenecji, powrót z Bergamo).
- **W jedną stronę** – najtańsze bilety na każdy kierunek.

## Jak to działa

1. Workflow `.github/workflows/szukaj.yml` uruchamia się co 6 godzin (oraz ręcznie: zakładka
   **Actions → Szukaj tanich lotów → Run workflow**, gdzie można zmienić lotniska, liczbę nocy itp.).
2. `python -m tanie_loty` pobiera kalendarze cen z publicznych (nieoficjalnych) endpointów
   ryanair.com i wizzair.com, przelicza waluty po kursach NBP i składa kombinacje.
3. Wynik (`results.json` + `index.html`) jest publikowany na GitHub Pages.

## Ustawienia

Domyślne parametry są w `config.json`:

| klucz | znaczenie |
|---|---|
| `origins` | lotniska startowe (traktowane jak jedno miasto) |
| `months` | ile miesięcy do przodu przeszukiwać |
| `nights_min`, `nights_max` | długość pobytu |
| `openjaw_km` | maks. odległość między lotniskiem przylotu i powrotu |
| `max_total` | maks. cena całej podróży w PLN (0 = bez limitu) |
| `providers` | `ryanair`, `wizzair` |

## Uruchomienie lokalne

```bash
pip install -r requirements.txt
python -m tanie_loty --out public        # prawdziwe wyszukiwanie
python -m tanie_loty --demo --out public # dane losowe (test strony)
python -m http.server -d public          # podgląd: http://localhost:8000
```

## Zastrzeżenia

Endpointy linii nie są oficjalnym API — mogą się zmienić lub zostać zablokowane bez ostrzeżenia.
Ceny to najtańsza taryfa dnia, bez bagażu i opłat dodatkowych; zawsze sprawdź cenę na stronie
przewoźnika przed zakupem. Open-jaw wymaga samodzielnego dojazdu między lotniskami.
