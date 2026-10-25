"""JSON API for websites: docs/data/api/*.json, rewritten every run and served by GitHub Pages
(public, CORS-enabled), e.g. https://crutledge13.github.io/dfw-builder-tracker/data/api/homes.json"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")

HOME_FIELDS = ["builder", "community", "city", "zip", "address", "plan", "beds", "baths", "sqft", "price",
               "first_price", "prev_price", "change_since_first", "pct_since_first", "cuts", "last_change",
               "last_change_at", "builder_was_price", "price_per_sqft", "status", "move_in", "url", "first_seen",
               "last_seen", "lat", "lng", "location_precision"]


def _clean(v):
    if v == "" or v is None:
        return None
    return v


def iso(ts):
    """'2026-10-19 08:19' (Central) -> '2026-10-19T08:19:00-05:00' so browsers can compute 'x hours ago'."""
    try:
        return datetime.strptime(str(ts), "%Y-%m-%d %H:%M").replace(tzinfo=TZ).isoformat()
    except (TypeError, ValueError):
        return None


def money_k(n) -> str:
    n = abs(int(n))
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M".replace(".00M", "M")
    if n >= 1000:
        return f"${round(n / 1000):,}k"
    return f"${n:,}"


def move_in_label(status, move_in) -> str | None:
    """One wording across builders: 'Ready now', 'Under construction', 'Pending', 'Ready Nov 2026', …"""
    t = f"{status or ''} {move_in or ''}".lower()
    if re.search(r"under contract|pending|\bsold\b", t):
        return "Pending"
    if re.search(r"move-?\s?in ready|ready now|available now|\bnow\b|complete", t):
        return "Ready now"
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{1,2}(?!\d),?\s*)?(20\d\d)?", t)
    if m:
        return "Ready " + m.group(1).title() + (" " + m.group(3) if m.group(3) else "")
    if re.search(r"under construction|in progress|framing|coming soon|to be built", t):
        return "Under construction"
    if re.search(r"available|active", t):
        return "Available"
    return None


PRICE_CUT_RE = re.compile(r"(\boff\b|reduc|price drop|markdown|was \$|now \$|discount)", re.I)


def incentive_value(text, price, skip_price_cuts=False) -> int | None:
    """Approximate dollar value of an advertised per-home offer: dollar amounts, plus '% toward closing'
    style offers converted with the home's price. Rate buydowns without a dollar figure count as 0."""
    if not text:
        return None
    seen, total = set(), 0
    for part in text.split(" | "):
        if skip_price_cuts and PRICE_CUT_RE.search(part):
            continue   # already reflected in builder_was_price - price
        amt = None
        dollars = []
        for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kKmM])?", part):
            n = float(m.group(1).replace(",", ""))
            n *= 1000 if (m.group(2) or "").lower() == "k" else 1_000_000 if (m.group(2) or "").lower() == "m" else 1
            if 500 <= n <= 300_000:
                dollars.append(int(n))
        if dollars:
            amt = max(dollars)
        elif price:
            m = re.search(r"(\d+(?:\.\d+)?)\s?%\s*(?:of|towards?|to|seller|in)\b.{0,25}(closing|contribution|seller|price|options|upgrades)",
                          part, re.I)
            if m and float(m.group(1)) <= 10:
                amt = int(round(float(m.group(1)) / 100 * price, -2))
        if amt and amt not in seen:
            seen.add(amt)
            total += amt
    return total or None


def _home(h: dict) -> dict:
    out = {"id": h.get("home_id")}
    for k in HOME_FIELDS:
        out["previous_price" if k == "prev_price" else k] = _clean(h.get(k))
    out["offer"] = None
    if h.get("incentive_text"):
        out["offer"] = {"text": h["incentive_text"], "type": _clean(h.get("offer_type")),
                        "amount": _clean(h.get("offer_amount")), "rate": _clean(h.get("offer_rate")),
                        "value": incentive_value(h["incentive_text"], h.get("price")),
                        "since": _clean(h.get("offer_since"))}
    return out


