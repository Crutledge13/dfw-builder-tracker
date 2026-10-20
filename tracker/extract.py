"""Turn a builder page into home records.

Builder sites rarely share a format, so extraction runs three passes and keeps
whatever produces home-like records:

1. schema.org JSON-LD (<script type="application/ld+json">)
2. Any JSON embedded in the page (Next.js __NEXT_DATA__, window.__STATE__ = {...},
   data-* attributes). Every dict is checked for "looks like a home" (a plausible
   price plus an address or beds/sqft). Listing pages often carry every home here.
3. Visible-HTML fallback for a single home page (price, beds, baths, sqft, address),
   ignoring struck-through "was" prices, which are kept separately.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

PRICE_MIN, PRICE_MAX = 60_000, 8_000_000

ALIASES = {
    "price": ["price", "listprice", "salesprice", "saleprice", "currentprice", "homeprice", "finalprice",
              "displayprice", "pricevalue", "netprice", "specprice", "inventoryprice", "lowprice", "startingprice",
              "baseprice", "pricefrom"],
    "was_price": ["originalprice", "oldprice", "wasprice", "previousprice", "strikethroughprice", "priorprice",
                  "regularprice", "pricebeforereduction"],
    "beds": ["beds", "bedrooms", "bedroom", "numbeds", "numberofbedrooms", "bedroomcount", "bedsmax", "maxbeds",
             "numberofrooms"],
    "baths": ["baths", "bathrooms", "totalbaths", "numberofbathroomstotal", "bathroomcount", "bathsmax", "maxbaths",
              "fullbaths"],
    "half_baths": ["halfbaths", "numberofhalfbaths", "halfbathrooms"],
    "sqft": ["sqft", "squarefeet", "squarefoot", "squarefootage", "livingarea", "floorsize", "homesize", "sqftmax", "maxsqft",
             "totalsqft", "size", "area"],
    "address": ["streetaddress", "address1", "addressline1", "street", "homeaddress", "lotaddress", "address",
                "displayaddress", "fulladdress"],
    "city": ["city", "addresslocality", "cityname"],
    "state": ["state", "addressregion", "statecode"],
    "zip": ["zip", "zipcode", "postalcode", "postcode"],
    "community": ["community", "communityname", "subdivision", "subdivisionname", "neighborhood", "neighborhoodname"],
    "plan": ["plan", "planname", "floorplan", "floorplanname", "model", "modelname", "plantitle"],
    "status": ["status", "homestatus", "constructionstatus", "availability", "inventorystatus", "salesstatus"],
    "move_in": ["moveindate", "availabledate", "estimatedcompletion", "completiondate", "readydate",
                "estimatedmoveindate", "estcompletiondate", "availabilitydate"],
    "url": ["url", "detailurl", "pageurl", "permalink", "canonicalurl", "link", "href", "homeurl", "slug"],
    "lat": ["lat", "latitude", "geolat", "maplat", "homelat"],
    "lng": ["lng", "lon", "long", "longitude", "geolng", "geolon", "maplng", "maplon", "homelng"],
}
DFW_BOUNDS = (31.3, 34.6, -98.8, -95.3)   # lat min/max, lng min/max: anything outside is not a DFW home


def valid_coords(lat, lng):
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if DFW_BOUNDS[0] <= lat <= DFW_BOUNDS[1] and DFW_BOUNDS[2] <= lng <= DFW_BOUNDS[3]:
        return round(lat, 6), round(lng, 6)
    return None
_ALIAS_LOOKUP = {a: field for field, names in ALIASES.items() for a in names}

INCENTIVE_WORDS = re.compile(
    r"(closing cost|rate buy ?down|buy ?down|interest rate|as low as \d|\bapr\b|flex ?cash|incentive|"
    r"price reduc|reduced|savings|save \$|\$[\d,]+k? off|seller credit|credit toward|special financing|promo)",
    re.I)
STATUS_WORDS = re.compile(
    r"(move-?in ready|ready now|available now|under construction|coming soon|under contract|sold|pending|"
    r"ready (?:in )?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? ?\d{0,4})", re.I)
DFW_CITY_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(c) for c in "Allen|Anna|Argyle|Arlington|Aubrey|Azle|Balch Springs|Bedford|Benbrook|Burleson|Caddo Mills|Carrollton|Cedar Hill|Celina|Cleburne|Colleyville|Commerce|Coppell|Corinth|Crandall|Crowley|Dallas|Decatur|Denton|DeSoto|Duncanville|Ennis|Euless|Farmers Branch|Farmersville|Fate|Ferris|Flower Mound|Forney|Fort Worth|Frisco|Gainesville|Garland|Glenn Heights|Godley|Granbury|Grand Prairie|Grapevine|Greenville|Gunter|Haltom City|Haslet|Heath|Howe|Hurst|Irving|Josephine|Joshua|Justin|Kaufman|Keller|Kennedale|Krum|Lancaster|Lavon|Lewisville|Little Elm|Lucas|Mansfield|McKinney|McLendon-Chisholm|Melissa|Mesquite|Midlothian|Murphy|Nevada|New Fairview|Northlake|North Richland Hills|Oak Point|Ovilla|Parker|Pilot Point|Plano|Ponder|Princeton|Prosper|Providence Village|Red Oak|Rhome|Richardson|Roanoke|Rockwall|Rowlett|Royse City|Sachse|Saginaw|Sanger|Seagoville|Sherman|Southlake|Springtown|Sunnyvale|Terrell|Trenton|Van Alstyne|Venus|Watauga|Waxahachie|Weatherford|Westlake|Wylie|Aledo|Alvarado|Denison|Lantana|Tioga|Heartland|Poolville|Boyd|Paradise|Bridgeport|Peaster|Millsap|Brock|Tolar|Cresson|Newark|Azle|Springtown|Mineral Wells|Haslet|Godley|Rhome|Burleson|Joshua".split("|")), key=len, reverse=True)) + r")\b", re.I)
STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:(?:[NSEW]|NE|NW|SE|SW)\.?\s+)?(?:[A-Z][A-Za-z0-9'.]*|\d{1,3}(?:st|nd|rd|th))"
    r"(?:\s+(?:[A-Z][A-Za-z0-9'.]*|\d{1,3}(?:st|nd|rd|th))){0,4}\s+"
    r"(?:Street|St|Drive|Dr|Lane|Ln|Road|Rd|Court|Ct|Way|Trail|Trl|Avenue|Ave|Boulevard|Blvd|Circle|Cir|"
    r"Place|Pl|Parkway|Pkwy|Loop|Path|Run|Cove|Cv|Ridge|Bend|Pass|Point|Pt|Terrace|Ter|Row|Crossing|Xing|"
    r"Hollow|Glen|Grove|Meadow|Meadows|Park|View|Vista|Mews|Walk|Trace|Landing|Creek|Hill|Hills|Heights|Commons|Tr|Pass|Bnd|Gln)\b\.?")


# --------------------------------------------------------------------------- helpers
def norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def to_number(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("value", "amount", "price", "min", "max"):
            if k in v:
                return to_number(v[k])
        return None
    if isinstance(v, list):
        return to_number(v[0]) if v else None
    s = str(v).strip().lower()
    m = re.search(r"(\d[\d,]*\.?\d*)\s*(k|m)?\b", s)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    if m.group(2) == "k":
        n *= 1_000
    elif m.group(2) == "m":
        n *= 1_000_000
    return n


def to_price(v: Any) -> int | None:
    n = to_number(v)
    if n is None or not (PRICE_MIN <= n <= PRICE_MAX):
        return None
    return int(round(n))


def to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("name", "title", "label", "value", "text"):
            if k in v and isinstance(v[k], (str, int, float)):
                return str(v[k]).strip()
        return ""
    if isinstance(v, list):
        return to_text(v[0]) if v else ""
    return re.sub(r"\s+", " ", str(v)).strip()


def clean_address(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip(" ,|-")
    return s[:120]


SUFFIXES = {"street", "st", "drive", "dr", "lane", "ln", "road", "rd", "court", "ct", "way", "trail", "trl",
            "avenue", "ave", "boulevard", "blvd", "circle", "cir", "place", "pl", "parkway", "pkwy", "loop",
            "path", "run", "cove", "cv", "ridge", "bend", "pass", "point", "pt", "terrace", "ter", "row",
            "crossing", "xing", "hollow", "glen", "grove", "meadow", "meadows", "park", "view", "vista", "walk",
            "trace", "landing", "creek", "hill", "hills", "heights", "court", "commons", "alley", "spur", "mews",
            "tr", "ln", "cv", "gln", "holw", "hl", "lndg", "trce", "vw", "xing", "sq", "bnd", "ridge", "rdg"}


NOT_STREET = {"plan", "plans", "series", "collection", "homes", "home", "phase", "lot", "lots", "model", "floor",
              "floorplan", "elevation", "smart", "estates", "village", "classic", "classics", "garden"}
NON_HOME_RE = re.compile(r"/(floor-?plans?|plans?|series|collections?|models?|amenities|gallery|blog|news)(/|$)"
                         r"|-(plan|series|collection)/?$", re.I)


def address_from_url(url: str) -> str:
    """'.../lots/1124-433-landyn-street/' -> '433 Landyn Street' (only if it ends in a street suffix)."""
    seg = [x for x in urlparse(url).path.lower().split("/") if x]
    if not seg:
        return ""
    last = seg[-1]
    tail = re.search(r"-(?:tx|texas)(?:-\d{5})?$", last)
    if tail:  # "...-cross-timber-tr-rowlett-tx-75089": drop the city, state and zip
        words = last[:tail.start()].split("-")
        for k in (3, 2, 1):
            if len(words) > k + 1 and DFW_CITY_RE.fullmatch(" ".join(words[-k:])):
                words = words[:-k]
                break
        else:
            # unknown town: the street ends at its last street-type word ("114-red-bud-road-boyd")
            ends = [i for i, w in enumerate(words) if i >= 2 and w in SUFFIXES]
            if ends and ends[-1] < len(words) - 1:
                words = words[:ends[-1] + 1]
        last = "-".join(words)
    last = re.sub(r"-unit-[a-z0-9]+$", "", last)
    m = re.search(r"(?:^|-)(\d{1,6})-((?:(?:[a-z]+|\d{1,3}(?:st|nd|rd|th))-?){1,5})$", last)
    if not m:
        return ""
    words = m.group(2).strip("-").split("-")
    if words[-1] in NOT_STREET or any(w in NOT_STREET for w in words) or len(words[-1]) < 2:
        return ""
    if words[-1] not in SUFFIXES and len(words) < 2:
        return ""
    return m.group(1) + " " + " ".join(w.upper() if w in ("ne", "nw", "se", "sw", "n", "s", "e", "w") else
                                       w if re.match(r"\d", w) else w.capitalize() for w in words)


DFW_CITY_NAMES = {}


def canonical_city(name: str) -> str:
    """'MCKINNEY' / 'Mckinney' -> 'McKinney' (known DFW cities); other names are returned trimmed."""
    if not name:
        return ""
    if not DFW_CITY_NAMES:
        for c in DFW_CITY_RE.pattern.split("(", 1)[1].rsplit(")", 1)[0].split("|"):
            c = c.replace("\\", "")
            DFW_CITY_NAMES[c.lower()] = c
    n = re.sub(r"\s+", " ", name).strip()
    return DFW_CITY_NAMES.get(n.lower(), n)


def city_from_url(url: str, skip=()) -> str:
    """The most specific DFW city in the URL path: '/texas/dallas/crandall/…' -> 'Crandall'.
    `skip` = path positions counted back from the home (-1) that hold a community or plan name,
    which can be a town name (Coventry's 'Kennedale' plan in a Georgetown community)."""
    allsegs = [x for x in urlparse(url).path.split("/") if x]
    if allsegs:   # a home slug ending '-<town>-tx-<zip>'
        t = re.search(r"-(?:tx|texas)(?:-\d{5})?$", allsegs[-1].lower())
        if t:
            words = allsegs[-1].lower()[:t.start()].split("-")
            for k in (3, 2, 1):
                if len(words) > k + 1 and DFW_CITY_RE.fullmatch(" ".join(words[-k:])):
                    return canonical_city(" ".join(words[-k:]))
            ends = [i for i, w in enumerate(words) if i >= 2 and w in SUFFIXES]
            if ends and ends[-1] < len(words) - 1:
                return " ".join(words[ends[-1] + 1:]).title()
    n = len(allsegs)
    skipped = {n + p for p in skip if isinstance(p, int) and p < 0}
    segs = [s for i, s in enumerate(allsegs[:-1]) if i not in skipped]   # last segment is the home itself
    for seg in reversed(segs):
        m = DFW_CITY_RE.fullmatch(seg.replace("-", " "))
        if m:
            return canonical_city(m.group(1))
    return ""


def same_address(a: str, b: str) -> bool:
    ka, kb = address_key(a).split("-"), address_key(b).split("-")
    return len(ka) > 1 and len(kb) > 1 and ka[:2] == kb[:2]


def address_key(addr: str) -> str:
    s = addr.lower()
    s = re.split(r",", s)[0]
    repl = {"street": "st", "drive": "dr", "lane": "ln", "road": "rd", "court": "ct", "trail": "trl",
            "avenue": "ave", "boulevard": "blvd", "circle": "cir", "place": "pl", "parkway": "pkwy",
            "cove": "cv", "point": "pt", "terrace": "ter", "crossing": "xing"}
    words = [repl.get(w, w) for w in re.findall(r"[a-z0-9]+", s)]
    return "-".join(words)


# --------------------------------------------------------------------------- JSON sources
FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)')


