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
        "map.json": [{"id": h["id"], "lat": h["lat"], "lng": h["lng"], "precision": h["location_precision"],
                      "price": h["price"], "builder": h["builder"], "address": h["address"], "city": h["city"],
                      "beds": h["beds"], "sqft": h["sqft"], "change": h["change_since_first"],
                      "total_drop": h["total_drop"], "cuts": h["cuts"], "savings": h["total_savings"],
                      "offer": bool(h["offer"]), "url": h["url"]}
                     for h in homes.values() if h["lat"] is not None],
        "price_history.json": {hid: pts for hid, pts in history.items() if hid in homes},
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
                   len(v["features"] if k.endswith(".geojson") else v) for k, v in files.items()},
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
