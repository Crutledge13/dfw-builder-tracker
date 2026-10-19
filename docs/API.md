# DFW Builder Tracker — data feed

Public JSON files, refreshed every hour. Any website can load them with `fetch()`; GitHub Pages
allows cross-site requests, so no key or server is needed.

Base URL: `https://crutledge13.github.io/dfw-builder-tracker/data/api/`

| File | What it is |
|---|---|
| `meta.json` | Last update time (Central), counts, builder list, notes. Check this first. |
| `homes.json` | Every home currently for sale (quick move-in). One object per home. |
| `price_drops.json` | Price cuts detected in the last 90 days, newest first. |
| `new_homes.json` | Homes that appeared in the last 30 days. |
| `sold_removed.json` | Homes that left the market in the last 90 days (usually sold or under contract). |
| `home_offers.json` | Homes whose own listing advertises an offer (same shape as `homes.json`). |
| `builder_offers.json` | Builder-wide promotions (rate buydowns, closing costs, flex cash…). |
| `summary.json` | One row per builder: active homes, cuts in 24h / 7 days, $ cut, new, removed, offers. |
| `map.json` | Lightweight list for map pins: id, lat, lng, precision, price, builder, address, city, beds, sqft, change, offer (true/false), url. |
| `homes.geojson` | Every mapped home as GeoJSON (works directly with Leaflet, Mapbox, Google Maps data layers). |
| `price_history.json` | `{ "<home id>": [ {"at": "...", "price": 449990}, … ] }` — for price charts. |

## A home (`homes.json`, `home_offers.json`)

```json
{
  "id": "bloomfield:214-seminole-trl",
  "builder": "Bloomfield Homes", "community": "Sunset Ridge", "city": "Alvarado", "zip": "76009",
  "address": "214 Seminole Trail", "plan": "Cypress II",
  "beds": 4, "baths": 3, "sqft": 2450,
  "price": 412900, "first_price": 424900, "previous_price": 424900,
  "change_since_first": -12000, "pct_since_first": -2.82, "cuts": 1,
  "last_change": -12000, "last_change_at": "2026-10-19 09:12",
  "builder_was_price": 443720, "price_per_sqft": 169,
  "status": "Under Construction", "move_in": "",
  "offer": {"text": "4% Towards Closing Costs on All Specs!", "type": "closing_costs",
            "amount": null, "rate": null, "since": "2026-10-18 18:59"},
  "lat": 32.416827, "lng": -97.217663, "location_precision": "builder",
  "url": "https://www.bloomfieldhomes.com/new-homes/tx/alvarado/sunset-ridge/cypress-ii/214-seminole-trail",
  "first_seen": "2026-10-18 18:59", "last_seen": "2026-10-19 09:12"
}
```

- Prices are whole dollars. Missing values are `null`.
- `first_price` / `change_since_first` are measured from when this tracker first saw the home,
  not the builder's original list price. `builder_was_price` is the builder's own struck-through price.
- Times are Central (`YYYY-MM-DD HH:MM`) and mean "when the tracker detected it" (checked hourly).
- `offer.type` is one of `rate`, `closing_costs`, `price_reduction`, `cash_credit`, `other`.
- `lat` / `lng` place the home on a map. `location_precision`: `builder` = the pin from the builder's
  own page, `address` = U.S. Census geocode of the street address, `zip` = center of the ZIP code
  (approximate; show these differently, e.g. a lighter pin, or hide them when zoomed in).
  Brand-new streets can take a few days to geocode, so some homes start as `zip` and get upgraded.
- `url` is the builder's own page for the home — link to it for final pricing and terms.

## Paste this into ChatGPT to build the site

> Build a website that shows new-construction homes in Dallas–Fort Worth with price drops and builder
> incentives. Load all data in the browser with `fetch()` from
> `https://crutledge13.github.io/dfw-builder-tracker/data/api/` — files: meta.json, homes.json,
> price_drops.json, new_homes.json, sold_removed.json, home_offers.json, builder_offers.json,
> summary.json, price_history.json. The field definitions are at
> https://github.com/Crutledge13/dfw-builder-tracker/blob/main/docs/API.md (read it).
> Never hard-code or copy the data into the site; always fetch it so the site stays current (it updates
> hourly). Pages: home (latest price drops + biggest offers), browse homes (filters: builder, city, price,
> beds, has offer, price cut) with a map view (Leaflet + OpenStreetMap tiles, no API key needed; load
> map.json for pins, cluster them, color pins with a price cut, show a lighter pin when precision is "zip",
> and filter the map together with the list), home detail (price history chart from price_history.json keyed by id, offer,
> link to the builder's page), builder pages (from summary.json), and "recently sold". Show "Updated <time>"
> from meta.json. Note on each page that prices and offers come from builders' websites and must be
> confirmed with the builder.
