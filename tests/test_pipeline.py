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
        sold = list(__import__("csv").DictReader(open(f"{tmp}/sold_removed.csv")))
        assert [r["address"] for r in sold] == ["1510 Elm Street"] and sold[0]["last_price"] == "519990"
        assert sold[0]["days_listed"] == "3"
        new = list(__import__("csv").DictReader(open(f"{tmp}/new_homes.csv")))
        assert new == []  # everything here was in the first (baseline) run

        # the sheet-shaped files exist and round-trip
        st2 = store.load()
        amber = next(h for h in st2.homes.values() if h["address"] == "1404 Amber Way")
        assert amber["first_price"] == 424990 and amber["price"] == 399990 and amber["cuts"] == 1
        assert amber["change_since_first"] == -25000
        offers = list(__import__("csv").DictReader(open(f"{tmp}/home_offers.csv")))
        amber_offer = next(o for o in offers if o["address"] == "1404 Amber Way")
        assert amber_offer["offer_type"] == "closing_costs" and amber_offer["offer_amount"] == "15000"
        assert "closing costs" in amber_offer["incentive_text"]
        api = json.load(open(f"{tmp}/api/homes.json"))
        a = next(x for x in api if x["address"] == "1404 Amber Way")
        assert a["price"] == 399990 and a["first_price"] == 424990 and a["offer"]["amount"] == 15000
        hist = json.load(open(f"{tmp}/api/price_history.json"))[a["id"]]
        assert [p["price"] for p in hist] == [424990, 399990]
        assert json.load(open(f"{tmp}/api/meta.json"))["counts"]["homes"] == len(api)
        drops = open(f"{tmp}/price_drops.csv").read()
        assert "toward closing costs" in drops  # each drop shows that home's advertised offer
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


def test_detail_page_ignores_similar_homes():
    others = [{"@type": "SingleFamilyResidence", "address": {"streetAddress": "11033 Cottonbowl Drive",
               "addressLocality": "Fort Worth", "postalCode": "76126"}, "offers": {"price": 276733},
               "numberOfBedrooms": 3, "floorSize": {"value": 1800}},
              {"@type": "SingleFamilyResidence", "address": {"streetAddress": "3037 New Chapel Street",
               "addressLocality": "Fort Worth", "postalCode": "76126"}, "offers": {"price": 332362},
               "numberOfBedrooms": 4, "floorSize": {"value": 2200}}]
    html = f'<html><body><script type="application/ld+json">{json.dumps(others)}</script></body></html>'
    url = "https://www.meritagehomes.com/state/tx/dallasft-worth/3037-new-chapel-street"
    h = extract_homes(html, url, single=True)
    assert len(h) == 1 and h[0]["address"] == "3037 New Chapel Street" and h[0]["price"] == 332362
    # nothing on the page is this home -> no JSON record is used
    url2 = "https://www.meritagehomes.com/state/tx/dallasft-worth/9999-other-lane"
    assert all(x["address"] != "11033 Cottonbowl Drive" for x in extract_homes(html, url2, single=True))


def test_sale_price_beats_list_price_and_payment_is_ignored():
    html = '''<html><body><div class="hero"><h1>2208 Continent Court</h1><p>Krum, Texas 76249</p>
      <p>Priced at <span class="price slash">$702,050</span> Sale <span class="price sale-price">$609,900</span></p>
      <p>Est. Payment <span>$3,410</span>/mo</p></div><main><p>Sales Price of $450,000 example</p></main></body></html>'''
    url = "https://www.riversidehomebuilders.com/quick-move-in/2208-continent-court"
    h = extract_homes(html, url, single=True)[0]
    assert (h["price"], h["builder_was_price"], h["address"], h["city"]) == (609900, 702050, "2208 Continent Court", "Krum")


def test_json_endpoint_with_html_cards():
    card = ('<div class="card"><a href="/quick-move-in-homes/anna/16513/1212-oak-hollow-lane">1212 Oak Hollow Lane</a>'
            '<p>Anna, TX 75409</p><p class="price">$389,990</p><p>4 Beds 3 Baths 2,301 Sq Ft</p>'
            '<p>Up to $20K Flex Cash</p></div>')
    payload = json.dumps({"success": True, "count": 1, "html": card})
    r = R.unwrap_json_html(Response("https://www.firsttexashomes.com/include/ajax/qmi.php", 200, payload))
    b = dict(B, detail_re=re.compile(r"(/\d{2,6}-[a-z0-9-]+/?$)|(/quick-move-in-homes?/(?!city/)[^?#]*\d[^?#]*$)"))
    from tracker.extract import card_homes
    cards = card_homes(r.text, r.url, b["detail_re"])
    assert len(cards) == 1 and cards[0]["price"] == 389990 and cards[0]["address"] == "1212 Oak Hollow Lane"
    assert cards[0]["city"] == "Anna" and "Flex Cash" in cards[0]["incentive_text"]


def test_sources_disagree_no_fake_changes():
    """Listing data shows a plan base price; the home's own page shows the real price."""
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp)
        t0 = R.now_local()
        listing = listing_page([("1404 Amber Way", 578375, 2104, "Plan")])
        pages = {"https://tb.example/qmi": listing, DETAIL: detail_page(609900)}
        _, ch = run_once(store, pages, t0)
        assert [c["event"] for c in ch] == ["new_home"] and ch[0]["new"] == 609900, ch
        _, ch = run_once(store, pages, t0 + timedelta(hours=1))   # same disagreement again: nothing
        assert ch == [], ch
        pages2 = dict(pages, **{DETAIL: detail_page(599900)})       # real cut on the home's page
        _, ch = run_once(store, pages2, t0 + timedelta(hours=2))
        assert [(c["event"], c["delta"]) for c in ch] == [("price_cut", -10000)], ch
    finally:
        shutil.rmtree(tmp)


