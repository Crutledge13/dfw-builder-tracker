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


def test_homes_in_other_texas_metros_are_not_tracked():
    """Coventry/Pacesetter/Shaddock also build in Austin, San Antonio and Houston; a builder's DFW flag or URL
    filter must not let those in, and a plan named after a DFW town isn't the home's city."""
    st = LocalStore(tempfile.mkdtemp()).load()
    rec = R.Recorder(st, R.now_local())
    b = dict(B, slug="coventry", name="Coventry Homes", dfw_only=True, url_parts={"community": -3, "plan": -2},
             filter_re=re.compile(r"/new-homes/tx/"))
    sa = {"address": "8332 Rocket View", "zip": "78263", "price": 334990,
          "url": "https://www.coventryhomes.com/new-homes/tx/san-antonio/garden-grove-50/young/8332-rocket-view"}
    plan_town = {"address": "2216 Crimson Sunset Drive", "zip": "78628", "price": 499990,
                 "url": "https://www.coventryhomes.com/new-homes/tx/georgetown/wolf-ranch/kaufman/2216-crimson-sunset-drive"}
    dfw = {"address": "1000 Goldenwave Trail", "zip": "75098", "price": 459990,
           "url": "https://www.coventryhomes.com/new-homes/tx/wylie/dominion-50/kennedale/1000-goldenwave-trail"}
    for o in (sa, plan_town, dfw):
        rec.observe(b, dict(o))
    assert list(st.homes) == ["coventry:1000-goldenwave-trl"]
    assert st.homes["coventry:1000-goldenwave-trl"]["city"] == "Wylie"   # not the 'Kennedale' plan
    # a DFW town on the page wins over a bogus ZIP
    rec.observe(b, {"address": "1002 Perrin Lane", "city": "Van Alstyne", "zip": "79495", "price": 300000})
    assert "coventry:1002-perrin-ln" in st.homes
    from tracker.extract import city_from_url
    assert city_from_url("https://x/new-homes/tx/heartland/heartland/howard/123-oak-lane", [-3, -2]) == "Heartland"


def test_status_rewording_is_not_a_status_change():
    st = LocalStore(tempfile.mkdtemp()).load()
    t0 = R.now_local()
    rec = R.Recorder(st, t0)
    h = {"address": "2709 Grapevine Way", "city": "Celina", "zip": "75009", "price": 400000,
         "url": "https://tb.example/2709-grapevine-way", "from_detail": True}
    rec.observe(B, dict(h, status="move-in ready"))
    rec = R.Recorder(st, t0 + timedelta(hours=1))
    rec.observe(B, dict(h, status="Ready Now"))
    assert [c["event"] for c in rec.changes] == []
    rec = R.Recorder(st, t0 + timedelta(hours=2))
    rec.observe(B, dict(h, status="Under Construction"))
    assert [c["event"] for c in rec.changes] == ["status_change"]


def test_fine_print_is_not_an_offer():
    from tracker.extract import incentive_snippets
    text = ("Zero Down & Rates as Low as 3.99% | Restrictions apply; promotional 5/1 ARM rate offer and Flex Cash "
            "incentives available on select inventory when financing with preferred lender | Zero Down Available")
    assert incentive_snippets(text) == ["Zero Down & Rates as Low as 3.99%", "Zero Down Available"]
    fp = ("<p>Builder forward commitments (BFCs), special fixed interest rates and flex cash offerings are limited "
          "and may be discontinued without prior notice.</p><p>Get $10,000 in flex cash on select homes.</p>")
    assert [o["text"] for o in extract_offers(fp)] == ["Get $10,000 in flex cash on select homes"]


