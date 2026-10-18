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
    "sqft": ["sqft", "squarefeet", "squarefootage", "livingarea", "floorsize", "homesize", "sqftmax", "maxsqft",
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
}
_ALIAS_LOOKUP = {a: field for field, names in ALIASES.items() for a in names}

INCENTIVE_WORDS = re.compile(
    r"(closing cost|rate buy ?down|buy ?down|interest rate|as low as \d|\bapr\b|flex ?cash|incentive|"
    r"price reduc|reduced|savings|save \$|\$[\d,]+k? off|seller credit|credit toward|special financing|promo)",
    re.I)
STATUS_WORDS = re.compile(
    r"(move-?in ready|ready now|available now|under construction|coming soon|under contract|sold|pending|"
    r"ready (?:in )?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? ?\d{0,4})", re.I)
DFW_CITY_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(c) for c in "Allen|Anna|Argyle|Arlington|Aubrey|Azle|Balch Springs|Bedford|Benbrook|Burleson|Caddo Mills|Carrollton|Cedar Hill|Celina|Cleburne|Colleyville|Commerce|Coppell|Corinth|Crandall|Crowley|Dallas|Decatur|Denton|DeSoto|Duncanville|Ennis|Euless|Farmers Branch|Farmersville|Fate|Ferris|Flower Mound|Forney|Fort Worth|Frisco|Gainesville|Garland|Glenn Heights|Godley|Granbury|Grand Prairie|Grapevine|Greenville|Gunter|Haltom City|Haslet|Heath|Howe|Hurst|Irving|Josephine|Joshua|Justin|Kaufman|Keller|Kennedale|Krum|Lancaster|Lavon|Lewisville|Little Elm|Lucas|Mansfield|McKinney|McLendon-Chisholm|Melissa|Mesquite|Midlothian|Murphy|Nevada|New Fairview|Northlake|North Richland Hills|Oak Point|Ovilla|Parker|Pilot Point|Plano|Ponder|Princeton|Prosper|Providence Village|Red Oak|Rhome|Richardson|Roanoke|Rockwall|Rowlett|Royse City|Sachse|Saginaw|Sanger|Seagoville|Sherman|Southlake|Springtown|Sunnyvale|Terrell|Trenton|Van Alstyne|Venus|Watauga|Waxahachie|Weatherford|Westlake|Wylie|Aledo|Alvarado|Denison|Lantana|Tioga".split("|")), key=len, reverse=True)) + r")\b", re.I)
STREET_RE = re.compile(
    r"\b\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z0-9'.]*(?:\s+[A-Z][A-Za-z0-9'.]*){0,4}\s+"
    r"(?:Street|St|Drive|Dr|Lane|Ln|Road|Rd|Court|Ct|Way|Trail|Trl|Avenue|Ave|Boulevard|Blvd|Circle|Cir|"
    r"Place|Pl|Parkway|Pkwy|Loop|Path|Run|Cove|Cv|Ridge|Bend|Pass|Point|Pt|Terrace|Ter|Row|Crossing|Xing|"
    r"Hollow|Glen|Grove|Meadow|Meadows|Park|View|Vista)\b\.?")


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
            "trace", "landing", "creek", "hill", "hills", "heights", "court", "commons", "alley", "spur"}


def address_from_url(url: str) -> str:
    """'.../lots/1124-433-landyn-street/' -> '433 Landyn Street' (only if it ends in a street suffix)."""
    seg = [x for x in urlparse(url).path.lower().split("/") if x]
    if not seg:
        return ""
    m = re.search(r"(?:^|-)(\d{2,6})-((?:[a-z]+-?){1,5})$", seg[-1])
    if not m:
        return ""
    words = m.group(2).strip("-").split("-")
    if words[-1] not in SUFFIXES or len(words) < 2:
        return ""
    return m.group(1) + " " + " ".join(w.capitalize() for w in words)


