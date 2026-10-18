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
from .extract import DFW_CITY_RE, address_key, extract_homes, to_price
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


def load_config(path="builders.yaml") -> list[dict]:
    cfg = yaml.safe_load(open(path))
    d = cfg.get("defaults", {})
    out = []
    for b in cfg["builders"]:
        bb = {**d, **b}
        bb["detail_re"] = re.compile(bb["detail_pattern"], re.I)
        bb["filter_re"] = re.compile(bb["sitemap_filter"], re.I) if bb.get("sitemap_filter") else None
        out.append(bb)
    return out


# ------------------------------------------------------------------ recording
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
            return f'{b["slug"]}:{address_key(obs["address"])}'
        if obs.get("url"):
            return f'{b["slug"]}:{urlparse(obs["url"]).path.strip("/").lower()}'
        return None

    @locked
    def observe(self, b, obs):
        if not b.get("dfw_only") and not in_dfw(obs) and not (
                obs.get("url") and b.get("filter_re") and b["filter_re"].search(obs["url"])):
            return
        hid = self.home_id(b, obs)
        if not hid or not obs.get("price"):
            return
        self.stat(b["slug"], "homes_seen")
        now = ts(self.now)
        price = int(obs["price"])
        h = self.st.homes.get(hid)
        if h is None:
            h = {"home_id": hid, "builder": b["name"], "first_price": price, "price": price, "cuts": 0,
                 "first_seen": now, "active": True}
            self.st.homes[hid] = h
            for k in ("community", "city", "zip", "address", "plan", "beds", "baths", "sqft", "status", "move_in",
                      "url", "builder_was_price", "incentive_text"):
                if obs.get(k) not in (None, ""):
                    h[k] = obs[k]
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
            for k in ("status", "move_in", "builder_was_price"):
                if obs.get(k) not in (None, ""):
                    if k == "status" and h.get(k) and obs[k].lower() != str(h[k]).lower():
                        self.event(b, "status_change", h, detail=f'{h[k]} -> {obs[k]}')
                    h[k] = obs[k]
            if "incentive_text" in obs:
                new_inc = obs.get("incentive_text") or ""
                if new_inc and h.get("incentive_text") and new_inc != h["incentive_text"]:
                    self.event(b, "home_offer_changed", h, detail=new_inc)
                if new_inc:
                    h["incentive_text"] = new_inc
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
        with rec.lock:
            if cu in fr:
                return
            fr[cu] = {"url": cu, "builder": b["name"], "kind": kind, "discovered_at": ts(now), "misses": 0}

    # 1) listing pages (+ pagination)
    seeds_ok = 0
    todo, done = list(b.get("seeds") or []), set()
    while todo and len(done) < len(b.get("seeds") or []) + MAX_PAGINATION:
        url = todo.pop(0)
        if url in done:
            continue
        done.add(url)
        r = fetch(url)
        if not r or r.status != 200:
            notes.append(f"seed {r.status if r else 'skipped'}: {url}")
            continue
        seeds_ok += 1
        for h in extract_homes(r.text, r.url):
            if h.get("url") and not b["detail_re"].search(urlparse(h["url"]).path):
                h["url"] = ""  # a link to a community/plan page, not the home
            rec.observe(b, h)
            if h.get("url"):
                queue(h["url"])
        links, more = discover.links_from_html(r.text, r.url)
        for l in links:
            if b["detail_re"].search(urlparse(l).path) and (b.get("dfw_only") or not b.get("filter_re")
                                                             or b["filter_re"].search(l)):
                queue(l)
        todo.extend(p for p in more if p not in done)

    # 2) sitemap discovery, every few hours
    if b.get("sitemap"):
        key = f'sitemap:{b["site"]}'
        with rec.lock:
            last = parse_ts((fr.get(key) or {}).get("last_checked"))
        if not last or now - last >= timedelta(hours=SITEMAP_EVERY_H):
            before = client.requests_made
            for u in discover.discover_from_sitemaps(client, b["site"], b["detail_re"],
                                                     None if b.get("dfw_only") else b.get("filter_re")):
                queue(u)
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
            drops.append({k: c.get(k) for k in ("at", "builder", "address", "community", "city", "old", "new",
                                                 "delta", "pct", "url")})
    drops.sort(key=lambda r: r["at"], reverse=True)
    last_run = {r["builder"]: r for r in runlog}
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
                                and (parse_ts(c.get("at")) or d90) >= d7),
            "removed_7d": sum(1 for c in allc if c.get("builder") == n and c.get("event") == "removed"
                              and (parse_ts(c.get("at")) or d90) >= d7),
            "active_offers": sum(1 for o in st.incentives.values() if o.get("builder") == n and o.get("active")),
            "last_run": lr.get("run_at", ""), "last_run_status": lr.get("status", ""),
        })
    return {"Summary": summary, "Price Drops": drops}


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
    store.save(st, rec.changes, runlog, derived_tabs(st, rec.changes, now, runlog, builders))
    log.info("done: %d changes recorded, %d requests", len(rec.changes), total_requests)


if __name__ == "__main__":
    main()