def test_seed_network_error_is_retried_once():
    class Flaky(FakeClient):
        def get(self, url):
            if url.rstrip("/").endswith("/qmi") and not getattr(self, "failed", False):
                self.failed = True
                self.requests_made += 1
                return Response(url, 0, "")
            return super().get(url)
    st = LocalStore(tempfile.mkdtemp()).load()
    rec = R.Recorder(st, R.now_local())
    pages = {"https://tb.example/qmi": listing_page([("1500 Elm Street", 489990, 2450, "Seaberry")])}
    _, status, note = R.crawl_builder(B, Flaky(pages), rec, budget=20)
    assert status == "ok" and "seed" not in note and len(st.homes) == 1


def test_nextjs_flight_stream_homes():
    """Beazer community pages stream homes in self.__next_f chunks: rows split across scripts, a text (T) row
    with no newline after it, homes nested ~20 levels deep, an Availability date, and plan elevations."""
    home = {"__typename": "HomeDetailPage", "Url": "https://www.beazer.com/dallas-TX/denton/stark-farms/berkshire/0415",
            "StreetAddress": "2300 Briarbrook Drive", "City": "Denton", "StateID": "TX", "Zip": "76207",
            "Beds": 4, "Baths": 2.5, "SquareFoot": 2323, "Availability": "2026-10-22T04:00:00Z", "HomePrice": 399990,
            "Elevation": {"Expanded": {"Price": 415990, "SquareFoot": 2323, "Beds": 4, "Baths": 2.5}}}
    nested = home
    for _ in range(9):     # ~20 levels, as on the real page
        nested = {"children": [nested]}
    text = "<p>Your contract\nwill define what is included.</p>"
    stream = (f'1:HL["/_next/static/css/a.css","style"]\n'
              f'2:T{len(text.encode()):x},{text}'
              f'3:["$","section",null,{json.dumps(nested)}]\n')
    half = len(stream) // 2
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>"
                      for part in (stream[:half], stream[half:]))
    html = f"<html><body><script>(self.__next_f=self.__next_f||[]).push([0])</script>{scripts}</body></html>"
    homes = [h for h in extract_homes(html, "https://www.beazer.com/dallas-TX/denton/stark-farms") if h["address"]]
    assert len(homes) == 1
    h = homes[0]
    assert (h["address"], h["price"], h["sqft"], h["zip"]) == ("2300 Briarbrook Drive", 399990, 2323, "76207")
    assert h["status"] == "" and h["move_in"] == "2026-10-22"
    b = dict(B, slug="beazer", name="Beazer Homes", dfw_only=False, url_parts={"community": -3, "plan": -2},
             detail_re=re.compile(r"/dallas-tx/[^/]+/[^/]+/[^/]+/\d{3,5}/?$", re.I))
    st = LocalStore(tempfile.mkdtemp()).load()
    rec = R.Recorder(st, R.now_local())
    pages = {"https://tb.example/qmi": html}
    R.crawl_builder(b, FakeClient(pages), rec, budget=5)
    rec_h = st.homes["beazer:2300-briarbrook-dr"]
    assert (rec_h["community"], rec_h["plan"], rec_h["price"]) == ("Stark Farms", "Berkshire", 399990)
    assert len(st.homes) == 1   # plan elevations are not homes


def test_town_and_numbered_street_in_home_slug():
    from tracker.extract import address_from_url, city_from_url
    u = "https://www.ashlynhomes.com/house/114-red-bud-road-boyd-tx-76023/"
    assert (address_from_url(u), city_from_url(u)) == ("114 Red Bud Road", "Boyd")
    u = "https://www.ashlynhomes.com/house/3221-nw-25th-street-fort-worth-tx-76106/"
    assert (address_from_url(u), city_from_url(u)) == ("3221 NW 25th Street", "Fort Worth")
    u = "https://www.ashlynhomes.com/house/140-prairie-winds-drive-poolville-texas-76487"
    assert (address_from_url(u), city_from_url(u)) == ("140 Prairie Winds Drive", "Poolville")


