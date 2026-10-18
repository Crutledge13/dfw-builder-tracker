"""End-to-end test with fake builder pages: two runs, a price cut, an offer change,
a new home, and a sold home dropping off."""
import json
import re
import shutil
import tempfile
from datetime import timedelta

from tracker import run as R
from tracker.extract import extract_homes
from tracker.fetch import Response
from tracker.incentives import extract_offers
from tracker.store import LocalStore


def listing_page(homes):
    data = {"props": {"pageProps": {"inventory": [
        {"streetAddress": h[0], "city": "Celina", "zip": "75009", "communityName": "Mosaic", "planName": h[3],
         "price": h[1], "bedrooms": 4, "bathrooms": 3, "halfBaths": 1, "squareFeet": h[2],
         "detailUrl": "/new-homes/tx/celina/mosaic/" + re.sub(r"\W+", "-", h[0].lower()) + "/"}
        for h in homes]}}}
    return f'''<html><body><h1>Quick Move-In Homes</h1>
    <script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>
    <a href="/new-homes/tx/celina/mosaic/1404-amber-way/">1404 Amber Way</a></body></html>'''


def detail_page(price, was=None):
    was_html = f'<del class="was-price">${was:,}</del>' if was else ""
    return f'''<html><body><main><h1>1404 Amber Way</h1><p>Celina, TX 75009</p>
    {was_html}<span class="price">${price:,}</span>
    <ul><li>3 Beds</li><li>2.5 Baths</li><li>2,104 Sq Ft</li></ul>
    <p>Move-in ready!</p><p>Get up to $15,000 toward closing costs on this home.</p>
    <p>Estimated payment $2,890 per month</p></main></body></html>'''


def promo_page(rate):
    return f'''<html><body><h2>Fall Sales Event</h2>
    <p>Rates as low as {rate}% with a rate buydown on select quick move-in homes.</p>
    <p>Plus up to $20,000 in Flex Cash when you use our preferred lender.</p>
    <p>Visit our models today.</p></body></html>'''


class FakeClient:
    def __init__(self, pages):
        self.pages, self.blocked, self.requests_made = pages, {}, 0

    def _host(self, url):
        return re.sub(r"^https?://", "", url).split("/")[0]

    def get(self, url):
        self.requests_made += 1
        url = url.rstrip("/")
        for k, v in self.pages.items():
            if k.rstrip("/") == url:
                return Response(url, 200, v)
        return Response(url, 404, "")

    def sitemaps_from_robots(self, url):
        return []


B = {"name": "Test Builder", "slug": "test", "site": "https://tb.example/", "dfw_only": True, "sitemap": False,
     "seeds": ["https://tb.example/qmi"], "promos": ["https://tb.example/promos"],
     "detail_re": re.compile(r"/\d{2,6}-[a-z0-9-]+/?$"), "filter_re": None}
DETAIL = "https://tb.example/new-homes/tx/celina/mosaic/1404-amber-way"


def run_once(store, pages, now):
    st = store.load()
    rec = R.Recorder(st, now)
    pages_n, status, note = R.crawl_builder(B, FakeClient(pages), rec, budget=50)
    runlog = [{"run_at": R.ts(now), "builder": B["name"], "status": status, "pages": pages_n}]
    store.save(st, rec.changes, runlog, R.derived_tabs(st, rec.changes, now, runlog, [B]))
    return st, rec.changes


def test_extractors():
    homes = extract_homes(listing_page([("1500 Elm Street", 489990, 2450, "Seaberry")]), "https://tb.example/qmi")
    assert len(homes) == 1
    h = homes[0]
    assert (h["price"], h["beds"], h["baths"], h["sqft"], h["city"]) == (489990, 4, 3.5, 2450, "Celina")
    assert h["url"].endswith("/1500-elm-street/")
    d = extract_homes(detail_page(399990, was=424990), DETAIL, single=True)[0]
    assert d["price"] == 399990 and d["builder_was_price"] == 424990, d
    assert d["address"] == "1404 Amber Way" and d["sqft"] == 2104 and d["beds"] == 3 and d["city"] == "Celina", d
    assert "closing costs" in d["incentive_text"]
    offers = extract_offers(promo_page(3.99))
    assert {o["kind"] for o in offers} == {"rate", "cash_credit"}
    assert [o["rate"] for o in offers if o["kind"] == "rate"] == [3.99]


