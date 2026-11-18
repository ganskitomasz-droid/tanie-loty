from tanie_loty.hotels import LiteApiHotels, hotel_targets
from tanie_loty.models import Airport


class FakeFx:
    def to_pln(self, amount, cur):
        return amount * 4.0


class FakeHttp:
    def __init__(self):
        self.calls = []

    def json(self, method, url, tag, **kw):
        self.calls.append(url)
        if url.endswith("/data/hotels"):
            return {"data": [
                {"id": "h1", "name": "Hotel Centrum", "rating": 8.1, "reviewCount": 900, "stars": 3,
                 "latitude": 41.390, "longitude": 2.170, "hotelTypeId": 204},
                {"id": "h2", "name": "Cheap Hostel", "rating": 8.5, "latitude": 41.390, "longitude": 2.170},
                {"id": "h3", "name": "Słaby Hotel", "rating": 6.2, "latitude": 41.390, "longitude": 2.170},
                {"id": "h4", "name": "Daleki Hotel", "rating": 9.0, "latitude": 41.50, "longitude": 2.30},
                {"id": "h5", "name": "Pensjonat", "rating": 7.4, "latitude": 41.392, "longitude": 2.171},
            ]}
        return {"data": [
            {"hotelId": "h1", "roomTypes": [{"offerRetailRate": {"amount": 200, "currency": "EUR"},
                                             "rates": [{"name": "Double Room", "boardName": "Room only"}]}]},
            {"hotelId": "h5", "roomTypes": [
                {"offerRetailRate": {"amount": 90, "currency": "EUR"},
                 "rates": [{"name": "Double Room with Shared Bathroom"}]},
                {"offerRetailRate": {"amount": 150, "currency": "EUR"},
                 "rates": [{"name": "Double Room with Private Bathroom"}]}]},
        ]}


def test_cheapest_matching_offer():
    lh = LiteApiHotels(FakeHttp(), FakeFx(), "k", {"max_km": 3, "min_rating": 7, "adults": 2})
    ap = {"BCN": Airport("BCN", lat=41.30, lon=2.08, cc="ES")}
    cen = {"BCN": {"lat": 41.389, "lon": 2.169, "name": "Barcelona", "km": 12}}
    out = lh.fetch({"BCN": {"checkin": "2026-12-18", "checkout": "2026-12-22"}}, ap, cen, {})
    o = out["BCN"]
    # hostel, ocena < 7 i hotel > 3 km odrzucone; pokój ze wspólną łazienką pominięty
    assert o["name"] == "Pensjonat" and o["total"] == 600 and o["per_night"] == 150 and o["nights"] == 4


def test_hotel_targets_use_cheapest_trip_and_arrival_date():
    trips = {"rt": [
        {"dest": "BCN", "total": 300, "out": {"date": "2026-12-18"}, "ret": {"date": "2026-12-22"}},
        {"dest": "BCN", "total": 200, "out": {"date": "2026-12-10", "arr_date": "2026-12-11"},
         "ret": {"date": "2026-12-14"}},
    ]}
    assert hotel_targets(trips) == {"BCN": {"checkin": "2026-12-11", "checkout": "2026-12-14"}}


class FakeXHttp:
    def json(self, method, url, tag, **kw):
        p = kw.get("params", {})
        if url.endswith("/search"):
            return {"error": None, "result": {"list": [{"location_key": "g187497", "name": "Barcelona"}]}}
        if url.endswith("/list"):
            mk = lambda n, t, r, lat, mn: {"name": n, "key": "g1-" + n, "accommodation_type": t,
                                           "review_summary": {"rating": r, "count": 100},
                                           "geo": {"latitude": lat, "longitude": 2.17},
                                           "price_ranges": {"minimum": mn, "maximum": 300}, "url": "https://ta/" + n}
            return {"error": None, "result": {"list": [
                mk("Tani Hostel", "Hostel", 4.5, 41.39, 20), mk("Słaby", "Hotel", 3.0, 41.39, 30),
                mk("Daleki", "Hotel", 4.5, 41.50, 40), mk("Dobry", "Hotel", 4.0, 41.39, 60),
                mk("Pensjonat", "B&B", 4.5, 41.391, 50)]}}
        rate = {"g1-Dobry": 70, "g1-Pensjonat": 80}[p["hotel_key"]]
        return {"error": None, "result": {"rates": [{"name": "Booking.com", "rate": rate}, {"name": "X", "rate": rate + 5}]}}


def test_xotelo_cheapest():
    from tanie_loty.xotelo import XoteloHotels
    xh = XoteloHotels(FakeXHttp(), FakeFx(), {"max_km": 3, "min_rating": 7, "adults": 2})
    ap = {"BCN": Airport("BCN", city="Barcelona", country="Hiszpania", lat=41.30, lon=2.08, cc="ES")}
    cen = {"BCN": {"lat": 41.389, "lon": 2.169, "name": "Barcelona", "km": 12}}
    o = xh.fetch({"BCN": {"checkin": "2026-12-18", "checkout": "2026-12-20"}}, ap, cen, {})["BCN"]
    assert o["name"] == "Dobry" and o["per_night"] == 280 and o["total"] == 560 and o["site"] == "Booking.com"