def _flight_objects(soup: BeautifulSoup) -> Iterator[Any]:
    """Next.js App Router pages stream their data as string chunks (self.__next_f.push([1,"..."]))
    that only make sense joined together: 'id:JSON' lines (e.g. Beazer community pages)."""
    chunks = []
    for tag in soup.find_all("script"):
        raw = tag.string or ""
        if "__next_f" not in raw:
            continue
        for lit in FLIGHT_RE.findall(raw):
            try:
                chunks.append(json.loads('"' + lit + '"'))
            except ValueError:
                continue
    stream = "".join(chunks).encode("utf-8")
    if not re.search(rb'price"\s*:\s*\d', stream, re.I):
        return
    # rows: '<hex id>:<json>\n', or '<hex id>:T<hex byte length>,<text>' (no newline after the text)
    pos, n = 0, len(stream)
    row = re.compile(rb"([0-9a-fA-F]+):(T([0-9a-fA-F]+),)?")
    while pos < n:
        m = row.match(stream, pos)
        if not m:
            nl = stream.find(b"\n", pos)
            pos = n if nl < 0 else nl + 1
            continue
        if m.group(2):
            pos = m.end() + int(m.group(3), 16)
            continue
        nl = stream.find(b"\n", m.end())
        end = n if nl < 0 else nl
        body = stream[m.end():end]
        pos = end + 1
        if body[:1] in (b"[", b"{"):
            try:
                yield json.loads(body)
            except ValueError:
                continue