def test_two_runs_track_changes():
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp)
        t0 = R.now_local()
        pages1 = {"https://tb.example/qmi": listing_page([("1500 Elm Street", 489990, 2450, "Seaberry"),
                                                           ("1510 Elm Street", 519990, 2800, "Lily")]),
                  DETAIL: detail_page(424990),
                  "https://tb.example/promos": promo_page(3.99)}
        st, ch = run_once(store, pages1, t0)
        assert sum(1 for c in ch if c["event"] == "new_home") == 3
        assert sum(1 for c in ch if c["event"] == "offer_new") == 2

        # an hour later: Elm 1500 cut $15k, Amber Way cut $25k, rate offer changed, 1510 still listed
        pages2 = dict(pages1)
        pages2["https://tb.example/qmi"] = listing_page([("1500 Elm Street", 474990, 2450, "Seaberry"),
                                                         ("1510 Elm Street", 519990, 2800, "Lily")])
        pages2[DETAIL] = detail_page(399990, was=424990)
        pages2["https://tb.example/promos"] = promo_page(3.49)
        st, ch = run_once(store, pages2, t0 + timedelta(hours=1))
        cuts = sorted((c["address"], c["delta"]) for c in ch if c["event"] == "price_cut")
        assert cuts == [("1404 Amber Way", -25000), ("1500 Elm Street", -15000)], ch
        changed = [c for c in ch if c["event"] == "offer_changed"]
        assert len(changed) == 1 and "3.49" in changed[0]["detail"]

        # three days later: 1510 Elm gone from the listing -> removed
        pages3 = dict(pages2)
        pages3["https://tb.example/qmi"] = listing_page([("1500 Elm Street", 474990, 2450, "Seaberry")])
        st, ch = run_once(store, pages3, t0 + timedelta(hours=73))
        removed = [c["address"] for c in ch if c["event"] == "removed"]
        assert removed == ["1510 Elm Street"], ch

        # the sheet-shaped files exist and round-trip
        st2 = store.load()
        amber = next(h for h in st2.homes.values() if h["address"] == "1404 Amber Way")
        assert amber["first_price"] == 424990 and amber["price"] == 399990 and amber["cuts"] == 1
        assert amber["change_since_first"] == -25000
        drops = open(f"{tmp}/price_drops.csv").read()
        assert "1404 Amber Way" in drops and "1500 Elm Street" in drops
    finally:
        shutil.rmtree(tmp)


def test_large_jump_needs_confirmation():
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp)
        t0 = R.now_local()
        base = {"https://tb.example/qmi": listing_page([("1500 Elm Street", 489990, 2450, "A")])}
        run_once(store, base, t0)
        odd = {"https://tb.example/qmi": listing_page([("1500 Elm Street", 189990, 2450, "A")])}
        _, ch = run_once(store, odd, t0 + timedelta(hours=1))
        assert [c["event"] for c in ch] == ["price_unconfirmed"]
        _, ch = run_once(store, odd, t0 + timedelta(hours=2))
        assert [c["event"] for c in ch] == ["price_cut"]
    finally:
        shutil.rmtree(tmp)


def test_parallel_builders_share_state_safely():
    from concurrent.futures import ThreadPoolExecutor
    st = LocalStore(tempfile.mkdtemp()).load()
    rec = R.Recorder(st, R.now_local())
    builders = []
    for i in range(12):
        b = dict(B, name=f"Builder {i}", slug=f"b{i}")
        pages = {"https://tb.example/qmi": listing_page(
            [(f"{1000 + j} Oak Lane", 300000 + j * 1000, 2000, "P") for j in range(40)])}
        builders.append((b, FakeClient(pages)))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda bc: R.crawl_builder(bc[0], bc[1], rec, 60), builders))
    assert len(st.homes) == 12 * 40