def add_card(out: dict, stats: dict):
    """Price-drop totals from the full change history, savings, move-in, and ready-to-show labels."""
    price, first = out.get("price"), out.get("first_price")
    out["total_drop"] = max(0, first - price) if first and price else 0          # since first tracked
    out["total_drop_pct"] = round(out["total_drop"] / first * 100, 2) if first and out["total_drop"] else 0
    out["cuts"] = stats.get("cuts", 0)
    out["largest_cut"] = stats.get("largest_cut")
    out["last_cut"] = stats.get("last_cut")
    out["last_cut_at"] = stats.get("last_cut_at")
    out["increases"] = stats.get("increases", 0)
    was = out.get("builder_was_price")
    out["builder_discount"] = was - price if was and price and was > price else None   # builder's own markdown
    ov = None
    if out.get("offer"):
        # a "$35K off" label restates the was-price markdown, so only count it when no markdown is shown
        ov = incentive_value(out["offer"]["text"], price, skip_price_cuts=bool(out["builder_discount"]))
        out["offer"]["value"] = ov
    out["total_savings"] = ((out["builder_discount"] or 0) + (ov or 0)) or None
    out["move_in_label"] = move_in_label(out.get("status"), out.get("move_in"))
    out["checked_at"] = iso(out.get("last_seen"))
    out["first_seen_at"] = iso(out.get("first_seen"))
    out["card"] = {
        "price_drop": f"Down {money_k(out['total_drop'])} since first tracked" if out["total_drop"] else None,
        "cuts": (f"{out['cuts']} cut" + ("s" if out["cuts"] != 1 else "")) if out["cuts"] else None,
        "savings": f"{money_k(out['total_savings'])} incentive" if out["total_savings"] else None,
        "move_in": out["move_in_label"],
        "beds": f"{out['beds']:g} bd" if out.get("beds") else None,
        "baths": f"{out['baths']:g} ba" if out.get("baths") else None,
        "sqft": f"{out['sqft']:,} sq ft" if out.get("sqft") else None,
    }
    return out


ALL_HOMES_RE = re.compile(r"\b(all|every|any)\s+(specs?|homes?|quick move-?ins?|qmis?|inventory|available homes)\b|"
                          r"\bon all\b|\bany lender\b", re.I)
PER_HOME_ONLY = re.compile(r"^\$[\d,.]+\s?k?\s*off$", re.I)   # "$62K off" on a listing page = one home's markdown
OFFER_ORDER = {"rate": 0, "closing_costs": 1, "cash_credit": 2, "price_reduction": 3, "other": 4}


def _norm_offer(t: str) -> str:
    return re.sub(r"[^a-z0-9$%]", "", (t or "").lower())