def _json_blobs(soup: BeautifulSoup) -> Iterator[tuple[str, Any]]:
    yield from (("embedded", o) for o in _flight_objects(soup))
    for tag in soup.find_all("script"):
        t = (tag.get("type") or "").lower()
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        if "ld+json" in t:
            try:
                yield "jsonld", json.loads(raw)
            except ValueError:
                continue
        elif "json" in t:
            try:
                yield "embedded", json.loads(raw)
            except ValueError:
                continue
        elif not tag.get("src") and re.search(r"price", raw, re.I):
            yield from (("embedded", o) for o in _objects_in_js(raw))
            if '\\"price' in raw or "\\u0022price" in raw:
                for lit in re.findall(r'"((?:[^"\\]|\\.){40,}?)"', raw):
                    if "price" not in lit:
                        continue
                    try:
                        decoded = json.loads('"' + lit + '"')
                    except ValueError:
                        continue
                    yield from (("embedded", o) for o in _objects_in_js(decoded, max_tries=400))
    for el in soup.find_all(attrs=True):
        for k, v in el.attrs.items():
            if k.startswith("data-") and isinstance(v, str) and v[:1] in "{[" and "price" in v.lower():
                try:
                    yield "embedded", json.loads(v)
                except ValueError:
                    pass


def _objects_in_js(js: str, max_tries: int = 60) -> Iterator[Any]:
    dec = json.JSONDecoder()
    tries = 0
    for m in re.finditer(r"(?:=|\(|:|^|\n)\s*([\[{])", js):
        tries += 1
        if tries > max_tries:
            break
        try:
            obj, _ = dec.raw_decode(js, m.start(1))
        except ValueError:
            continue
        if isinstance(obj, (dict, list)):
            yield obj