def city_from_url(url: str) -> str:
    m = DFW_CITY_RE.search(urlparse(url).path.replace("-", " ").replace("/", " / "))
    return m.group(1).title() if m else ""


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
def _json_blobs(soup: BeautifulSoup) -> Iterator[tuple[str, Any]]:
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
    if depth > 14:
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
    addr = d.get("address")
    if isinstance(addr, dict):
        out["address"] = addr.get("streetAddress") or addr.get("street") or addr.get("address1") or ""
        out.setdefault("city", addr.get("addressLocality") or addr.get("city"))
        out.setdefault("state", addr.get("addressRegion") or addr.get("state"))
        out.setdefault("zip", addr.get("postalCode") or addr.get("zip"))
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
    return {
        "address": address,
        "city": to_text(f.get("city")),
        "state": to_text(f.get("state")),
        "zip": to_text(f.get("zip"))[:5],
        "community": to_text(f.get("community")),
        "plan": to_text(f.get("plan")),
        "beds": beds, "baths": baths, "sqft": int(sqft) if sqft else None,
        "price": price,
        "builder_was_price": was if was and was > price else None,
        "status": to_text(f.get("status"))[:60],
        "move_in": to_text(f.get("move_in"))[:40],
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


def _pick_price(main):
    """Choose the home's asking price among the dollar amounts on the page.
    Struck-through amounts and amounts labeled like the 'before' price count as was-price;
    an amount labeled Sale/Now/Reduced wins; payment, HOA, credits etc. are skipped."""
    cands = []
    for el in main.find_all(True):
        own = el.get_text(" ", strip=True)
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
        cands.append({"price": p, "struck": _is_struck(el), "sale": bool(SALE_CTX.search(label[-25:]))})
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
        if sale > 0 and was is None and live[sale - 1]["price"] > live[sale]["price"]:
            was = live[sale - 1]["price"]
        return live[sale]["price"], was
    first = live[0]["price"]
    if was is None and len(live) > 1 and live[1]["price"] < first and live[1]["price"] > first * 0.7:
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
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    m = STREET_RE.search(title) or STREET_RE.search(text[:4000])
    address = clean_address(m.group(0)) if m else ""
    beds = re.search(r"(\d(?:\.\d)?)\s*(?:-\s*\d\s*)?(?:beds?|bd|br|bedrooms?)\b", text, re.I)
    baths = re.search(r"(\d(?:\.\d)?)\s*(?:-\s*\d(?:\.\d)?\s*)?(?:baths?|ba|bathrooms?)\b", text, re.I)
    sqft = re.search(r"(\d[\d,]{2,5})\s*(?:-\s*[\d,]+\s*)?(?:sq\.?\s*ft|sqft|square feet|sf)\b", text, re.I)
    status = STATUS_WORDS.search(text)
    zipm = re.search(r",\s*TX\s+(\d{5})", text)
    city = None
    for cm in re.finditer(r",\s*(?:TX|Texas)\b", text):
        found = list(DFW_CITY_RE.finditer(text[max(0, cm.start() - 40):cm.start()]))
        if found:
            city = found[-1]
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
    }


def incentive_snippets(text: str, limit: int = 3) -> list[str]:
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\s{2,}|\n", text):
        s = sent.strip()
        if 15 <= len(s) <= 300 and INCENTIVE_WORDS.search(s) and re.search(r"[\d$%]", s):
            if s not in out:
                out.append(s)
        if len(out) >= limit:
            break
    return out


def card_homes(html: str, page_url: str, detail_re: re.Pattern) -> list[dict]:
    """Listing pages that render each home as a card: find links to home pages, climb to the
    smallest container holding just that home, and read price/beds/baths/sqft from it."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"]).split("#")[0].split("?")[0]
        if not url.startswith("http") or not detail_re.search(urlparse(url).path):
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
        seen.add(key)
        h["source"] = "card"
        h["status"] = h["status"] or ""
        if not h["address"] or not same_address(h["address"], address_from_url(url) or h["address"]):
            h["address"] = address_from_url(url) or h["address"]
        if not h["city"]:
            h["city"] = city_from_url(url)
        if not h["address"]:
            continue  # without an address a card can't be told apart from a promo tile
        out.append(h)
    return out


# --------------------------------------------------------------------------- entry points
def extract_homes(html: str, page_url: str, single: bool = False) -> list[dict]:
    """single=True: page is one home's page. Falls back to visible HTML and attaches the page URL."""
    soup = BeautifulSoup(html, "lxml")
    homes: list[dict] = []
    seen: set[tuple] = set()
    for source, blob in _json_blobs(soup):
        for d in _walk(blob):
            h = _home_from_dict(d, page_url, source)
            if not h:
                continue
            key = (address_key(h["address"]) or h["url"] or f'{h["beds"]}-{h["sqft"]}', h["price"])
            if key in seen:
                continue
            seen.add(key)
            homes.append(h)
    for h in homes:
        if h.get("url"):
            if not h.get("address"):
                h["address"] = address_from_url(h["url"])
            if not h.get("city"):
                h["city"] = city_from_url(h["url"])
    page_text = soup.get_text(" ", strip=True)
    if single:
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
        if not homes and fb:
            homes = [fb]
        elif homes and fb:  # fill gaps from the visible page
            for k, v in fb.items():
                if not homes[0].get(k) and v and k not in ("price", "builder_was_price"):
                    homes[0][k] = v
        for h in homes:
            h["url"] = page_url
            if not h.get("address") and url_addr:
                h["address"] = url_addr
            h["incentive_text"] = " | ".join(incentive_snippets(page_text))
    return homes