def test_lot_code_page_uses_title_address_and_skips_plans():
    similar = [{"@type": "SingleFamilyResidence", "address": {"streetAddress": "1557 Thicket Hill Drive"},
                "offers": {"price": 503990}, "numberOfBedrooms": 4, "floorSize": {"value": 2600}}]
    html = (f'<html><head><title>New Home for Sale: 2616 Manzanar Mews, Celina, TX 75009</title></head><body>'
            f'<h1>Highland Homes</h1><main><p class="price">$562,926</p><p>4 Beds 3 Baths 2,458 Sq Ft</p></main>'
            f'<script type="application/ld+json">{json.dumps(similar)}</script></body></html>')
    h = extract_homes(html, "https://www.highlandhomes.com/dfw/celina/the-parks-at-wilson-creek/829-108", single=True)
    assert len(h) == 1 and h[0]["address"] == "2616 Manzanar Mews" and h[0]["price"] == 562926, h
    plan = '<html><head><title>New Home Plan Picasso in Sherman, TX 75092</title></head><body><p>$352,990</p></body></html>'
    assert extract_homes(plan, "https://www.highlandhomes.com/dfw/sherman/heritage-ranch/831-PICA-AM", single=True) == []


def test_card_pins_matched_by_listing_id():
    from tracker.extract import card_homes
    html = '''<div class="card spec-card" data-card-listing-id="1769117"><div class="row">
        <a href="/new-homes/tx/aledo/parks/cypress/131-kingfisher-lane">131 Kingfisher Lane</a>
        <p>Aledo, TX 76008</p><p class="price">$469,990</p><p>3 Beds 2,034 Sq Ft</p></div></div>
      <div class="card spec-card" data-card-listing-id="1769118"><div class="row">
        <a href="/new-homes/tx/aledo/parks/cypress/137-kingfisher-lane">137 Kingfisher Lane</a>
        <p>Aledo, TX 76008</p><p class="price">$539,990</p><p>4 Beds 2,454 Sq Ft</p></div></div>
      <div class="oi-map-item" data-listing-id="1769118" data-latitude="32.702917" data-longitude="-97.623828"></div>
      <div class="oi-map-item" data-listing-id="1769117" data-latitude="32.702953" data-longitude="-97.623091"></div>
      <div class="office" data-id="hq" data-latitude="40.0" data-longitude="-75.0"></div>'''
    c = {h["address"]: (h["lat"], h["lng"]) for h in card_homes(html, "https://www.bloomfieldhomes.com/x", B["detail_re"])}
    assert c == {"131 Kingfisher Lane": (32.702953, -97.623091), "137 Kingfisher Lane": (32.702917, -97.623828)}


def test_geocode_census_then_zip_fallback(tmp_path, monkeypatch):
    from tracker import geocode
    zf = tmp_path / "zips.csv"
    zf.write_text("zip,lat,lng\n75009,33.33,-96.78\n")
    monkeypatch.setattr(geocode, "ZIP_FILE", zf)

    class FakeSession:
        posts = 0
        def post(self, url, data, files, timeout):
            FakeSession.posts += 1
            class R:
                text = ('"a","1404 Amber Way, Celina, TX, 75009","Match","Exact","1404 AMBER WAY","-96.7801,33.3101","1","L"\n'
                        '"b","9 New Street, Celina, TX, 75009","No_Match"\n')
                def raise_for_status(self): pass
            return R()
    homes = {"a": {"active": True, "address": "1404 Amber Way", "city": "Celina", "zip": "75009"},
             "b": {"active": True, "address": "9 New Street", "city": "Celina", "zip": "75009"},
             "c": {"active": True, "address": "1 Pin Lane", "city": "Celina", "zip": "75009",
                   "lat": 33.3, "lng": -96.7, "location_precision": "builder"}}
    now = R.now_local()
    out = geocode.fill_missing(homes, now, R.FMT, "test", session=FakeSession())
    assert homes["a"]["location_precision"] == "address" and (homes["a"]["lat"], homes["a"]["lng"]) == (33.3101, -96.7801)
    assert homes["b"]["location_precision"] == "zip" and homes["b"]["lat"] == 33.33
    assert homes["c"]["location_precision"] == "builder"          # builder pins are never overwritten
    assert out == {"census_tried": 2, "census_matched": 1, "community": 0, "zip": 1, "city": 0}
    geocode.fill_missing(homes, now + timedelta(hours=1), R.FMT, "test", session=FakeSession())
    assert FakeSession.posts == 1    # the unmatched street isn't retried for 7 days


def test_community_center_beats_zip(tmp_path, monkeypatch):
    from tracker import geocode
    zf = tmp_path / "zips.csv"; zf.write_text("zip,lat,lng\n75009,33.33,-96.78\n")
    monkeypatch.setattr(geocode, "ZIP_FILE", zf)
    monkeypatch.setattr(geocode, "census_batch", lambda rows, session: {})
    homes = {f"p{i}": {"active": True, "builder": "B", "community": "Mosaic", "city": "Celina", "zip": "75009",
                       "address": f"{i} A St", "lat": 33.301 + i / 1000, "lng": -96.80, "location_precision": "builder"}
             for i in range(3)}
    homes["x"] = {"active": True, "builder": "B", "community": "Mosaic", "city": "Celina", "zip": "75009", "address": "9 B St"}
    homes["y"] = {"active": True, "builder": "B", "community": "Elsewhere", "city": "Celina", "zip": "75009", "address": "8 C St"}
    geocode.fill_missing(homes, R.now_local(), R.FMT, "t", session=object())
    assert homes["x"]["location_precision"] == "community" and homes["x"]["lat"] == 33.302
    assert homes["y"]["location_precision"] == "zip"