def _walk(obj: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 24:   # React server payloads nest homes ~20 levels deep
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:2000]:
            yield from _walk(v, depth + 1)


def _fields(d: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in d.items():
        f = _ALIAS_LOOKUP.get(norm_key(k))
        if f and f not in out and v not in (None, "", [], {}):
            out[f] = v
    # schema.org nesting
    offers = d.get("offers")
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if isinstance(offers, dict) and "price" not in out:
        p = offers.get("price") or offers.get("lowPrice")
        if p is not None:
            out["price"] = p
    for key in ("geo", "_geoloc", "geoloc", "location", "coordinates", "position", "latlng", "geocode"):
        g = d.get(key)
        if isinstance(g, dict) and "lat" not in out:
            gf = {_ALIAS_LOOKUP.get(norm_key(k)): v for k, v in g.items()}
            if gf.get("lat") is not None and gf.get("lng") is not None:
                out["lat"], out["lng"] = gf["lat"], gf["lng"]
    addr = d.get("address")
    if not isinstance(addr, dict):
        addr = next((v for k, v in d.items() if norm_key(k) == "address" and isinstance(v, dict)), None)
    if isinstance(addr, dict):
        a = {norm_key(k): v for k, v in addr.items() if v not in (None, "")}
        out["address"] = a.get("streetaddress") or a.get("street") or a.get("address1") or a.get("line1") or ""
        if not out.get("city") or isinstance(out.get("city"), dict):
            out["city"] = a.get("addresslocality") or a.get("city")
        out.setdefault("state", a.get("addressregion") or a.get("state") or a.get("stateabbreviation"))
        out.setdefault("zip", a.get("postalcode") or a.get("zip") or a.get("zipcode"))
    return out


def _home_from_dict(d: dict, base_url: str, source: str) -> dict | None:
    f = _fields(d)
    price = to_price(f.get("price"))
    if price is None:
        return None
    address = clean_address(to_text(f.get("address")))
    if address and not re.match(r"^\d", address):
        address = ""  # not a street address (e.g. "Call for address")
    beds, sqft = to_number(f.get("beds")), to_number(f.get("sqft"))
    if not address and not (beds and sqft):
        return None
    if sqft is not None and not (400 <= sqft <= 15000):
        sqft = None
    if beds is not None and not (1 <= beds <= 10):
        beds = None
    baths = to_number(f.get("baths"))
    half = to_number(f.get("half_baths"))
    if baths is not None and half:
        baths += 0.5 * half
    if baths is not None and not (1 <= baths <= 10):
        baths = None
    url = to_text(f.get("url"))
    if url and not url.startswith("http"):
        url = urljoin(base_url, url)
    if url and not url.startswith("http"):
        url = ""
    was = to_price(f.get("was_price"))
    status, move_in = to_text(f.get("status")), to_text(f.get("move_in"))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[\d:.]+Z?)?", status):  # an 'Availability' date, not a status
        move_in, status = move_in or status[:10], ""
    coords = valid_coords(f.get("lat"), f.get("lng"))
    return {
        "lat": coords[0] if coords else None, "lng": coords[1] if coords else None,
        "address": address,
        "city": to_text(f.get("city")),
        "state": to_text(f.get("state")),
        "zip": to_text(f.get("zip"))[:5],
        "community": to_text(f.get("community")),
        "plan": to_text(f.get("plan")),
        "beds": beds, "baths": baths, "sqft": int(sqft) if sqft else None,
        "price": price,
        "builder_was_price": was if was and was > price else None,
        "status": status[:60],
        "move_in": move_in[:40],
        "url": url,
        "source": source,
    }


