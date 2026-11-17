from tanie_loty.combine import combine
from tanie_loty.models import Airport, Fare


def F(c, o, d, date, p):
    return Fare(c, o, d, date, "10:00", p, p, "PLN", "")


AP = {
    "WAW": Airport("WAW", lat=52.17, lon=20.97),
    "BGY": Airport("BGY", lat=45.67, lon=9.70),
    "MXP": Airport("MXP", lat=45.63, lon=8.72),
    "LIS": Airport("LIS", lat=38.77, lon=-9.13),
}


def test_round_trip_mixes_carriers_and_respects_nights():
    fares = [
        F("Ryanair", "WAW", "BGY", "2026-12-04", 50),
        F("Wizz Air", "BGY", "WAW", "2026-12-06", 60),   # 2 noce — OK
        F("Ryanair", "BGY", "WAW", "2026-12-05", 10),    # 1 noc — poza zakresem
    ]
    r = combine(fares, AP, ["WAW"], nights_min=2, nights_max=5)
    assert len(r["rt"]) == 1
    t = r["rt"][0]
    assert t["total"] == 110 and t["nights"] == 2
    assert {t["out"]["carrier"], t["ret"]["carrier"]} == {"Ryanair", "Wizz Air"}


def test_open_jaw_only_for_nearby_airports():
    fares = [
        F("Ryanair", "WAW", "BGY", "2026-12-04", 50),
        F("Wizz Air", "MXP", "WAW", "2026-12-07", 40),   # ~76 km od BGY
        F("Wizz Air", "LIS", "WAW", "2026-12-07", 5),    # daleko — nie open-jaw
    ]
    r = combine(fares, AP, ["WAW"], openjaw_km=350)
    assert [(t["dest"], t["back"]) for t in r["oj"]] == [("BGY", "MXP")]


def test_max_total_filter():
    fares = [F("Ryanair", "WAW", "LIS", "2026-12-04", 300), F("Ryanair", "LIS", "WAW", "2026-12-08", 300)]
    assert combine(fares, AP, ["WAW"], max_total=500)["rt"] == []