def builder_profiles(st, homes: dict, derived: dict, run_at: str) -> list[dict]:
    """One record per builder: inventory, prices, activity, blanket (builder-wide) offers and the offers
    that appear on most of its homes."""
    import statistics
    try:
        import yaml
        cfg = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "builders.yaml"))["builders"]
    except Exception:
        cfg = []
    meta = {b["name"]: b for b in cfg}
    summary = {r["builder"]: r for r in derived.get("Summary", [])}
    names = list(dict.fromkeys([b["name"] for b in cfg] + sorted({h["builder"] for h in homes.values()})))

    out = []
    for name in names:
        hs = [h for h in homes.values() if h["builder"] == name]
        prices = sorted(h["price"] for h in hs if h.get("price"))
        ppsf = [h["price_per_sqft"] for h in hs if h.get("price_per_sqft")]
        disc = [h["builder_discount"] for h in hs if h.get("builder_discount")]
        b = meta.get(name, {})
        sm = summary.get(name, {})

        # blanket offers from the builder's promotions pages, de-duplicated
        blanket, seen = [], set()
        for o in sorted((o for o in st.incentives.values() if o.get("builder") == name and o.get("active")),
                        key=lambda o: (OFFER_ORDER.get(o.get("kind"), 9), -(o.get("headline_amount") or 0))):
            text = (o.get("text") or "").strip()
            if not text or PER_HOME_ONLY.match(text):
                continue
            key = (o.get("kind"), o.get("headline_amount") or None, o.get("rate") or None)
            if key in seen or _norm_offer(text) in seen:
                continue
            seen.update({key, _norm_offer(text)})
            blanket.append({"type": o.get("kind"), "amount": _clean(o.get("headline_amount")),
                            "rate": _clean(o.get("rate")), "text": text, "source_url": o.get("source_url"),
                            "first_seen": iso(o.get("first_seen")), "last_seen": iso(o.get("last_seen"))})

        # offers repeated across many of the builder's homes = a de-facto blanket incentive
        counts: dict = {}
        for h in hs:
            for part in ((h.get("offer") or {}).get("text") or "").split(" | "):
                part = part.strip()
                if part and not PER_HOME_ONLY.match(part):
                    k = _norm_offer(part)
                    c = counts.setdefault(k, {"text": part, "homes": 0})
                    c["homes"] += 1
        from .incentives import classify, headline_amount, lowest_rate
        common = []
        kept = {(o["type"], o["rate"], o["amount"]) for o in blanket} | {_norm_offer(o["text"]) for o in blanket}
        for c in sorted(counts.values(), key=lambda c: -c["homes"]):
            share = c["homes"] / len(hs) if hs else 0
            says_all = bool(ALL_HOMES_RE.search(c["text"]))
            if not (c["homes"] >= 3 and (share >= 0.05 or says_all)):
                continue
            o = {"text": c["text"], "type": classify(c["text"]), "amount": headline_amount(c["text"]),
                 "rate": lowest_rate(c["text"]), "homes": c["homes"], "share": round(share, 2),
                 "says_all_homes": says_all}
            key = (o["type"], o["rate"], o["amount"])
            vague = o["rate"] is None and o["amount"] is None and any(k[0] == o["type"] for k in kept if isinstance(k, tuple))
            if key in kept or _norm_offer(o["text"]) in kept or vague:
                for b2 in blanket:   # same offer as a promotions-page one: just note how many homes show it
                    if (b2["type"], b2["rate"], b2["amount"]) == key:
                        b2["homes"] = max(b2.get("homes") or 0, c["homes"])
                continue
            kept.update({key, _norm_offer(o["text"])})
            common.append(o)
            if len(common) == 3:
                break

        allo = blanket + common
        rates = [o["rate"] for o in allo if o.get("rate")]
        cash = [o["amount"] for o in allo if o.get("amount") and o.get("type") in ("closing_costs", "cash_credit", "price_reduction")]
        bits = []
        if rates:
            bits.append(f"Rates as low as {min(rates):g}%")
        if cash:
            kinds = {o["type"] for o in allo if o.get("amount") == max(cash)}
            label = "closing costs" if "closing_costs" in kinds else "flex cash" if "cash_credit" in kinds else "savings"
            bits.append(f"Up to {money_k(max(cash))} {label}")
        if not bits and allo:
            bits.append(allo[0]["text"][:80])

        out.append({
            "builder": name, "slug": b.get("slug"), "website": b.get("site"),
            "status": sm.get("last_run_status") or ("ok" if hs else "no data"),
            "checked_at": iso(sm.get("last_run")) or iso(run_at),
            "headline_offer": " · ".join(bits) or None,
            "blanket_offers": blanket,
            "common_home_offers": common,
            "homes": {
                "active": len(hs),
                "with_offer": sum(1 for h in hs if h.get("offer")),
                "with_price_cut": sum(1 for h in hs if h.get("cuts")),
                "with_builder_discount": len(disc),
                "ready_now": sum(1 for h in hs if h.get("move_in_label") == "Ready now"),
                "under_construction": sum(1 for h in hs if h.get("move_in_label") == "Under construction"),
                "pending": sum(1 for h in hs if h.get("move_in_label") == "Pending"),
            },
            "prices": {
                "min": prices[0] if prices else None, "max": prices[-1] if prices else None,
                "median": int(statistics.median(prices)) if prices else None,
                "median_per_sqft": int(statistics.median(ppsf)) if ppsf else None,
                "avg_builder_discount": int(statistics.mean(disc)) if disc else None,
                "max_total_savings": max((h.get("total_savings") or 0) for h in hs) or None if hs else None,
            },
            "activity": {
                "price_cuts_24h": sm.get("price_cuts_24h") or 0, "price_cuts_7d": sm.get("price_cuts_7d") or 0,
                "total_cut_7d": sm.get("total_cut_7d") or 0, "biggest_cut_7d": sm.get("biggest_cut_7d"),
                "new_homes_7d": sm.get("new_homes_7d") or 0, "sold_removed_7d": sm.get("removed_7d") or 0,
            },
            "cities": [c for c, _ in __import__("collections").Counter(h["city"] for h in hs if h.get("city")).most_common()],
            "communities": len({h["community"] for h in hs if h.get("community")}),
        })
    out.sort(key=lambda r: -r["homes"]["active"])
    return out


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}