# --------------------------------------------------------------------------- HTML fallback
def _is_struck(el) -> bool:
    for p in [el, *el.parents]:
        if getattr(p, "name", None) in ("del", "s", "strike"):
            return True
        cls = " ".join(p.get("class") or []) if hasattr(p, "get") else ""
        if re.search(r"(strike|\bwas\b|was-|orig|old-price|previous|line-through|slash|regular-price|list-price)", cls, re.I):
            return True
        style = p.get("style", "") if hasattr(p, "get") else ""
        if "line-through" in style:
            return True
        if p.name in ("body", "html"):
            break
    return False


SKIP_CTX = re.compile(r"(per month|/mo\b|monthly|est\.? payment|payment|closing|savings|save|\boff\b|credit|"
                      r"hoa|tax|deposit|sales price of|loan amount|based on|income|starting|from the|from \$)", re.I)
SALE_CTX = re.compile(r"(sale|now|reduced|special|new price|your price|current price|net price)\W*$", re.I)


SIMILAR_RE = re.compile(r"(also for sale|similar homes|similar quick|you may also like|other homes|nearby homes|"
                        r"more homes|related homes|homes like this|other quick move|more quick move|recently viewed|"
                        r"explore more homes|other available homes|available homes in|other plans)", re.I)


def _near(a, b, limit=200) -> bool:
    """True when two elements sit in the same small block (e.g. 'Priced at $702,050  Sale $609,900')."""
    pa = {id(x): x for x in a.parents}
    for x in b.parents:
        if id(x) in pa:
            text = x.get_text(" ", strip=True)
            addresses = {m.group(0).lower() for m in STREET_RE.finditer(text)}
            return len(text) <= limit and len(addresses) <= 1   # a second address = another home's card
    return False


def _pick_price(main):
    """Choose the home's asking price among the dollar amounts on the page.
    Struck-through amounts and amounts labeled like the 'before' price count as was-price;
    an amount labeled Sale/Now/Reduced wins; payment, HOA, credits etc. are skipped."""
    cands = []
    for el in main.find_all(True):
        own = el.get_text(" ", strip=True)
        if len(own) <= 80 and SIMILAR_RE.search(own) and not re.search(r"\$\s?\d", own):
            break   # everything after a "similar homes / also for sale" heading belongs to other homes
        if len(own) > 40 or not re.search(r"\$\s?\d{2,3}(?:,\d{3})+", own):
            continue
        if any(re.search(r"\$\s?\d", c.get_text(" ", strip=True)) for c in el.find_all(True)):
            continue  # innermost element holding a price
        p = to_price(re.search(r"\$\s?([\d,]+)", own).group(1))
        if p is None:
            continue
        before = ""
        prev = el.find_previous(string=True)
        hops = 0
        while prev is not None and hops < 3 and len(before) < 40:
            t = prev.strip()
            if t and not re.search(r"\$\s?\d", t):
                before = t + " " + before
            hops += 1
            prev = prev.find_previous(string=True)
        label = (before + " " + own.split("$")[0]).strip()
        if SKIP_CTX.search(label[-40:]) or SKIP_CTX.search(own):
            continue
        cands.append({"price": p, "struck": _is_struck(el), "sale": bool(SALE_CTX.search(label[-25:])), "el": el})
        if len(cands) >= 6:
            break
    if not cands:
        return None, None
    was = next((c["price"] for c in cands if c["struck"]), None)
    live = [c for c in cands if not c["struck"]]
    if not live:
        return None, was
    sale = next((i for i, c in enumerate(live) if c["sale"]), None)
    if sale is not None:
        if sale > 0 and was is None and live[sale - 1]["price"] > live[sale]["price"] \
                and _near(live[sale - 1]["el"], live[sale]["el"]):
            was = live[sale - 1]["price"]
        return live[sale]["price"], was
    first = live[0]["price"]
    if was is None and len(live) > 1 and live[1]["price"] < first and live[1]["price"] > first * 0.7 \
            and _near(live[0]["el"], live[1]["el"]):
        # "Priced at $702,050  $609,900" with no markup: the second, lower number is the current price
        return live[1]["price"], first
    return first, was