def test_card_totals_match_example_listing():
    """2202 Redding Drive: 10 cuts totaling $108k since first tracked, builder shows $777,670 -> $133k incentive."""
    from types import SimpleNamespace
    from tracker.export import export_json
    tmp = tempfile.mkdtemp()
    try:
        hid = "bloomfield:2202-redding-dr"
        prices = [752990, 742990, 732990, 722990, 712990, 702990, 692990, 682990, 672990, 662990, 644990]
        changes = [{"at": "2026-09-04 08:00", "event": "new_home", "home_id": hid, "new": prices[0]}]
        changes += [{"at": f"2026-10-{i + 1:02d} 08:00", "event": "price_cut", "home_id": hid, "old": a, "new": b,
                     "url": "u"} for i, (a, b) in enumerate(zip(prices, prices[1:]))]
        home = {"home_id": hid, "active": True, "builder": "Bloomfield Homes", "address": "2202 Redding Drive",
                "community": "Somerset Classic 70s & 80s", "plan": "Magnolia II", "beds": 4, "baths": 3.5,
                "sqft": 3430, "price": 644990, "first_price": 752990, "builder_was_price": 777670,
                "status": "Move-In Ready", "last_seen": "2026-10-19 08:19", "url": "u"}
        st = SimpleNamespace(homes={hid: home}, changes=changes, incentives={})
        export_json(__import__("pathlib").Path(tmp), st, {}, "2026-10-19 08:19")
        h = json.load(open(f"{tmp}/api/homes.json"))[0]
        assert (h["total_drop"], h["cuts"], h["largest_cut"], h["builder_discount"]) == (108000, 10, 18000, 132680)
        assert h["card"] == {"price_drop": "Down $108k since first tracked", "cuts": "10 cuts",
                             "savings": "$133k incentive", "move_in": "Ready now", "beds": "4 bd",
                             "baths": "3.5 ba", "sqft": "3,430 sq ft"}
        assert h["checked_at"] == "2026-10-19T08:19:00-05:00"
    finally:
        shutil.rmtree(tmp)


def test_move_in_and_offer_value():
    from tracker.export import move_in_label as m, incentive_value as v
    assert [m("Move-in Ready", None), m("Under Contract", None), m("Under Construction", "Est. Dec 2026"),
            m("Active", None), m("under construction", None)] == \
           ["Ready now", "Pending", "Ready Dec 2026", "Available", "Under construction"]
    assert v("4% Towards Closing Costs on All Specs!", 500000) == 20000
    assert v("+ Up to $15K towards Closing Costs/Rate Buydown | $25K towards Options", 600000) == 40000
    assert v("Zero Down & Rates as Low as 3.99%", 400000) is None
    assert v("Up to $10,000 in Flex Cash | Up to $10,000 in Flex Cash available on select homes", 1) == 10000


def test_markdown_label_not_double_counted():
    from tracker.export import add_card
    h = {"price": 347990, "first_price": 399990, "builder_was_price": 383990,
         "offer": {"text": "$35K off | Up to $10,000 toward closing costs"}}
    add_card(h, {})
    assert h["builder_discount"] == 36000 and h["offer"]["value"] == 10000 and h["total_savings"] == 46000
    h2 = {"price": 347990, "first_price": 347990, "offer": {"text": "$35K off"}}
    add_card(h2, {})
    assert h2["total_savings"] == 35000      # no was-price shown: the label is the only record of the markdown


