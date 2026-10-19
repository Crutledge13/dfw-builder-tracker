"""JSON API for websites: docs/data/api/*.json, rewritten every run and served by GitHub Pages
(public, CORS-enabled), e.g. https://crutledge13.github.io/dfw-builder-tracker/data/api/homes.json"""
from __future__ import annotations

import json
from pathlib import Path

HOME_FIELDS = ["builder", "community", "city", "zip", "address", "plan", "beds", "baths", "sqft", "price",
               "first_price", "prev_price", "change_since_first", "pct_since_first", "cuts", "last_change",
               "last_change_at", "builder_was_price", "price_per_sqft", "status", "move_in", "url", "first_seen",
               "last_seen"]


def _clean(v):
    if v == "" or v is None:
        return None
    return v


def _home(h: dict) -> dict:
    out = {"id": h.get("home_id")}
    for k in HOME_FIELDS:
        out["previous_price" if k == "prev_price" else k] = _clean(h.get(k))
    out["offer"] = None
    if h.get("incentive_text"):
        out["offer"] = {"text": h["incentive_text"], "type": _clean(h.get("offer_type")),
                        "amount": _clean(h.get("offer_amount")), "rate": _clean(h.get("offer_rate")),
                        "since": _clean(h.get("offer_since"))}
    return out


def _write(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")


def export_json(folder: Path, st, derived: dict, run_at: str):
    api = folder / "api"
    api.mkdir(parents=True, exist_ok=True)
    active = [h for h in st.homes.values() if h.get("active")]
    homes = {h["home_id"]: _home(h) for h in active}

    history: dict[str, list] = {}
    for c in st.changes:
        if c.get("event") in ("new_home", "price_cut", "price_increase") and c.get("new"):
            history.setdefault(c.get("home_id"), []).append({"at": c.get("at"), "price": c.get("new")})

    def rows(tab, key_fields=None):
        return [{k: _clean(v) for k, v in r.items()} for r in derived.get(tab, [])]

    files = {
        "homes.json": sorted(homes.values(), key=lambda r: (r["builder"] or "", r["city"] or "", r["address"] or "")),
        "price_drops.json": rows("Price Drops"),
        "new_homes.json": rows("New Homes"),
        "sold_removed.json": rows("Sold / Removed"),
        "home_offers.json": [homes[h["home_id"]] for h in active if h.get("incentive_text")],
        "builder_offers.json": [{"builder": o.get("builder"), "type": o.get("kind"),
                                 "amount": _clean(o.get("headline_amount")), "rate": _clean(o.get("rate")),
                                 "text": o.get("text"), "source_url": o.get("source_url"),
                                 "first_seen": o.get("first_seen"), "last_seen": o.get("last_seen")}
                                for o in st.incentives.values() if o.get("active")],
        "summary.json": rows("Summary"),
        "price_history.json": {hid: pts for hid, pts in history.items() if hid in homes},
    }
    for name, data in files.items():
        _write(api / name, data)
    _write(api / "meta.json", {
        "updated_at": run_at, "timezone": "America/Chicago",
        "counts": {k.removesuffix(".json"): len(v) for k, v in files.items()},
        "builders": sorted({h["builder"] for h in active if h.get("builder")}),
        "files": sorted(files) + ["meta.json"],
        "notes": {
            "price changes": "Measured from the first price this tracker saw, not the builder's original list "
                             "price. 'at'/'last_change_at' is when the change was detected (hourly checks).",
            "builder_was_price": "The builder's own struck-through price, when the page shows one.",
            "sold_removed": "Gone from the builder's site for 48+ hours (usually sold or under contract).",
            "ids": "Home 'id' is stable across runs and keys price_history.json.",
        },
    })
