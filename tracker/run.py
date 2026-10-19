"""One tracking run: for each builder, read listing pages, queue and refresh home pages
(new first, then least recently checked, within a per-builder page budget), read promo
pages, then record price changes, new/removed homes and offer changes.

Usage:
    python -m tracker.run                       # all builders
    python -m tracker.run --only bloomfield     # one builder
    python -m tracker.run --budget 40           # fewer pages per builder (testing)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

from . import discover, incentives
from .extract import DFW_CITY_RE, NON_HOME_RE, address_from_url, canonical_city, city_from_url, fill_coords, page_geo_index, address_key, card_homes, extract_homes, same_address, to_price
from .fetch import PoliteClient
from .store import State, store_from_env

log = logging.getLogger("tracker")
TZ = ZoneInfo("America/Chicago")
FMT = "%Y-%m-%d %H:%M"

MIN_CHANGE = 100            # ignore price wiggles smaller than this
SUSPECT_PCT = 0.30          # a move bigger than 30% must be seen twice before it's recorded
REMOVE_AFTER_H = 48         # active home unseen this long (while its builder is readable) -> removed
SITEMAP_EVERY_H = 6
MAX_PAGINATION = 8



def now_local() -> datetime:
    return datetime.now(TZ).replace(second=0, microsecond=0)


def ts(dt: datetime) -> str:
    return dt.strftime(FMT)


def parse_ts(s) -> datetime | None:
    try:
        return datetime.strptime(str(s), FMT).replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None


def in_dfw(h: dict) -> bool:
    z = (h.get("zip") or "")[:5]
    if z.isdigit() and (75000 <= int(z) <= 75499 or 76000 <= int(z) <= 76499):
        return True
    if z.isdigit():
        return False
    st = (h.get("state") or "").upper()
    if st and st not in ("TX", "TEXAS"):
        return False
    return bool(DFW_CITY_RE.search(h.get("city") or ""))


def outside_dfw(h: dict) -> bool:
    """True when a home's own data puts it in another Texas metro (Austin, San Antonio, Houston, ...):
    a non-DFW Texas ZIP and no DFW city stated on the page. Such homes are never tracked, even when the
    builder is marked DFW-only or its URL matched the builder's DFW filter."""
    z = (h.get("zip") or "")[:5]
    if not z.isdigit():
        return False
    n = int(z)
    tx_other = 73300 <= n <= 73399 or 75500 <= n <= 75999 or 76500 <= n <= 79999
    return tx_other and not DFW_CITY_RE.fullmatch((h.get("city") or "").strip())


STATUS_SAME = {"readynow": "ready", "moveinready": "ready", "movein": "ready", "availablenow": "ready",
               "quickmovein": "ready", "completed": "ready", "complete": "ready", "finished": "ready"}


def status_key(s) -> str:
    """'Move-in ready' and 'Ready Now' are the same status worded differently."""
    k = re.sub(r"[^a-z]", "", str(s or "").lower())
    return STATUS_SAME.get(k, k)


def load_config(path="builders.yaml") -> list[dict]:
    cfg = yaml.safe_load(open(path))
    d = cfg.get("defaults", {})
    out = []
    for b in cfg["builders"]:
        bb = {**d, **b}
        bb["detail_re"] = re.compile(bb["detail_pattern"], re.I)
        bb["filter_re"] = re.compile(bb["sitemap_filter"], re.I) if bb.get("sitemap_filter") else None
        bb["hub_re"] = re.compile(bb["hub_pattern"], re.I) if bb.get("hub_pattern") else None
        out.append(bb)
    return out


# ------------------------------------------------------------------ recording
def offer_terms(text: str) -> frozenset:
    """The numbers that make an offer what it is: dollar amounts, percentages, buydown structure."""
    t = (text or "").lower().replace(",", "")
    terms = set(re.findall(r"\$\s?\d+(?:\.\d+)?\s?k?", t)) | set(re.findall(r"\d+(?:\.\d+)?\s?%", t))
    terms |= set(re.findall(r"\b\d/\d(?:/\d)?\b", t))
    return frozenset(x.replace(" ", "") for x in terms)