def test_reworded_offer_is_not_a_change():
    from tracker.run import offer_terms
    a = "Program offers up to $1,000 in closing costs (available with the use of any lender) or up to $1,00"
    b = "Program offers up to $1,000 in closing costs (available with the use of any lender) or up to $1,000 in design"
    assert offer_terms(a) != offer_terms("Up to $2,000 in closing costs")
    assert offer_terms("2/1 Buydown, 3.25% first year") == offer_terms("Lower payments with a 2/1 buydown: 3.25% in year one")
    assert offer_terms("2/1 Buydown, 3.25% first year") != offer_terms("2/1 Buydown, 2.99% first year")
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp); t0 = R.now_local()
        seed = {"https://tb.example/qmi": listing_page([])}     # links to the detail page
        run_once(store, dict(seed, **{DETAIL: detail_page(424990)}), t0)
        st, _ = run_once(store, dict(seed, **{DETAIL: detail_page(424990)}), t0 + timedelta(hours=1))
        assert "closing costs" in next(iter(st.homes.values()))["incentive_text"]
        reworded = detail_page(424990).replace("Get up to $15,000 toward closing costs on this home.",
                                               "Receive up to $15,000 toward your closing costs!")
        _, ch = run_once(store, dict(seed, **{DETAIL: reworded}), t0 + timedelta(hours=2))
        assert not [c for c in ch if c["event"].startswith("home_offer")], ch
        changed = reworded.replace("$15,000", "$20,000")
        _, ch = run_once(store, dict(seed, **{DETAIL: changed}), t0 + timedelta(hours=3))
        assert [c["event"] for c in ch if c["event"].startswith("home_offer")] == ["home_offer_changed"]
    finally:
        shutil.rmtree(tmp)


def test_similar_homes_section_is_not_this_home():
    """2032 Naples Drive (Beazer): $474,038; the $349,905 below 'also for sale' belongs to 2116 Naples Drive."""
    html = '''<html><head><title>2032 Naples Drive</title></head><body><main>
      <h1>2032 Naples Drive</h1><p>Rowlett, TX 75088</p><span class="price">$474,038</span>
      <p>3 Bedrooms 3.5 Bathrooms 2,167 Sq Ft</p>
      <h2>See these other plans also for sale in Bayside</h2>
      <div><span>Price Was $454,832!*</span> Caspian Home <span>$349,905</span> 2116 Naples Drive Rowlett, TX</div>
      </main></body></html>'''
    h = extract_homes(html, "https://www.beazer.com/dallas-TX/rowlett/bayside/caspian/0312", single=True)[0]
    assert (h["price"], h["builder_was_price"]) == (474038, None)
    # Riverside: own price, then a neighbor's price further down the page (not adjacent)
    html2 = '''<html><body><main><div class="hero"><h1>508 Mitchell Blvd</h1><p>Weatherford, Texas 76087</p>
      <p>Priced at <span class="price">$494,015</span></p></div>
      <section><div class="card"><a href="/quick-move-in/524-mitchell-blvd">524 Mitchell Blvd</a>
      <p>Weatherford, Texas 76087</p><p>Priced at <span class="price">$468,260</span></p></div></section></main></body></html>'''
    h2 = extract_homes(html2, "https://www.riversidehomebuilders.com/quick-move-in/508-mitchell-blvd", single=True)[0]
    assert (h2["price"], h2["builder_was_price"]) == (494015, None)


def test_redirect_to_community_page_means_gone():
    from tracker.extract import page_is_elsewhere as gone
    home = "https://www.drhorton.com/texas/dallas/crandall/cartwright-ranch/qmis/1223-barkridge-st"
    community = '<html><head><link rel="canonical" href="https://www.drhorton.com/texas/dallas/crandall"></head>' \
                '<body><select><option>$150,000</option></select></body></html>'
    assert gone(home, home, community)
    assert gone(home, "https://www.drhorton.com/texas/dallas/crandall/cartwright-ranch", "<html></html>")
    dw = "https://www.davidweekleyhomes.com/homes-ready-soon/tx/dallas-ft-worth/arlington/elements/19420035"
    assert gone(dw, dw, '<link rel="canonical" href="https://www.davidweekleyhomes.com/neighborhoods/tx/dallas-ft-worth/elements-at-viridian">')
    ok = "https://www.riversidehomebuilders.com/quick-move-in/508-mitchell-blvd"
    assert not gone(ok, ok, '<link rel="canonical" href="https://www.riversidehomebuilders.com/quick-move-in/508-mitchell-blvd/">')
    hl = "https://www.highlandhomes.com/dfw/aubrey/sandbrock-ranch/172-170"
    assert not gone(hl, hl, '<link rel="canonical" href="https://highlandhomes.com/dfw/aubrey/sandbrock-ranch/172-170">')