def _html_home(soup: BeautifulSoup, url: str) -> dict | None:
    for t in soup(["script", "style", "noscript", "footer", "nav"]):
        t.decompose()
    main = soup.find("main") or soup.body or soup
    price = was = None
    meta = soup.find("meta", attrs={"property": "product:price:amount"}) or soup.find(attrs={"itemprop": "price"})
    if meta:
        price = to_price(meta.get("content") or meta.get_text())
    if price is None:
        price, was = _pick_price(main)
    if price is None and main is not soup.body and soup.body is not None:
        price, was = _pick_price(soup.body)
    if price is None:
        return None
    text = main.get_text(" ", strip=True)
    # The page's own address is in its title/heading; addresses elsewhere may be neighbouring listings.
    heads = [soup.title.get_text(" ", strip=True) if soup.title else ""]
    og = soup.find("meta", attrs={"property": "og:title"})
    heads.append(og.get("content", "") if og else "")
    h1 = soup.find("h1")
    heads.append(h1.get_text(" ", strip=True) if h1 else "")
    m = next((STREET_RE.search(t) for t in heads if t and STREET_RE.search(t)), None)
    address = clean_address(m.group(0)) if m else ""
    beds = re.search(r"(\d(?:\.\d)?)\s*(?:-\s*\d\s*)?(?:beds?|bd|br|bedrooms?)\b", text, re.I)
    baths = re.search(r"(\d(?:\.\d)?)\s*(?:-\s*\d(?:\.\d)?\s*)?(?:baths?|ba|bathrooms?)\b", text, re.I)
    # whole numbers only: "From $509,762 Sq Ft 2074" must not read as 9,762 sq ft
    sqft = (re.search(r"(?<![\d,.$])(\d[\d,]{2,5})\s*(?:-\s*[\d,]+\s*)?(?:sq\.?\s*ft|sqft|square feet|sf)\b", text, re.I)
            or re.search(r"\b(?:sq\.?\s*ft\.?|sqft|square feet)\s*:?\s*(\d,?\d{3}|\d{3})\b(?![\d,])", text, re.I))
    status = STATUS_WORDS.search(text)
    full = (soup.body or soup).get_text(" ", strip=True)
    zipm = re.search(r",\s*(?:TX|Texas)\s+(\d{5})", text) or re.search(r",\s*(?:TX|Texas)\s+(\d{5})", full)
    city = None
    for src in (text, full):
        for cm in re.finditer(r",\s*(?:TX|Texas)\b", src):
            found = list(DFW_CITY_RE.finditer(src[max(0, cm.start() - 40):cm.start()]))
            if found:
                city = found[-1]
                break
        if city:
            break
    sq = to_number(sqft.group(1)) if sqft else None
    return {
        "address": address,
        "city": city.group(1) if city else "",
        "state": "TX" if zipm or city else "",
        "zip": zipm.group(1) if zipm else "",
        "community": "", "plan": "",
        "beds": to_number(beds.group(1)) if beds else None,
        "baths": to_number(baths.group(1)) if baths else None,
        "sqft": int(sq) if sq and 400 <= sq <= 15000 else None,
        "price": price,
        "builder_was_price": was if was and was > price else None,
        "status": status.group(0)[:60] if status else "",
        "move_in": "",
        "url": url,
        "source": "html",
        "lat": None, "lng": None,
    }


DISCLAIMER = re.compile(r"(\bapr\b.*based on|fico|not all (borrowers|buyers)|subject to (credit|change)|"
                        r"sales price of|loan amount|per \$1,?000|nmls|cannot be combined|terms and conditions|"
                        r"based on a|example|payments? (exclude|of \$)|restrictions apply|without (prior )?notice|"
                        r"may be (changed|modified|discontinued|withdrawn)|forward commitments)", re.I)


OFFER_RE = re.compile(
    r"(\$\s?[\d,.]+\s?k?\s*(?:off\b|in flex cash|flex cash|toward|towards|in closing|closing cost|"
    r"seller credit|in savings|savings|incentive|bonus|allowance|price reduction|design (?:center )?(?:credit|upgrades?))"
    r"|(?:reduced|save|savings of|price reduction of|price drop of)\s*(?:up to\s*)?\$\s?[\d,.]+\s?k?"
    r"|up to \$\s?[\d,.]+\s?k?(?=[^.|]{0,60}(?:closing|flex|credit|incentive|toward|upgrade|design|savings|\boff\b|bonus))"
    r"|\d+(?:\.\d+)?\s?%\s*(?:towards?|to|seller|in)\s*(?:closing|contribution|seller)"
    r"|(?:rates?|financing|interest)\s*(?:as low as|starting at|from|of)\s*\d+(?:\.\d+)?\s?%"
    r"|\d+(?:\.\d+)?\s?%\s*(?:fixed|rate|\(\d|apr)"
    r"|\d/\d(?:/\d)?\s*buy ?-?down|zero down|\$0 down|flex cash)", re.I)
SPEC_RE = re.compile(r"\b(beds?|baths?|sq\.? ?ft|stories|garage|homesite|schedule a tour|est\.? completion)\b", re.I)


def _offer_window(s: str, m: re.Match, width: int = 70) -> str:
    """Trim a long block of text to the words around the offer phrase."""
    if len(s) <= 120 and not SPEC_RE.search(s) and not re.search(r"\$[\d,]{6,}|completion|skip to", s, re.I):
        return s
    a, b = max(0, m.start() - width), min(len(s), m.end() + 2 * width)   # right side runs to the sentence end
    while a > 0 and not s[a - 1].isspace():          # never cut a word or number in half
        a -= 1
    while b < len(s) and not s[b].isspace():
        b += 1
    piece = s[a:b]
    # cut back to clause boundaries / spec words so neighbouring listing details don't leak in
    left = re.split(r"(?:\s[|•*]\s|\b(?:beds?|baths?|sq\.? ?ft|stories|garage|completion|main content)\b|\$[\d,]{6,})",
                    s[a:m.start()], flags=re.I)[-1]
    right = re.split(r"(?:\s[|•*]\s|[.!](?:\s|$)|\b(?:beds?|baths?|est\.|completion|homesite)\b)",
                     s[m.start():b], flags=re.I)[0]
    piece = (left + right).strip(" ,;:-|*•")
    return piece if len(piece) >= 6 else s[m.start():m.end()]