def _completion_month(label: str | None, move_in: str | None):
    """(year, month) from 'Ready Nov 2026' / 'Est. completion Oct 2026' style text, else None."""
    t = f"{label or ''} {move_in or ''}".lower()
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(?:\d{1,2}(?!\d),?\s*)?(20\d\d)?", t)
    if not m:
        return None
    return int(m.group(2)) if m.group(2) else None, MONTHS[m.group(1)]


def this_week(st, homes: dict, run_at: str, days: int = 7) -> dict:
    """Everything that changed in the last 7 days, in sections ready for a 'This week' page."""
    from collections import Counter, defaultdict
    now = datetime.strptime(run_at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ) if run_at else datetime.now(TZ)
    start = now - __import__("datetime").timedelta(days=days)

    def when(c):
        try:
            return datetime.strptime(c.get("at") or "", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError:
            return None

    # each builder's first day of tracking is its baseline, not new inventory
    first = {}
    for c in st.changes:
        t = when(c)
        if c.get("event") == "new_home" and t:
            first[c.get("builder")] = min(first.get(c.get("builder"), t), t)
    baseline_end = {b: t + __import__("datetime").timedelta(hours=24) for b, t in first.items()}

    week = [c for c in st.changes if (when(c) or start) > start]
    all_by_id = {h.get("home_id"): h for h in st.homes.values()}

    def home_bits(hid, url=None):
        h = homes.get(hid)
        if h is None and url:
            h = next((x for x in homes.values() if x.get("url") == url), None)
        return h or {}

    cuts = [c for c in week if c.get("event") == "price_cut" and c.get("old") and c.get("new")]
    incs = [c for c in week if c.get("event") == "price_increase"]
    new = [c for c in week if c.get("event") == "new_home" and when(c) > baseline_end.get(c.get("builder"), when(c))
           and not str(c.get("detail") or "").startswith("baseline")]
    gone = [c for c in week if c.get("event") == "removed"]
    offer_events = [c for c in week if c.get("event") in ("offer_new", "offer_changed", "home_offer_new",
                                                          "home_offer_changed", "offer_removed")]

    def drop_row(c):
        h = home_bits(c.get("home_id"), c.get("url"))
        old, new_ = int(c["old"]), int(c["new"])
        return {"home_id": c.get("home_id"), "address": c.get("address"), "city": c.get("city"),
                "community": c.get("community"), "builder": c.get("builder"), "detected_at": iso(c.get("at")),
                "old_price": old, "new_price": new_, "drop": old - new_, "drop_pct": round((old - new_) / old * 100, 1),
                "current_price": h.get("price"), "total_drop": h.get("total_drop"), "cuts": h.get("cuts"),
                "beds": h.get("beds"), "baths": h.get("baths"), "sqft": h.get("sqft"),
                "move_in": h.get("move_in_label"), "lat": h.get("lat"), "lng": h.get("lng"),
                "card": h.get("card"), "url": c.get("url") or h.get("url")}

    biggest = sorted((drop_row(c) for c in cuts), key=lambda r: -r["drop"])

    # offers: builder-wide offers new/changed this week, plus per-home offers grouped by builder + text
    offers = []
    for c in offer_events:
        if c.get("event") in ("offer_new", "offer_changed"):
            text = (c.get("detail") or "").split("  ->  ")[-1]
            offers.append({"kind": "builder_offer", "change": "new" if c["event"] == "offer_new" else "changed",
                           "builder": c.get("builder"), "text": text,
                           "previous_text": (c.get("detail") or "").split("  ->  ")[0] if c["event"] == "offer_changed" else None,
                           "amount": c.get("new"), "detected_at": iso(c.get("at")), "homes": None,
                           "source_url": c.get("url")})
    grouped = defaultdict(lambda: {"homes": 0, "last": None, "cities": Counter(), "communities": set(), "prev": Counter()})
    for c in offer_events:
        if c.get("event") in ("home_offer_new", "home_offer_changed"):
            text = (c.get("detail") or "").split("  ->  ")[-1]
            g = grouped[(c.get("builder"), text, c["event"])]
            g["homes"] += 1
            g["last"] = max(g["last"] or c["at"], c["at"])
            if "  ->  " in (c.get("detail") or ""):
                g["prev"][c["detail"].split("  ->  ")[0]] += 1
            if c.get("city"):
                g["cities"][c["city"]] += 1
            if c.get("community"):
                g["communities"].add(c["community"])
    for (b, text, ev), g in grouped.items():
        offers.append({"kind": "home_offer", "change": "new" if ev == "home_offer_new" else "changed",
                       "builder": b, "text": text,
                       "previous_text": g["prev"].most_common(1)[0][0] if g["prev"] else None, "amount": None,
                       "detected_at": iso(g["last"]), "homes": g["homes"],
                       "cities": [c for c, _ in g["cities"].most_common(3)],
                       "communities": len(g["communities"]), "source_url": None})
    offers.sort(key=lambda o: (o["detected_at"] or ""), reverse=True)

    new_rows = []
    for c in sorted(new, key=lambda c: c.get("at") or "", reverse=True):
        h = home_bits(c.get("home_id"), c.get("url"))
        if not h:
            continue
        new_rows.append({"home_id": h.get("id"), "address": h.get("address"), "city": h.get("city"),
                         "community": h.get("community"), "builder": h.get("builder"), "price": h.get("price"),
                         "beds": h.get("beds"), "baths": h.get("baths"), "sqft": h.get("sqft"),
                         "move_in": h.get("move_in_label"), "has_offer": bool(h.get("offer")),
                         "first_seen_at": iso(c.get("at")), "checked_at": h.get("checked_at"),
                         "lat": h.get("lat"), "lng": h.get("lng"), "card": h.get("card"), "url": h.get("url")})

    gone_rows = []
    for c in sorted(gone, key=lambda c: c.get("at") or "", reverse=True):
        h = all_by_id.get(c.get("home_id")) or {}
        fs = h.get("first_seen")
        days_listed = None
        try:
            days_listed = (when(c) - datetime.strptime(fs, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)).days
        except (TypeError, ValueError):
            pass
        last = c.get("old") or h.get("price")
        gone_rows.append({"home_id": c.get("home_id"), "address": c.get("address"), "city": c.get("city"),
                          "community": c.get("community"), "builder": c.get("builder"), "last_price": last,
                          "first_price": h.get("first_price"),
                          "total_drop": (h.get("first_price") - last) if h.get("first_price") and last and h.get("first_price") > last else 0,
                          "cuts": sum(1 for x in st.changes if x.get("home_id") == c.get("home_id") and x.get("event") == "price_cut"),
                          "days_tracked": days_listed, "detected_at": iso(c.get("at")), "url": c.get("url")})

    # past estimated completion: still listed after the month the builder said it would be finished
    past = []
    for h in homes.values():
        ym = _completion_month(h.get("move_in_label"), h.get("move_in"))
        if not ym or h.get("move_in_label") in ("Ready now", "Pending"):
            continue
        y, m = ym
        y = y or now.year
        if (y, m) < (now.year, now.month):
            past.append({"home_id": h.get("id"), "address": h.get("address"), "city": h.get("city"),
                         "community": h.get("community"), "builder": h.get("builder"), "price": h.get("price"),
                         "estimated_completion": f"{y}-{m:02d}", "move_in": h.get("move_in_label"),
                         "total_drop": h.get("total_drop"), "cuts": h.get("cuts"), "checked_at": h.get("checked_at"),
                         "card": h.get("card"), "url": h.get("url")})
    past.sort(key=lambda r: (r["estimated_completion"], -(r["total_drop"] or 0)))

    # rankings
    new_ids = {id(c) for c in new}
    COUNTED = {"price_cut", "price_increase", "removed", "status_change", "offer_new", "offer_changed",
               "home_offer_new", "home_offer_changed", "offer_removed"}

    def rank(key_fn, label_fn):
        agg = defaultdict(lambda: {"changes": 0, "price_cuts": 0, "total_cut": 0, "new_homes": 0, "sold_removed": 0,
                                   "offer_changes": 0})
        for c in week:
            ev, k = c.get("event"), key_fn(c)
            if not k or not (ev in COUNTED or id(c) in new_ids):
                continue
            a = agg[k]
            a["changes"] += 1
            if ev == "price_cut" and c.get("old") and c.get("new"):
                a["price_cuts"] += 1
                a["total_cut"] += int(c["old"]) - int(c["new"])
            elif ev == "new_home":
                a["new_homes"] += 1
            elif ev == "removed":
                a["sold_removed"] += 1
            elif "offer" in ev:
                a["offer_changes"] += 1
        rows = [label_fn(k) | v for k, v in agg.items()]
        return sorted(rows, key=lambda r: (-r["changes"], -r["total_cut"]))

    def comm_label(k):
        b, comm, city = k
        hs = [h for h in homes.values() if h.get("builder") == b and h.get("community") == comm]
        return {"community": comm, "builder": b, "city": city, "homes_available": len(hs),
                "ready_now": sum(1 for h in hs if h.get("move_in_label") == "Ready now"),
                "with_offer": sum(1 for h in hs if h.get("offer"))}

    communities = rank(lambda c: (c.get("builder"), c.get("community"), c.get("city")) if c.get("community") else None,
                       comm_label)
    builders = rank(lambda c: c.get("builder"),
                    lambda b: {"builder": b, "homes_available": sum(1 for h in homes.values() if h.get("builder") == b)})
    cities = rank(lambda c: c.get("city"),
                  lambda city: {"city": city, "homes_available": sum(1 for h in homes.values() if h.get("city") == city)})

    total_cut = sum(r["drop"] for r in biggest)
    return {
        "range": {"start": start.isoformat(), "end": now.isoformat(), "days": days},
        "generated_at": iso(run_at),
        "stats": {
            "price_cuts": len(cuts), "total_cut": total_cut,
            "average_cut": round(total_cut / len(cuts)) if cuts else None,
            "biggest_cut": biggest[0]["drop"] if biggest else None,
            "price_increases": len(incs),
            "new_homes": len(new_rows), "sold_removed": len(gone_rows),
            "offer_changes": len(offers),
            "homes_tracked": len(homes),
            "homes_with_offer": sum(1 for h in homes.values() if h.get("offer")),
            "builders_with_cuts": len({r["builder"] for r in biggest}),
        },
        "biggest_cuts": biggest[:25],
        "offer_changes": offers[:25],
        "new_homes": new_rows[:25],
        "sold_removed": gone_rows[:25],
        "past_completion": past[:25],
        "most_active_communities": communities[:15],
        "builders": builders,
        "cities": cities[:25],
        "notes": "Price changes are measured from when this tracker first saw a home (tracking began 2026-10-18). "
                 "Each builder's first day of tracking counts as its baseline, not as new inventory. "
                 "'sold_removed' = homes that left the builder's site (usually sold or under contract).",
    }


def _short_offer(text: str | None) -> str | None:
    """First offer phrase, trimmed for a listing card."""
    if not text:
        return None
    t = text.split(" | ")[0].strip()
    return t if len(t) <= 90 else t[:87].rsplit(" ", 1)[0] + "..."


GENERIC_PROMO_RE = re.compile(r"\bup to\b.{0,40}\bon (select|selected|participating|qualifying)\s+(homes|inventory)\b", re.I)


def consumer_site(st, homes: dict, run_at: str, weeks: int = 6) -> dict:
    """Compact feed for the public site (docs/homes/): every active home with one 'deal' figure to sort by,
    and Monday-to-Monday weeks of price drops and new incentives."""
    from datetime import timedelta
    now = datetime.strptime(run_at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ) if run_at else datetime.now(TZ)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    def when(s):
        try:
            return datetime.strptime(str(s or ""), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError:
            return None

    builders = sorted({h["builder"] for h in homes.values() if h.get("builder")})
    rows = []
    for h in homes.values():
        if not h.get("price"):
            continue
        offer = h.get("offer") or {}
        disc, drop = h.get("builder_discount") or 0, h.get("total_drop") or 0
        ov = 0
        for part in (offer.get("text") or "").split(" | "):
            if GENERIC_PROMO_RE.search(part):
                continue      # "Plus save up to $75,000 on select homes": not this home's own number
            v = incentive_value(part, h.get("price")) or 0
            if PRICE_CUT_RE.search(part) and v:
                disc = max(disc, v)      # "$182K off" is this home's markdown, not an extra incentive
            elif v not in (0, ov):
                ov += v
        deal = max(disc, drop) + ov          # builder's markdown or our observed drop (they overlap), + incentive
        rows.append({
            "id": h["id"], "builder": h.get("builder"), "address": h.get("address"), "city": h.get("city"),
            "zip": h.get("zip"), "community": h.get("community"), "beds": h.get("beds"), "baths": h.get("baths"),
            "sqft": h.get("sqft"), "price": h["price"], "was": h.get("builder_was_price"),
            "first_price": h.get("first_price"), "drop": drop or None, "cuts": h.get("cuts") or 0,
            "last_cut_at": h.get("last_cut_at"), "discount": disc or None, "offer_value": ov or None,
            "offer": _short_offer(offer.get("text")), "offer_full": offer.get("text"),
            "offer_type": offer.get("type"), "rate": offer.get("rate"),
            "deal": deal or None, "deal_pct": round(deal / (h["price"] + max(disc, drop)) * 100, 1) if deal else None,
            "move_in": h.get("move_in_label"), "url": h.get("url"), "lat": h.get("lat"), "lng": h.get("lng"),
            "pin": h.get("location_precision"), "first_seen": h.get("first_seen_at"),
        })
    # biggest deal first; then rate offers (lowest rate first); then the rest, newest listings first
    rows.sort(key=lambda r: (-(r["deal"] or 0), r["rate"] if r["rate"] else 99, -(int((r["first_seen"] or "0")[:10].replace("-", "") or 0))))

    first_day = {}
    for c in st.changes:
        t = when(c.get("at"))
        if c.get("event") == "new_home" and t:
            first_day[c.get("builder")] = min(first_day.get(c.get("builder"), t), t)
    out_weeks = []
    for i in range(weeks):
        start = monday - timedelta(days=7 * i)
        end = start + timedelta(days=7)
        if i and first_day and end <= min(first_day.values()) + timedelta(days=1):
            break   # nothing tracked yet that week beyond the first-day baseline (the current week always shows)
        drops, new_homes = [], 0
        for c in st.changes:
            t = when(c.get("at"))
            if not t or not (start <= t < end):
                continue
            if c.get("event") == "price_cut" and c.get("home_id") in homes and c.get("old") and c.get("new"):
                drops.append({"id": c["home_id"], "at": iso(c["at"]), "old": int(c["old"]), "new": int(c["new"]),
                              "drop": int(c["old"]) - int(c["new"])})
            elif c.get("event") == "new_home" and t >= first_day.get(c.get("builder"), t) + timedelta(hours=24):
                new_homes += 1
        drops.sort(key=lambda d: -d["drop"])
        offers = []
        for h in homes.values():
            o = h.get("offer") or {}
            t, seen = when(o.get("since")), when(h.get("first_seen"))
            if t and start <= t < end and o.get("value") and t >= first_day.get(h.get("builder"), t) + timedelta(hours=24):
                offers.append({"id": h["id"], "at": iso(o["since"]), "value": o["value"],
                               "new_listing": bool(seen and seen >= start)})
        offers.sort(key=lambda d: -d["value"])
        out_weeks.append({"start": start.isoformat(), "end": end.isoformat(), "current": i == 0,
                          "price_drops": drops, "total_dropped": sum(d["drop"] for d in drops),
                          "new_incentives": offers, "new_listings": new_homes})
    return {"updated_at": iso(run_at), "builders": builders,
            "stats": {"homes": len(rows), "builders": len(builders),
                      "with_deal": sum(1 for r in rows if r["deal"]),
                      "with_drop": sum(1 for r in rows if r["drop"]),
                      "with_offer": sum(1 for r in rows if r["offer"])},
            "homes": rows, "weeks": out_weeks}


def _write(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")


def export_json(folder: Path, st, derived: dict, run_at: str):
    api = folder / "api"
    api.mkdir(parents=True, exist_ok=True)
    active = [h for h in st.homes.values() if h.get("active")]
    homes = {h["home_id"]: _home(h) for h in active}

    history: dict[str, list] = {}
    stats: dict[str, dict] = {}
    for c in st.changes:
        hid = c.get("home_id")
        if c.get("event") in ("new_home", "price_cut", "price_increase") and c.get("new"):
            history.setdefault(hid, []).append({"at": c.get("at"), "price": c.get("new")})
        if c.get("event") in ("price_cut", "price_increase") and c.get("old") and c.get("new"):
            s = stats.setdefault(hid, {"cuts": 0, "increases": 0})
            d = int(c["old"]) - int(c["new"])
            if d > 0:
                s["cuts"] += 1
                s["largest_cut"] = max(s.get("largest_cut") or 0, d)
                s["last_cut"], s["last_cut_at"] = d, iso(c.get("at"))
            else:
                s["increases"] += 1
    for hid, out in homes.items():
        add_card(out, stats.get(hid, {}))

    def rows(tab, key_fields=None):
        return [{k: _clean(v) for k, v in r.items()} for r in derived.get(tab, [])]

    def drop_rows():
        out = []
        by_url = {h["url"]: h for h in homes.values() if h.get("url")}
        for r in rows("Price Drops"):
            h = by_url.get(r.get("url")) or {}
            r.update(home_id=h.get("id"), detected_at=iso(r.get("at")), total_drop=h.get("total_drop"),
                     cuts=h.get("cuts"), current_price=h.get("price"), card=h.get("card"))
            out.append(r)
        return out

    files = {
        "homes.json": sorted(homes.values(), key=lambda r: (r["builder"] or "", r["city"] or "", r["address"] or "")),
        "price_drops.json": drop_rows(),
        "new_homes.json": rows("New Homes"),
        "sold_removed.json": rows("Sold / Removed"),
        "home_offers.json": [homes[h["home_id"]] for h in active if h.get("incentive_text")],
        "builder_offers.json": [{"builder": o.get("builder"), "type": o.get("kind"),
                                 "amount": _clean(o.get("headline_amount")), "rate": _clean(o.get("rate")),
                                 "text": o.get("text"), "source_url": o.get("source_url"),
                                 "first_seen": o.get("first_seen"), "last_seen": o.get("last_seen")}
                                for o in st.incentives.values() if o.get("active")],
        "summary.json": rows("Summary"),
        "builders.json": builder_profiles(st, homes, derived, run_at),
        "this_week.json": this_week(st, homes, run_at),
        "map.json": [{"id": h["id"], "lat": h["lat"], "lng": h["lng"], "precision": h["location_precision"],
                      "price": h["price"], "builder": h["builder"], "address": h["address"], "city": h["city"],
                      "beds": h["beds"], "sqft": h["sqft"], "change": h["change_since_first"],
                      "total_drop": h["total_drop"], "cuts": h["cuts"], "savings": h["total_savings"],
                      "offer": bool(h["offer"]), "url": h["url"]}
                     for h in homes.values() if h["lat"] is not None],
        "price_history.json": {hid: pts for hid, pts in history.items() if hid in homes},
        "site.json": consumer_site(st, homes, run_at),
    }
    files["homes.geojson"] = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [h["lng"], h["lat"]]},
         "properties": {k: v for k, v in h.items() if k not in ("lat", "lng")}}
        for h in homes.values() if h["lat"] is not None]}
    for name, data in files.items():
        _write(api / name, data)
    _write(api / "meta.json", {
        "updated_at": run_at, "timezone": "America/Chicago",
        "counts": {(k[:-5] if k.endswith(".json") else k.replace(".", "_")):
                   len(v["features"] if k.endswith(".geojson") else v["homes"] if k == "site.json" else v)
                   for k, v in files.items()},
        "builders": sorted({h["builder"] for h in active if h.get("builder")}),
        "files": sorted(files) + ["meta.json"],
        "mapped": sum(1 for h in homes.values() if h["lat"] is not None),
        "notes": {
            "price changes": "Measured from the first price this tracker saw, not the builder's original list "
                             "price. 'at'/'last_change_at' is when the change was detected (hourly checks).",
            "builder_was_price": "The builder's own struck-through price, when the page shows one.",
            "sold_removed": "Gone from the builder's site for 48+ hours (usually sold or under contract).",
            "ids": "Home 'id' is stable across runs and keys price_history.json.",
            "total_drop / cuts": "Dollars and number of price cuts since this tracker first saw the home, "
                                 "recomputed from the full change history every run (tracking began 2026-10-18).",
            "builder_discount": "builder_was_price minus price: the markdown the builder itself shows.",
            "total_savings": "builder_discount + offer.value (advertised per-home incentive, approximate).",
            "location_precision": "builder = pin from the builder's own page; address = U.S. Census geocode of "
                                  "the street address; zip = center of the ZIP code (approximate).",
        },
    })