def test_gone_home_and_quiet_correction():
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp); t0 = R.now_local()
        seed = {"https://tb.example/qmi": listing_page([])}
        st, _ = run_once(store, dict(seed, **{DETAIL: detail_page(424990)}), t0)
        h = next(iter(st.homes.values()))
        assert h["price_version"] == R.EXTRACT_VERSION
        # an old, wrongly read price from before the fix gets corrected quietly on the next read
        h.update(price=349905, price_version=1)
        store.save(st, [], [], {})
        st, ch = run_once(store, dict(seed, **{DETAIL: detail_page(424990)}), t0 + timedelta(hours=1))
        h = next(iter(st.homes.values()))
        assert ch == [] and h["price"] == 424990 and h["first_price"] == 424990
        # the home's page now redirects to the community page -> removed, not a $150,000 price
        community = '<html><head><link rel="canonical" href="https://tb.example/new-homes/tx/celina/mosaic"></head>' \
                    '<body><p>Budget $150,000</p></body></html>'
        st, ch = run_once(store, dict(seed, **{DETAIL: community}), t0 + timedelta(hours=2))
        assert ch == [] and next(iter(st.homes.values()))["active"]       # first redirect: confirm later
        st, ch = run_once(store, dict(seed, **{DETAIL: community}), t0 + timedelta(hours=3))
        assert [c["event"] for c in ch] == ["removed"] and next(iter(st.homes.values()))["price"] == 424990
    finally:
        shutil.rmtree(tmp)


def test_brief_redirect_while_repricing_is_not_a_sale():
    """Trophy Signature: five live homes' pages showed the community page for one run (during a repricing),
    were logged as sold, then 'relisted' three hours later. One redirect alone, or a redirect while a listing
    still shows the home, must not record a sale."""
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp); t0 = R.now_local()
        seed = {"https://tb.example/qmi": listing_page([])}
        community = '<html><head><link rel="canonical" href="https://tb.example/new-homes/tx/celina/mosaic"></head>' \
                    '<body><p>From $299,990</p></body></html>'
        run_once(store, dict(seed, **{DETAIL: detail_page(424990)}), t0)
        st, ch = run_once(store, dict(seed, **{DETAIL: community}), t0 + timedelta(hours=1))
        h = next(iter(st.homes.values()))
        assert ch == [] and h["active"] and h["gone_pending"]
        st, ch = run_once(store, dict(seed, **{DETAIL: detail_page(419990)}), t0 + timedelta(hours=2))
        h = next(iter(st.homes.values()))
        assert [c["event"] for c in ch] == ["price_cut"] and h["active"] and not h["gone_pending"]
        # a listing card shows the home in the same run its page redirects: still for sale, nothing pending
        listed = {"https://tb.example/qmi": listing_page([("1404 Amber Way", 419990, 2104, "Oak")])}
        for k in (3, 4):
            st, ch = run_once(store, dict(listed, **{DETAIL: community}), t0 + timedelta(hours=k))
            h = next(iter(st.homes.values()))
            assert ch == [] and h["active"] and not h["gone_pending"]
    finally:
        shutil.rmtree(tmp)