def incentive_snippets(text: str, limit: int = 3) -> list[str]:
    """Phrases that state an offer (not fine print, calculators or listing specs)."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\s{2,}|\n|\s\|\s", text):
        s = sent.strip(" *•")
        if len(s) < 6 or DISCLAIMER.search(s):
            continue
        m = OFFER_RE.search(s)
        if not m:
            continue
        piece = _offer_window(s, m)
        piece = re.sub(r"^[\s|•*:,;-]+|[\s|•*:,;-]+$|\s+est\.?$", "", piece, flags=re.I)
        if 6 <= len(piece) <= 220 and piece not in out and not any(piece in o for o in out):
            out.append(piece)
        if len(out) >= limit:
            break
    # Several "$NK off"-style per-home discounts on one page means a list of other homes is shown;
    # the first one belongs to this home.
    per_home = [o for o in out if re.fullmatch(r"\$[\d,.]+\s?k?\s*off", o, re.I)]
    if len(per_home) > 1:
        out = [o for o in out if o not in per_home[1:]]
    return out


def card_homes(html: str, page_url: str, detail_re: re.Pattern) -> list[dict]:
    """Listing pages that render each home as a card: find links to home pages, climb to the
    smallest container holding just that home, and read price/beds/baths/sqft from it."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    # map markers carried as attributes: data-latitude/-longitude, keyed by a data-*-id the card also has
    def el_coords(el):
        a = el.attrs
        lat = a.get("data-latitude") or a.get("data-lat")
        lng = a.get("data-longitude") or a.get("data-lng") or a.get("data-lon")
        return valid_coords(lat, lng) if lat and lng else None
    marker_by_id = {}
    for el in soup.find_all(lambda t: t.has_attr("data-latitude") or t.has_attr("data-lat")):
        c = el_coords(el)
        if c:
            for k, v in el.attrs.items():
                if k.startswith("data-") and k.endswith("id") and isinstance(v, str) and v.strip():
                    marker_by_id.setdefault(v.strip(), c)
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"]).split("#")[0].split("?")[0]
        if not url.startswith("http") or not detail_re.search(urlparse(url).path) or NON_HOME_RE.search(url):
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            continue
        box = a
        for _ in range(8):
            parent = box.parent
            if parent is None or parent.name in ("body", "html"):
                break
            hrefs = {urljoin(page_url, x["href"]).split("#")[0].split("?")[0].rstrip("/").lower()
                     for x in parent.find_all("a", href=True)
                     if detail_re.search(urlparse(urljoin(page_url, x["href"])).path)}
            if len(hrefs) > 1:
                break
            box = parent
            if re.search(r"\$\s?\d{2,3},\d{3}", box.get_text(" ", strip=True)):
                break
        h = _html_home(BeautifulSoup(str(box), "lxml"), url)
        if not h:
            continue
        for el in [box, *box.find_all(True), *list(box.parents)[:5]]:
            if el.name in ("body", "html", "[document]") or el.find_all("a", href=True, limit=40).__len__() > 30:
                continue
            c = el_coords(el)
            if not c:
                for k, v in el.attrs.items():
                    if k.startswith("data-") and k.endswith("id") and isinstance(v, str) and v.strip() in marker_by_id:
                        c = marker_by_id[v.strip()]
                        break
            if c:
                h["lat"], h["lng"] = c
                break
        seen.add(key)
        h["source"] = "card"
        h["incentive_text"] = " | ".join(incentive_snippets(box.get_text("  ", strip=True), limit=2))
        h["status"] = h["status"] or ""
        if not h["address"] or not same_address(h["address"], address_from_url(url) or h["address"]):
            h["address"] = address_from_url(url) or h["address"]
        if not h["city"]:
            h["city"] = city_from_url(url)
        if not h["address"]:
            continue  # without an address a card can't be told apart from a promo tile
        out.append(h)
    return out


def _geo_index_from_blobs(blobs) -> dict:
    """address key -> (lat, lng) for every record on the page that has both an address and coordinates
    (map-marker lists often carry coordinates without prices)."""
    idx = {}
    for _src, blob in blobs:
        for d in _walk(blob):
            f = _fields(d)
            coords = valid_coords(f.get("lat"), f.get("lng"))
            if not coords:
                continue
            addr = to_text(f.get("address"))
            if not re.match(r"^\d", addr or ""):
                name = to_text(d.get("name") or d.get("title"))
                addr = name if re.match(r"^\d{2,6}\s+\w", name or "") else ""
            if addr:
                idx.setdefault(address_key(clean_address(addr)), coords)
    return idx


def page_geo_index(html: str) -> dict:
    return _geo_index_from_blobs(list(_json_blobs(BeautifulSoup(html, "lxml"))))