def locked(fn):
    def wrapper(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    return wrapper


class Recorder:
    """Applies observations to state. Thread-safe: builders are crawled in parallel."""

    def __init__(self, st: State, now: datetime):
        self.st, self.now, self.changes = st, now, []
        self.stats: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.run_rank: dict[str, int] = {}  # home_id -> best source rank read this run
        self.run_applied: dict[str, bool] = {}  # home_id -> did this run's best reading set the price

    @locked
    def stat(self, slug, key, n=1):
        self.stats.setdefault(slug, {}).setdefault(key, 0)
        self.stats[slug][key] += n

    @locked
    def event(self, b, event, h, old=None, new=None, detail=""):
        delta = (new - old) if (old is not None and new is not None) else None
        pct = round(delta / old * 100, 2) if delta is not None and old else None
        self.changes.append({"at": ts(self.now), "builder": b["name"], "event": event,
                             "home_id": h.get("home_id", ""), "address": h.get("address", ""),
                             "community": h.get("community", ""), "city": h.get("city", ""),
                             "old": old, "new": new, "delta": delta, "pct": pct,
                             "url": h.get("url", "") or h.get("source_url", ""), "detail": detail[:500]})

    def home_id(self, b, obs) -> str | None:
        if obs.get("address"):
            unit = re.search(r"-unit-([a-z0-9]+)/?$", (obs.get("url") or "").lower())
            return f'{b["slug"]}:{address_key(obs["address"])}' + (f"-unit-{unit.group(1)}" if unit else "")
        if obs.get("url"):
            return f'{b["slug"]}:{urlparse(obs["url"]).path.strip("/").lower()}'
        return None

    @staticmethod
    def url_parts(b, obs):
        parts = b.get("url_parts") or {}
        url = obs.get("url") or ""
        if not parts or not url:
            return
        segs = [x for x in urlparse(url).path.split("/") if x]
        for field, pos in parts.items():
            if obs.get(field) or not isinstance(pos, int) or len(segs) < abs(pos) + 2:
                continue
            seg = segs[pos]
            if re.fullmatch(r"[\d-]+|lots?|series|quick-move-in-home|homes?", seg, re.I):
                continue
            small = {"at", "of", "on", "the", "and", "in"}
            words = seg.split("-")
            obs[field] = " ".join(
                w.upper() if re.fullmatch(r"[ivx]{2,4}", w, re.I) else
                w if w.isdigit() else w.lower() if (w.lower() in small and i > 0) else w.capitalize()
                for i, w in enumerate(words))

    @locked
    def observe(self, b, obs):
        if outside_dfw(obs):
            return
        if not b.get("dfw_only") and not in_dfw(obs) and not (
                obs.get("url") and b.get("filter_re") and b["filter_re"].search(obs["url"])):
            return
        hid = self.home_id(b, obs)
        if not hid or not obs.get("price"):
            return
        self.url_parts(b, obs)
        now = ts(self.now)
        price = int(obs["price"])
        url_city = city_from_url(obs["url"], (b.get("url_parts") or {}).values()) if obs.get("url") else ""
        if url_city and (not obs.get("city") or obs["city"].lower() in ("dallas", "fort worth", "ft worth")):
            obs["city"] = url_city   # metro name in the data, actual town in the URL
        if obs.get("city"):
            obs["city"] = canonical_city(obs["city"])
        # How much to trust this reading: the home's own page > a listing card > listing-page data.
        rank = 3 if obs.get("from_detail") else 2 if obs.get("source") == "card" else 1
        seen_rank = self.run_rank.get(hid)
        if seen_rank is None:
            self.stat(b["slug"], "homes_seen")
        elif seen_rank >= rank:
            return hid  # already have an equal or better reading of this home this run
        self.run_rank[hid] = rank
        h = self.st.homes.get(hid)
        applied_earlier = self.run_applied.get(hid, False)
        self.run_applied[hid] = True
        if h is not None and seen_rank is not None and applied_earlier and h.get("price") != price:
            # a better source this run disagrees with an earlier reading this run: take it, no event
            if h.get("first_seen") == now:
                h["first_price"] = price
                for c in self.changes:
                    if c["home_id"] == hid and c["event"] == "new_home":
                        c["new"] = price
            h["price"], h["price_source"] = price, rank
        elif h is not None and int(h.get("price_source") or 1) > rank and h.get("price") != price:
            price = h["price"]  # a weaker source disagrees with what a stronger one said: ignore it
            self.run_applied[hid] = False
        elif h is not None and int(h.get("price_source") or 1) < rank and h.get("price") != price:
            h["price"], h["price_source"] = price, rank  # first reading from a stronger source: adopt quietly
        if h is not None:
            h["price_source"] = max(int(h.get("price_source") or 1), rank)
        if h is None:
            h = {"home_id": hid, "builder": b["name"], "first_price": price, "price": price, "cuts": 0,
                 "first_seen": now, "active": True, "price_source": rank}
            self.st.homes[hid] = h
            for k in ("community", "city", "zip", "address", "plan", "beds", "baths", "sqft", "status", "move_in",
                      "url", "builder_was_price", "incentive_text"):
                if obs.get(k) not in (None, ""):
                    h[k] = obs[k]
            if h.get("incentive_text"):
                h["offer_since"] = now
            if h.get("status"):
                h["status_rank"] = rank
            if obs.get("from_detail"):
                h["page_read"] = True
            self.event(b, "new_home", h, new=price)
            self.stat(b["slug"], "new_homes")
        else:
            if not h.get("active"):
                self.event(b, "relisted", h, new=price)
                h["active"] = True
            old = h.get("price")
            if old and abs(price - old) >= MIN_CHANGE:
                big = abs(price - old) / old > SUSPECT_PCT
                if big and h.get("pending_price") != price:
                    h["pending_price"] = price  # confirm on the next look before recording
                    self.event(b, "price_unconfirmed", h, old, price, "large move; waiting for a second reading")
                else:
                    kind = "price_cut" if price < old else "price_increase"
                    self.event(b, kind, h, old, price)
                    self.stat(b["slug"], "price_cuts" if price < old else "price_increases")
                    h.update(prev_price=old, price=price, last_change=price - old, last_change_at=now,
                             pending_price=None)
                    if price < old:
                        h["cuts"] = int(h.get("cuts") or 0) + 1
            else:
                h["pending_price"] = None
            for k in ("community", "city", "zip", "address", "plan", "beds", "baths", "sqft", "url"):
                if obs.get(k) not in (None, "") and not h.get(k):
                    h[k] = obs[k]
            if obs.get("city") and h.get("city") != obs["city"] and str(h.get("city") or "").lower() in (
                    "dallas", "fort worth", "ft worth", obs["city"].lower()):
                h["city"] = obs["city"]
            # status / move-in / was-price follow the same source ranking as price
            if rank >= int(h.get("status_rank") or 0):
                st_new = obs.get("status") or ""
                if (st_new and h.get("status") and status_key(st_new) != status_key(h["status"])
                        and rank == int(h.get("status_rank") or 0) and seen_rank is None
                        and h.get("first_seen") != now):
                    self.event(b, "status_change", h, detail=f'{h["status"]} -> {st_new}')
                for k in ("status", "move_in", "builder_was_price"):
                    if obs.get(k) not in (None, ""):
                        h[k] = obs[k]
                if st_new:
                    h["status_rank"] = rank
            new_inc = obs.get("incentive_text") or ""
            old_inc = h.get("incentive_text") or ""
            first_page_read = obs.get("from_detail") and not h.get("page_read")
            if obs.get("from_detail"):
                h["page_read"] = True
            if obs.get("from_detail"):  # the home's own page is the authority on its offer
                if new_inc and new_inc != old_inc:
                    if old_inc and offer_terms(old_inc) == offer_terms(new_inc):
                        h["incentive_text"] = new_inc   # same offer, reworded or cut differently: keep its date
                    else:
                        if h.get("first_seen") == now or first_page_read:
                            pass  # brand-new home, or first look at its page: not a change
                        elif old_inc:
                            self.event(b, "home_offer_changed", h, detail=f"{old_inc}  ->  {new_inc}")
                        else:
                            self.event(b, "home_offer_new", h, detail=new_inc)
                        h["incentive_text"], h["offer_since"] = new_inc, now
                elif not new_inc and old_inc and not first_page_read:
                    self.event(b, "home_offer_removed", h, detail=old_inc)
                    for k in ("incentive_text", "offer_type", "offer_amount", "offer_rate", "offer_since"):
                        h[k] = None
            elif new_inc and not old_inc:  # listing cards only fill a gap
                if h.get("first_seen") != now:
                    self.event(b, "home_offer_new", h, detail=new_inc)
                h["incentive_text"], h["offer_since"] = new_inc, now
        if h.get("incentive_text"):
            t = h["incentive_text"]
            h["offer_type"] = incentives.classify(t)
            h["offer_amount"] = incentives.headline_amount(t)
            h["offer_rate"] = incentives.lowest_rate(t)
            h.setdefault("offer_since", h.get("first_seen"))
        if obs.get("lat") is not None and obs.get("lng") is not None:
            h.update(lat=obs["lat"], lng=obs["lng"], location_precision="builder")
        h["last_seen"] = now
        fp, p = h.get("first_price"), h.get("price")
        if fp and p:
            h["change_since_first"] = p - fp
            h["pct_since_first"] = round((p - fp) / fp * 100, 2)
        if p and h.get("sqft"):
            h["price_per_sqft"] = round(p / float(h["sqft"]))
        return hid

    @locked
    def retire_unseen(self, b):
        cutoff = self.now - timedelta(hours=REMOVE_AFTER_H)
        for h in self.st.homes.values():
            if h.get("builder") != b["name"] or not h.get("active"):
                continue
            seen = parse_ts(h.get("last_seen"))
            if seen and seen < cutoff:
                h["active"] = False
                self.event(b, "removed", h, old=h.get("price"), detail="no longer listed (sold, under contract or withdrawn)")

    @locked
    def offers(self, b, url, found: list[dict]):
        now = ts(self.now)
        current = {}
        for o in found:
            oid = incentives.offer_id(b["slug"], o["text"])
            current[oid] = {**o, "offer_id": oid, "builder": b["name"], "source_url": url}
        self.stat(b["slug"], "offers_seen", len(current))
        before = {k: v for k, v in self.st.incentives.items()
                  if v.get("source_url") == url and v.get("active")}
        added = [v for k, v in current.items() if k not in self.st.incentives or not self.st.incentives[k].get("active")]
        removed = [v for k, v in before.items() if k not in current]
        pairs, gone, new = incentives.pair_changes(removed, added)
        for old_o, new_o in pairs:
            self.event(b, "offer_changed", {"source_url": url}, old_o.get("headline_amount"),
                       new_o.get("headline_amount"), f'{old_o["text"]}  ->  {new_o["text"]}')
        for o in gone:
            self.event(b, "offer_removed", {"source_url": url}, detail=o["text"])
        for o in new:
            self.event(b, "offer_new", {"source_url": url}, new=o.get("headline_amount"), detail=o["text"])
        self.stat(b["slug"], "offer_changes", len(pairs) + len(gone) + len(new))
        for o in removed:
            self.st.incentives[o["offer_id"]]["active"] = False
        for oid, o in current.items():
            rec = self.st.incentives.get(oid) or {"first_seen": now}
            rec.update({k: o.get(k) for k in ("offer_id", "builder", "kind", "headline_amount", "rate", "text",
                                              "source_url")})
            rec.update(last_seen=now, active=True)
            self.st.incentives[oid] = rec


# ------------------------------------------------------------------ crawling
def unwrap_json_html(r):
    """Some builder pages load their listings from a JSON endpoint that returns ready-made HTML
    ({"success": true, "html": "<div>..."}). Return a response whose text is that HTML."""
    t = r.text.lstrip()
    if not t.startswith("{"):
        return r
    try:
        data = json.loads(t)
    except ValueError:
        return r
    best = ""
    for d in [data] + [v for v in data.values() if isinstance(v, dict)]:
        for v in d.values():
            if isinstance(v, str) and "<a " in v and len(v) > len(best):
                best = v
    if not best:
        return r
    from .fetch import Response
    return Response(r.url, r.status, best + "<script type=application/json>" + t[:2_000_000] + "</script>")



def crawl_builder(b, client, rec: Recorder, budget: int):
    slug, now = b["slug"], rec.now
    pages = 0
    fr = rec.st.frontier
    notes = []

    def fetch(url):
        nonlocal pages
        if pages >= budget:
            return None
        pages += 1
        return client.get(url)

    def queue(url, kind="home"):
        cu = discover.canonical_url(url)
        if kind == "home" and NON_HOME_RE.search(cu):
            return
        with rec.lock:
            if cu in fr:
                return
            fr[cu] = {"url": cu, "builder": b["name"], "kind": kind, "discovered_at": ts(now), "misses": 0}

    def process_listing(r):
        """Record homes found on a listing/hub page; queue home and hub links. Returns pagination links."""
        r = unwrap_json_html(r)
        builder_geo.update({k: v for k, v in page_geo_index(r.text).items() if k not in builder_geo})
        homes = extract_homes(r.text, r.url)
        covered = set()
        for h in homes:
            if h.get("url") and not b["detail_re"].search(urlparse(h["url"]).path):
                h["url"] = ""  # a link to a community/plan page, not the home
            if h.get("url") and NON_HOME_RE.search(h["url"]):
                h["url"] = ""
            ua = address_from_url(h["url"]) if h.get("url") else ""
            if ua and h.get("address") and not same_address(ua, h["address"]):
                h["url"] = ""  # the link belongs to a different home
            rec.observe(b, h)
            if h.get("url"):
                covered.add(h["url"].rstrip("/").lower())
                queue(h["url"])
        cards = card_homes(r.text, r.url, b["detail_re"])
        fill_coords(cards, page_geo_index(r.text))
        for h in cards:
            if h["url"].rstrip("/").lower() not in covered:
                rec.observe(b, h)
        links, more = discover.links_from_html(r.text, r.url)
        for l in links:
            path = urlparse(l).path
            if b["detail_re"].search(path) and (b.get("dfw_only") or not b.get("filter_re")
                                                or b["filter_re"].search(l)):
                queue(l)
            elif b.get("hub_re") and b["hub_re"].search(path):
                queue(l, "hub")
        return more

    builder_geo: dict = {}   # address key -> coordinates seen on any of this builder's pages this run

    # 1) listing pages (+ pagination)
    seeds_ok = 0
    todo, done = list(b.get("seeds") or []), set()
    while todo and len(done) < len(b.get("seeds") or []) + MAX_PAGINATION:
        url = todo.pop(0)
        if url in done:
            continue
        done.add(url)
        r = fetch(url)
        if r is not None and r.status == 0:
            r = fetch(url)  # network hiccup (timeout/reset, not a refusal): one more try
        if not r or r.status != 200:
            notes.append(f"seed {r.status if r else 'skipped'}: {url}")
            continue
        seeds_ok += 1
        more = process_listing(r)
        todo.extend(p for p in more if p not in done)

    # 1b) hub pages (community / city pages that list homes), stalest first, within budget
    with rec.lock:
        hubs = [f for f in fr.values() if f.get("builder") == b["name"] and f.get("kind") == "hub"
                and int(f.get("misses") or 0) < 3]
    hubs.sort(key=lambda f: (bool(f.get("last_checked")), str(f.get("last_checked") or "")))
    hub_budget = pages + int(budget * 0.6)
    for f in hubs:
        if pages >= hub_budget:
            break
        r = fetch(f["url"])
        if r is None:
            if client._host(f["url"]) in client.blocked:
                break
            continue
        f["last_checked"], f["last_status"] = ts(now), r.status
        if r.status in (404, 410):
            f["misses"] = int(f.get("misses") or 0) + 1
            continue
        if r.status == 200:
            seeds_ok += 1
            process_listing(r)

    # 2) sitemap discovery, every few hours
    if b.get("sitemap"):
        key = f'sitemap:{b["site"]}'
        with rec.lock:
            last = parse_ts((fr.get(key) or {}).get("last_checked"))
        if not last or now - last >= timedelta(hours=SITEMAP_EVERY_H):
            before = client.requests_made
            for u in discover.discover_from_sitemaps(client, b["site"], b["detail_re"],
                                                     None if b.get("dfw_only") else b.get("filter_re"),
                                                     roots=b.get("sitemaps")):
                queue(u)
            if b.get("hub_re"):
                for u in discover.discover_from_sitemaps(client, b["site"], b["hub_re"], b.get("filter_re"),
                                                         max_sitemaps=10, roots=b.get("sitemaps")):
                    queue(u, "hub")
            pages += client.requests_made - before
            with rec.lock:
                fr[key] = {"url": key, "builder": b["name"], "kind": "sitemap", "discovered_at": ts(now),
                           "last_checked": ts(now), "misses": 0}

    # 3) home pages: never-checked first, then stalest
    with rec.lock:
        homes = [f for f in fr.values() if f.get("builder") == b["name"] and f.get("kind") == "home"
                 and int(f.get("misses") or 0) < 3]
    homes.sort(key=lambda f: (bool(f.get("last_checked")), str(f.get("last_checked") or "")))
    reachable = seeds_ok > 0
    for f in homes:
        if pages >= budget:
            break
        r = fetch(f["url"])
        if r is None:
            if client._host(f["url"]) in client.blocked:
                break
            continue
        f["last_checked"], f["last_status"] = ts(now), r.status
        if r.status in (404, 410):
            f["misses"] = int(f.get("misses") or 0) + 1
            continue
        if r.status != 200:
            continue
        reachable = True
        found = extract_homes(r.text, f["url"], single=True)
        if found:
            f["misses"] = 0
            rec.observe(b, found[0])
        else:
            f["misses"] = int(f.get("misses") or 0) + 1  # page no longer shows a home for sale

    # 4) promotions
    for url in b.get("promos") or []:
        r = client.get(url)
        pages += 1
        if r and r.status == 200:
            rec.offers(b, url, incentives.extract_offers(r.text))

    # map pins listed on a different page than the home itself (e.g. a map-marker list)
    with rec.lock:
        for h in rec.st.homes.values():
            if h.get("builder") == b["name"] and h.get("location_precision") != "builder" and h.get("address"):
                c = builder_geo.get(address_key(h["address"]))
                if c:
                    h.update(lat=c[0], lng=c[1], location_precision="builder")

    if reachable:
        rec.retire_unseen(b)
    blocked = client.blocked.get(client._host(b["site"]))
    status = "blocked" if blocked else ("ok" if reachable else "no data")
    if blocked:
        notes.insert(0, blocked)
    return pages, status, "; ".join(notes)[:500]


def derived_tabs(st: State, new_changes: list, now: datetime, runlog: list, builders: list) -> dict:
    allc = st.changes + new_changes
    d7, d1, d90 = now - timedelta(days=7), now - timedelta(days=1), now - timedelta(days=90)
    drops = []
    for c in allc:
        at = parse_ts(c.get("at"))
        if c.get("event") == "price_cut" and at and at >= d90:
            row = {k: c.get(k) for k in ("at", "builder", "address", "community", "city", "old", "new",
                                         "delta", "pct", "url")}
            row["offer"] = (st.homes.get(c.get("home_id")) or {}).get("incentive_text", "")
            drops.append(row)
    drops.sort(key=lambda r: r["at"], reverse=True)
    last_run = {r["builder"]: r for r in runlog}
    summary_baseline = {}   # a builder's first 24h of tracking is its baseline, not new listings
    for c in allc:
        if c.get("event") == "new_home" and parse_ts(c.get("at")):
            t = parse_ts(c["at"])
            summary_baseline[c["builder"]] = min(summary_baseline.get(c["builder"], t + timedelta(hours=24)),
                                                 t + timedelta(hours=24))
    summary = []
    for b in builders:
        n = b["name"]
        cuts7 = [c for c in allc if c.get("builder") == n and c.get("event") == "price_cut"
                 and (parse_ts(c.get("at")) or d90) >= d7]
        cuts1 = [c for c in cuts7 if (parse_ts(c.get("at")) or d90) >= d1]
        amounts = [-(c.get("delta") or 0) for c in cuts7]
        lr = last_run.get(n, {})
        summary.append({
            "builder": n,
            "active_homes": sum(1 for h in st.homes.values() if h.get("builder") == n and h.get("active")),
            "price_cuts_24h": len(cuts1), "price_cuts_7d": len(cuts7),
            "total_cut_7d": sum(amounts) or None,
            "avg_cut_7d": round(sum(amounts) / len(amounts)) if amounts else None,
            "biggest_cut_7d": max(amounts) if amounts else None,
            "new_homes_7d": sum(1 for c in allc if c.get("builder") == n and c.get("event") == "new_home"
                                and (parse_ts(c.get("at")) or d90) >= d7
                                and (parse_ts(c.get("at")) or d90) > summary_baseline.get(n, d90)),
            "removed_7d": sum(1 for c in allc if c.get("builder") == n and c.get("event") == "removed"
                              and (parse_ts(c.get("at")) or d90) >= d7),
            "active_offers": sum(1 for o in st.incentives.values() if o.get("builder") == n and o.get("active")),
            "last_run": lr.get("run_at", ""), "last_run_status": lr.get("status", ""),
        })
    offers = [{k: h.get(k) for k in ("builder", "address", "community", "city", "price", "change_since_first",
                                      "offer_type", "offer_amount", "offer_rate", "incentive_text", "offer_since",
                                      "url")}
              for h in st.homes.values() if h.get("active") and h.get("incentive_text")]
    offers.sort(key=lambda r: (-(r.get("offer_amount") or 0), r.get("builder") or ""))
    d30 = now - timedelta(days=30)
    new_homes, gone = [], []
    # the first run of each builder records everything as new; that's the baseline, not new listings
    # A builder's first 24 hours of tracking are its baseline: homes found then were already for sale
    # (large sites are discovered over several runs), so they aren't listed as new.
    first_run = {}
    for c in allc:
        if c.get("event") == "new_home" and c.get("at"):
            first_run[c["builder"]] = min(first_run.get(c["builder"], c["at"]), c["at"])
    baseline = {k: parse_ts(v) + timedelta(hours=24) for k, v in first_run.items() if parse_ts(v)}
    for c in allc:
        at = parse_ts(c.get("at"))
        if not at:
            continue
        h = st.homes.get(c.get("home_id")) or {}
        if c.get("event") == "new_home" and at >= d30 and at > baseline.get(c.get("builder"), at - timedelta(1)):
            new_homes.append({"at": c["at"], "builder": c.get("builder"), "address": c.get("address"),
                              "community": c.get("community"), "city": c.get("city"), "price": c.get("new"),
                              "beds": h.get("beds"), "baths": h.get("baths"), "sqft": h.get("sqft"),
                              "incentive_text": h.get("incentive_text"), "url": c.get("url")})
        elif c.get("event") == "removed" and at >= d90 and not h.get("active"):
            first = parse_ts(h.get("first_seen"))
            fp, lp = h.get("first_price"), c.get("old") or h.get("price")
            gone.append({"at": c["at"], "builder": c.get("builder"), "address": c.get("address"),
                         "community": c.get("community"), "city": c.get("city"), "last_price": lp,
                         "first_price": fp, "total_change": (lp - fp) if lp and fp else None,
                         "days_listed": (at - first).days if first else None, "cuts": h.get("cuts"),
                         "url": c.get("url")})
    new_homes.sort(key=lambda r: r["at"], reverse=True)
    gone.sort(key=lambda r: r["at"], reverse=True)
    return {"Summary": summary, "Price Drops": drops, "Home Offers": offers, "New Homes": new_homes,
            "Sold / Removed": gone}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="builders.yaml")
    ap.add_argument("--only", nargs="*", help="builder slugs to run")
    ap.add_argument("--budget", type=int, default=int(os.environ.get("PAGES_PER_BUILDER", 120)))
    ap.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", 8)))
    ap.add_argument("--delay", type=float, default=float(os.environ.get("REQUEST_DELAY", 2.0)))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    builders = load_config(args.config)
    selected = [b for b in builders if not args.only or b["slug"] in args.only]
    ua = os.environ.get("CRAWLER_USER_AGENT") or (
                        "DFWBuilderTracker/1.0 (+https://github.com/Crutledge13/dfw-builder-tracker)")
    store = store_from_env()
    st = store.load()
    now = now_local()
    rec = Recorder(st, now)
    total_requests = 0

    def one(b):
        t0 = time.time()
        client = PoliteClient(ua, min_delay=args.delay)  # one client per builder = per-site pacing
        try:
            pages, status, note = crawl_builder(b, client, rec, args.budget)
        except Exception as e:  # one builder failing must not stop the others
            log.exception("builder %s failed", b["slug"])
            pages, status, note = 0, "error", f"{e.__class__.__name__}: {e}"[:500]
        s = rec.stats.get(b["slug"], {})
        log.info("%s: %s pages, %s homes, %.0fs, %s", b["name"], pages, s.get("homes_seen", 0),
                 time.time() - t0, status)
        return client.requests_made, {
            "run_at": ts(now), "builder": b["name"], "pages": client.requests_made,
            "homes_seen": s.get("homes_seen", 0), "new_homes": s.get("new_homes", 0),
            "price_cuts": s.get("price_cuts", 0), "price_increases": s.get("price_increases", 0),
            "offers_seen": s.get("offers_seen", 0), "offer_changes": s.get("offer_changes", 0),
            "status": status, "note": note}

    runlog = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, row in pool.map(one, selected):
            total_requests += n
            runlog.append(row)
    if os.environ.get("GEOCODE", "1") != "0":
        try:
            from .geocode import fill_missing
            log.info("geocoding: %s", fill_missing(st.homes, now, FMT, ua))
        except Exception:
            log.exception("geocoding failed; continuing without it")
    store.save(st, rec.changes, runlog, derived_tabs(st, rec.changes, now, runlog, builders))
    log.info("done: %d changes recorded, %d requests", len(rec.changes), total_requests)


if __name__ == "__main__":
    main()