def test_this_week_sections():
    from types import SimpleNamespace
    from tracker.export import this_week
    run_at = "2026-10-26 10:00"
    homes = {"a": {"id": "a", "builder": "B", "address": "1 A St", "city": "Celina", "community": "Mosaic", "price": 380000,
                   "total_drop": 20000, "cuts": 2, "move_in_label": "Ready Sep 2026", "move_in": "Est. Sep 2026", "url": "ua"},
             "n": {"id": "n", "builder": "B", "address": "2 B St", "city": "Celina", "community": "Mosaic", "price": 300000,
                   "move_in_label": "Under construction", "url": "un"}}
    ch = [{"at": "2026-10-18 09:00", "event": "new_home", "builder": "B", "home_id": "a", "new": 400000},   # baseline
          {"at": "2026-10-10 09:00", "event": "price_cut", "builder": "B", "home_id": "a", "old": 410000, "new": 400000},  # too old
          {"at": "2026-10-21 09:00", "event": "price_cut", "builder": "B", "home_id": "a", "old": 400000, "new": 390000,
           "community": "Mosaic", "city": "Celina", "address": "1 A St", "url": "ua"},
          {"at": "2026-10-24 09:00", "event": "price_cut", "builder": "B", "home_id": "a", "old": 390000, "new": 380000,
           "community": "Mosaic", "city": "Celina", "address": "1 A St", "url": "ua"},
          {"at": "2026-10-23 09:00", "event": "new_home", "builder": "B", "home_id": "n", "new": 300000,
           "community": "Mosaic", "city": "Celina", "url": "un"},
          {"at": "2026-10-25 09:00", "event": "removed", "builder": "B", "home_id": "g", "old": 450000,
           "community": "Mosaic", "city": "Celina", "address": "3 C St", "url": "ug"}]
    st = SimpleNamespace(changes=ch, homes={"g": {"home_id": "g", "first_seen": "2026-10-18 09:00", "first_price": 470000}},
                         incentives={})
    w = this_week(st, homes, run_at)
    assert (w["stats"]["price_cuts"], w["stats"]["total_cut"], w["stats"]["new_homes"], w["stats"]["sold_removed"]) == (2, 20000, 1, 1)
    assert [r["drop"] for r in w["biggest_cuts"]] == [10000, 10000]
    assert w["new_homes"][0]["address"] == "2 B St"
    assert w["sold_removed"][0]["total_drop"] == 20000 and w["sold_removed"][0]["days_tracked"] == 7
    assert w["past_completion"][0]["address"] == "1 A St"
    c = w["most_active_communities"][0]
    assert (c["community"], c["changes"], c["price_cuts"], c["total_cut"], c["new_homes"], c["sold_removed"]) == ("Mosaic", 4, 2, 20000, 1, 1)


def test_redirect_to_community_under_other_path_means_gone():
    """David Weekley: a sold home's URL shows its community page, whose canonical is /new-homes/.../tavolo-park-50
    (a path with a number, beside the home's path rather than above it). Its 'From $505,990' is not a home price."""
    from tracker.extract import page_is_elsewhere as gone
    dw = "https://www.davidweekleyhomes.com/homes-ready-soon/tx/dallas-ft-worth/fort-worth/tavolo-park-50/19000126"
    assert gone(dw, dw, '<link rel="canonical" href="https://www.davidweekleyhomes.com/new-homes/tx/dallas-ft-worth/'
                        'fort-worth/tavolo-park-50" />')
    live = dw.replace("19000126", "19000193")
    assert not gone(live, live, f'<link rel="canonical" href="{live}" />')
    bl = "https://www.bloomfieldhomes.com/new-homes/tx/godley/star-ranch-classic-50/cypress/921-ridgehurst-drive/"
    assert not gone(bl, bl, f'<link rel="canonical" href="{bl}">')


def test_nested_address_line1_and_whole_number_sqft():
    """David Weekley community page: homes as JSON with Address.Line1; its home page says 'From $509,762 Sq Ft 2074'."""
    rec = {"Address": {"Line1": "7852 Pondview Lane", "City": "Fort Worth", "StateAbbreviation": "TX",
                       "ZipCode": "76123"}, "ReadyLabel": "Ready Now", "Price": 509762, "SquareFootage": 2074,
           "Bedrooms": 3, "FullBaths": 2,
           "Url": "/homes-ready-soon/tx/dallas-ft-worth/fort-worth/tavolo-park-50/19000193"}
    html = f'<html><body><script type="application/json">{json.dumps({"homes": [rec]})}</script></body></html>'
    h = extract_homes(html, "https://www.davidweekleyhomes.com/new-homes/tx/dallas-ft-worth/fort-worth/tavolo-park-50")[0]
    assert (h["address"], h["city"], h["zip"], h["price"], h["sqft"]) == \
        ("7852 Pondview Lane", "Fort Worth", "76123", 509762, 2074)
    page = '''<html><head><title>New Home Ready Soon in Fort Worth, TX</title></head><body><main>
      <div>Plan #T002</div><h2>The Tulip</h2><div class="pricing"><div class="att-label">From</div>
      <div class="att-value">$509,762</div></div><div class="square-footage"><div class="att-label">Sq Ft</div>
      <div class="att-value">2074</div></div><p>3 Bedrooms</p>
      </main></body></html>'''
    d = extract_homes(page, "https://www.davidweekleyhomes.com" + rec["Url"], single=True)[0]
    assert (d["price"], d["sqft"]) == (509762, 2074)