def fill_coords(homes: list[dict], idx: dict):
    for h in homes:
        if h.get("lat") is None and h.get("address"):
            c = idx.get(address_key(h["address"]))
            if c:
                h["lat"], h["lng"] = c


# --------------------------------------------------------------------------- entry points
def extract_homes(html: str, page_url: str, single: bool = False) -> list[dict]:
    """single=True: page is one home's page. Falls back to visible HTML and attaches the page URL."""
    soup = BeautifulSoup(html, "lxml")
    homes: list[dict] = []
    seen: set[tuple] = set()
    blobs = list(_json_blobs(soup))
    for source, blob in blobs:
        for d in _walk(blob):
            h = _home_from_dict(d, page_url, source)
            if not h:
                continue
            key = (address_key(h["address"]) or h["url"] or f'{h["beds"]}-{h["sqft"]}', h["price"])
            if key in seen:
                continue
            seen.add(key)
            homes.append(h)
    fill_coords(homes, _geo_index_from_blobs(blobs))
    for h in homes:
        if h.get("url"):
            if not h.get("address"):
                h["address"] = address_from_url(h["url"])
            if not h.get("city"):
                h["city"] = city_from_url(h["url"])
    page_text = soup.get_text("  ", strip=True)
    if single:
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if re.search(r"\b(home plan|floor ?plan|plan \w+ in)\b", title, re.I) and not STREET_RE.search(title):
            return []  # a floor-plan page, not a home for sale
        # Home pages often also list "similar homes"; keep only the record that is this page's home.
        url_addr = address_from_url(page_url)
        mine = [h for h in homes if h["url"] and h["url"].rstrip("/").lower() == page_url.rstrip("/").lower()]
        if not mine and url_addr:
            mine = [h for h in homes if h["address"] and same_address(h["address"], url_addr)]
        if not mine and len(homes) == 1 and (not url_addr or not homes[0]["address"]):
            mine = homes
        homes = mine[:1]
        fb = _html_home(BeautifulSoup(html, "lxml"), page_url)
        if fb and url_addr and fb["address"] and not same_address(fb["address"], url_addr):
            fb["address"] = url_addr  # the address the HTML scan found belongs to another listing
        if not url_addr and fb and fb["address"]:
            # no address in the URL: the title's address identifies this home; JSON records must match it
            homes = [h for h in homes if h["address"] and same_address(h["address"], fb["address"])][:1]
        if not homes and fb:
            homes = [fb]
        elif homes and fb:  # fill gaps from the visible page
            for k, v in fb.items():
                if not homes[0].get(k) and v and k not in ("price", "builder_was_price"):
                    homes[0][k] = v
        main = soup.find("main") or soup.body or soup
        main_text = main.get_text("  ", strip=True)
        maps = {valid_coords(m.group(1), m.group(2)) for m in re.finditer(
            r"google\.[a-z.]+/maps[^\"'<>\s]*?[?&](?:q|query|ll|daddr)=(-?\d{2}\.\d{4,}),\s*(-?\d{2,3}\.\d{4,})", html)}
        maps.discard(None)
        for h in homes:
            h["url"] = page_url
            if not h.get("address") and url_addr:
                h["address"] = url_addr
            if h.get("lat") is None and len(maps) == 1:
                h["lat"], h["lng"] = next(iter(maps))  # the page's own "view on map" link
            h["incentive_text"] = " | ".join(incentive_snippets(main_text) or incentive_snippets(page_text))
            h["from_detail"] = True
    return homes


def _path(u: str) -> str:
    return urlparse(u or "").path.lower().rstrip("/")


def page_is_elsewhere(requested_url: str, final_url: str, html: str) -> bool:
    """True when a home's URL now lands on a different page (builders redirect a sold or withdrawn
    home to its community or city page). Checks the URL we ended up on and the page's canonical link."""
    want = _path(requested_url)
    last = want.rsplit("/", 1)[-1]
    if not last:
        return False
    candidates = [_path(final_url)]
    head = html[:200_000]
    m = (re.search(r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)', head, re.I)
         or re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']canonical', head, re.I)
         or re.search(r'<meta[^>]+property=["\']og:url["\'][^>]*content=["\']([^"\']+)', head, re.I))
    if m:
        candidates.append(_path(m.group(1)))
    ancestors = set(want.split("/")[:-1]) - {""}
    for p in candidates:
        tail = p.rsplit("/", 1)[-1] if p else ""
        if p and tail != last and tail in ancestors and p.count("/") < want.count("/"):
            return True          # now shows its community's page under another path:
                                 # /homes-ready-soon/.../tavolo-park-50/19000126 -> /new-homes/.../tavolo-park-50
        if p and p != want and p.rsplit("/", 1)[-1] != last and want.startswith(p + "/"):
            return True          # moved up the tree: /community/qmis/123-main-st -> /community
        if p and p.rsplit("/", 1)[-1] != last and not re.search(r"\d", p.rsplit("/", 1)[-1]) \
                and re.search(r"\d", last):
            return True          # a home URL (has a number) now shows a page without one
    return False