def test_home_page_without_address_joins_its_listing_card():
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp); st = store.load(); rec = R.Recorder(st, R.now_local())
        url = "https://tb.example/homes-ready-soon/celina/mosaic/19000193"
        rec.observe(B, {"address": "7852 Pondview Lane", "price": 509762, "url": url, "city": "Celina", "source": "json"})
        rec.observe(B, {"address": "", "price": 509762, "url": url, "city": "Celina", "from_detail": True, "sqft": 2074})
        assert len(st.homes) == 1 and next(iter(st.homes.values()))["sqft"] in (2074, None)
    finally:
        shutil.rmtree(tmp)


def test_unseen_home_waits_for_its_own_page_before_removal():
    """Listing pages are read in rotation and runs can be hours apart: a home not on a listing for 48h is removed
    only once its own page was re-checked and no longer shows it (or after a week)."""
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp); t0 = R.now_local()
        seed = {"https://tb.example/qmi": listing_page([])}
        st, _ = run_once(store, dict(seed, **{DETAIL: detail_page(424990)}), t0)
        fr = st.frontier[DETAIL] if DETAIL in st.frontier else next(f for k, f in st.frontier.items()
                                                                       if k.rstrip("/") == DETAIL)
        fr["last_checked"] = R.ts(t0 + timedelta(days=30))     # pretend the page check is not due yet
        store.save(st, [], [], {})
        # 3 days later the home's page wasn't fetched (budget): still active
        rec = R.Recorder(st, t0 + timedelta(days=3)); rec.retire_unseen(B)
        assert rec.changes == [] and next(iter(st.homes.values()))["active"]
        # its page was checked and no longer shows a home -> removed
        fr["misses"] = 1
        rec = R.Recorder(st, t0 + timedelta(days=3)); rec.retire_unseen(B)
        assert [c["event"] for c in rec.changes] == ["removed"]
        # a week unseen -> removed even without a page check
        h = next(iter(st.homes.values())); h["active"] = True; fr["misses"] = 0
        rec = R.Recorder(st, t0 + timedelta(days=8)); rec.retire_unseen(B)
        assert [c["event"] for c in rec.changes] == ["removed"]
    finally:
        shutil.rmtree(tmp)


def test_homes_found_after_coverage_repair_are_not_new():
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp); t0 = R.now_local()
        b = dict(B, baseline_until=R.ts(t0 + timedelta(hours=24.5)))
        st = store.load(); rec = R.Recorder(st, t0)
        rec.observe(b, {"address": "7852 Pondview Lane", "price": 509762, "city": "Celina", "source": "json"})
        rec2 = R.Recorder(st, t0 + timedelta(hours=25))
        rec2.observe(b, {"address": "7856 Pondview Lane", "price": 519762, "city": "Celina", "source": "json"})
        allc = rec.changes + rec2.changes
        assert [R.is_baseline(c) for c in allc] == [True, False]
        # the builder's own first day was long before (its first-day baseline doesn't apply here)
        allc = [{"at": R.ts(t0 - timedelta(days=5)), "builder": B["name"], "event": "new_home", "home_id": "x",
                 "address": "1 Old St", "new": 300000}] + allc
        st.changes = []
        tabs = R.derived_tabs(st, allc, t0 + timedelta(days=2), [], [b])
        new = tabs.get("new_homes") or tabs.get("New Homes") or next(v for k, v in tabs.items() if "new" in k.lower())
        assert sorted(r["address"] for r in new) == ["7856 Pondview Lane"]
    finally:
        shutil.rmtree(tmp)


def test_quietly_dropped_home_returns_quietly():
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp); st = store.load(); t0 = R.now_local()
        url = "https://tb.example/homes-ready-soon/celina/mosaic/19160142"
        st.homes["test:x"] = {"home_id": "test:x", "builder": B["name"], "url": url, "price": 495990,
                              "first_price": 495990, "active": False, "gone_unverified": True, "price_source": 3,
                              "price_version": 2, "first_seen": R.ts(t0 - timedelta(days=2))}
        rec = R.Recorder(st, t0)
        rec.observe(B, {"address": "1501 Venture Drive", "price": 499000, "url": url, "city": "Celina", "source": "json"})
        h = st.homes["test:x"]
        assert rec.changes == [] and h["active"] and (h["price"], h["first_price"]) == (499000, 499000)
    finally:
        shutil.rmtree(tmp)


def test_county_road_address_from_url():
    """Riverside: '/quick-move-in/8685-cr-623' homes had no address at all."""
    from tracker.extract import address_from_url
    assert address_from_url("https://www.riversidehomebuilders.com/quick-move-in/8685-cr-623") == "8685 CR 623"
    assert address_from_url("https://www.riversidehomebuilders.com/quick-move-in/1536-cr-4910/") == "1536 CR 4910"
    assert address_from_url("https://x.example/homes/12-fm-1385") == "12 FM 1385"
    # a sold CR home's page lists only 'Similar Homes Nearby': none of their prices is this home's
    page = '''<html><head><title>8685 CR 623 - Riverside Home Builders</title></head><body><main>
      <h1>8685 CR 623</h1><p>Blue Ridge, Texas 75424</p><h2>Similar Homes Nearby</h2>
      <div class="card"><p>Priced at $603,591</p><a href="/quick-move-in/8473-cr-623">8473 CR 623</a></div>
      <div class="card"><p>Priced at $555,200</p><a href="/quick-move-in/8489-cr-623">8489 CR 623</a></div>
      </main></body></html>'''
    found = extract_homes(page, "https://www.riversidehomebuilders.com/quick-move-in/8685-cr-623", single=True)
    assert all(h.get("price") not in (603591, 555200) for h in found)


def test_unconfirmed_old_price_is_not_a_sold_record():
    """Riverside: 11 homes carried $468,260 (501 Mitchell Blvd's price, read from a 'similar homes' strip by the
    old reader) and were logged as sold at that price. A home whose own page was only read by an older reader and
    never confirmed is dropped quietly; a home only ever seen on listings is still removed with a record."""
    tmp = tempfile.mkdtemp()
    try:
        store = LocalStore(tmp); t0 = R.now_local()
        seed = {"https://tb.example/qmi": listing_page([("1500 Elm Street", 489990, 2450, "Seaberry")])}
        st, _ = run_once(store, dict(seed, **{DETAIL: detail_page(424990)}), t0)
        amber = next(h for h in st.homes.values() if h["address"] == "1404 Amber Way")
        amber.update(price=468260, price_version=None, page_read=True)    # read before the reader fix
        store.save(st, [], [], {})
        sold = '<html><head><title>1404 Amber Way</title></head><body><main><h1>1404 Amber Way</h1>' \
               '<p>This home has been sold.</p></main></body></html>'
        empty = {"https://tb.example/qmi": listing_page([])}
        st, ch = run_once(store, dict(empty, **{DETAIL: sold}), t0 + timedelta(hours=73))
        assert [c["address"] for c in ch if c["event"] == "removed"] == ["1500 Elm Street"]
        amber = next(h for h in st.homes.values() if h["address"] == "1404 Amber Way")
        assert not amber["active"] and amber["gone_unverified"]
    finally:
        shutil.rmtree(tmp)
