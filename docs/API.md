# DFW Builder Tracker — data feed

Public JSON files, refreshed every hour. Any website can load them with `fetch()`; GitHub Pages
allows cross-site requests, so no key or server is needed.

Base URL: `https://crutledge13.github.io/dfw-builder-tracker/data/api/`

| File | What it is |
|---|---|
| `meta.json` | Last update time (Central), counts, builder list, notes. Check this first. |
| `homes.json` | Every home currently for sale (quick move-in). One object per home. |
| `price_drops.json` | Price cuts detected in the last 90 days, newest first. Each row also has that home's `total_drop`, `cuts`, `current_price` and `card`. |
| `new_homes.json` | Homes that appeared in the last 30 days. |
| `sold_removed.json` | Homes that left the market in the last 90 days (usually sold or under contract). |
| `home_offers.json` | Homes whose own listing advertises an offer (same shape as `homes.json`). |
| `builder_offers.json` | Builder-wide promotions (rate buydowns, closing costs, flex cash…). |
| `builders.json` | **One record per builder**: headline incentive, builder-wide offers, offers repeated across its homes, inventory, price range, 7-day activity, cities. Powers a Builders tab. |
| `this_week.json` | **The last 7 days**: headline stats, biggest price cuts, new/changed offers, new inventory, sold/off market, homes past estimated completion, most active communities, builders and cities. Powers a "This week" page. |
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
  "change_since_first": -12000, "pct_since_first": -2.82,
  "total_drop": 12000, "total_drop_pct": 2.82, "cuts": 1, "largest_cut": 12000,
  "last_cut": 12000, "last_cut_at": "2026-10-19T09:12:00-05:00", "increases": 0,
  "last_change": -12000, "last_change_at": "2026-10-19 09:12",
  "builder_was_price": 443720, "builder_discount": 30820, "total_savings": 47320,
  "price_per_sqft": 169,
  "status": "Under Construction", "move_in": "", "move_in_label": "Under construction",
  "offer": {"text": "4% Towards Closing Costs on All Specs!", "type": "closing_costs",
            "amount": null, "rate": null, "value": 16500, "since": "2026-10-18 18:59"},
  "checked_at": "2026-10-19T09:12:00-05:00", "first_seen_at": "2026-10-18T18:59:00-05:00",
  "card": {"price_drop": "Down $12k since first tracked", "cuts": "1 cut", "savings": "$47k incentive",
           "move_in": "Under construction", "beds": "4 bd", "baths": "3 ba", "sqft": "2,450 sq ft"},
  "lat": 32.416827, "lng": -97.217663, "location_precision": "builder",
  "url": "https://www.bloomfieldhomes.com/new-homes/tx/alvarado/sunset-ridge/cypress-ii/214-seminole-trail",
  "first_seen": "2026-10-18 18:59", "last_seen": "2026-10-19 09:12"
}
```

- Prices are whole dollars. Missing values are `null`.
- **Price-drop totals** (recomputed every hour from the full change history):
  `total_drop` = dollars down since first tracked (0 if not down), `cuts` = number of separate price cuts,
  `largest_cut`, `last_cut` / `last_cut_at`, `increases` = number of price increases.
- **Savings**: `builder_discount` = `builder_was_price` − `price` (the markdown the builder shows);
  `offer.value` = approximate dollar value of the home's advertised offer (e.g. "4% toward closing costs"
  × price; rate buydowns without a dollar figure = null); `total_savings` = both together, with the
  builder's markdown never counted twice.
- `move_in_label` is one wording across builders: `Ready now`, `Available`, `Under construction`,
  `Pending`, or `Ready <Mon YYYY>`.
- `checked_at` is when the tracker last saw the home on the builder's site (ISO time with offset, so a
  browser can show "Checked 2 hours ago").
- `card` holds ready-to-display labels for a listing card (null when not applicable).
- `first_price` / `change_since_first` are measured from when this tracker first saw the home,
  not the builder's original list price. `builder_was_price` is the builder's own struck-through price.
- Times are Central (`YYYY-MM-DD HH:MM`) and mean "when the tracker detected it" (checked hourly).
- `offer.type` is one of `rate`, `closing_costs`, `price_reduction`, `cash_credit`, `other`.
- `lat` / `lng` place the home on a map. `location_precision`: `builder` = the pin from the builder's
  own page, `address` = U.S. Census geocode of the street address, `zip` = center of the ZIP code
  (approximate; show these differently, e.g. a lighter pin, or hide them when zoomed in).
  Brand-new streets can take a few days to geocode, so some homes start as `zip` and get upgraded.
- `url` is the builder's own page for the home — link to it for final pricing and terms.

## A builder (`builders.json`)

```json
{
  "builder": "First Texas Homes", "slug": "first-texas", "website": "https://www.firsttexashomes.com/",
  "status": "ok", "checked_at": "2026-10-19T09:01:00-05:00",
  "headline_offer": "Rates as low as 2.99% · Up to $30k flex cash",
  "blanket_offers": [
    {"type": "rate", "amount": 20000, "rate": 2.99, "text": "As Low As 2.99% Rate Buydown or Up to $20K Flex Cash",
     "source_url": "https://www.firsttexashomes.com/promos/index.php", "homes": null,
     "first_seen": "2026-10-18T18:59:00-05:00", "last_seen": "2026-10-19T09:01:00-05:00"}
  ],
  "common_home_offers": [
    {"text": "Zero Down & Rates as Low as 3.99%", "type": "rate", "amount": null, "rate": 3.99,
     "homes": 175, "share": 0.59, "says_all_homes": false}
  ],
  "homes": {"active": 296, "with_offer": 290, "with_price_cut": 1, "with_builder_discount": 0,
            "ready_now": 120, "under_construction": 60, "pending": 4},
  "prices": {"min": 289950, "max": 1250000, "median": 489990, "median_per_sqft": 188,
             "avg_builder_discount": null, "max_total_savings": 30000},
  "activity": {"price_cuts_24h": 0, "price_cuts_7d": 1, "total_cut_7d": 5000, "biggest_cut_7d": 5000,
               "new_homes_7d": 2, "sold_removed_7d": 0},
  "cities": ["Celina", "Forney", "Midlothian", "..."], "communities": 40
}
```

- `blanket_offers`: promotions the builder publishes for its homes in general (its promotions page or
  DFW landing page). `homes` = how many of its homes also show that offer, when known.
- `common_home_offers`: offers that appear on many of the builder's own home listings (at least 5% of
  them, or the text says "all specs"/"all homes"), up to 3, not repeating a blanket offer.
  `share` = fraction of the builder's homes showing it.
- `headline_offer`: one line summarizing the best advertised rate and the biggest cash / closing-cost
  offer across both lists; `null` when the builder publishes no incentive.
- `status`: `ok`, `blocked` (the builder's site refuses automated reading), `no data`, or `error`.
- A ready-made Builders tab (plain HTML + JavaScript, no libraries) is in
  [docs/builders-tab.html](builders-tab.html) and live at
  https://crutledge13.github.io/dfw-builder-tracker/builders-tab.html

## This week (`this_week.json`)

One object, rebuilt every hour for the 7 days ending now:

- `range` {start, end, days}, `generated_at`, `notes`
- `stats`: price_cuts, total_cut, average_cut, biggest_cut, price_increases, new_homes, sold_removed,
  offer_changes, homes_tracked, homes_with_offer, builders_with_cuts
- `biggest_cuts` (largest single cuts first): address, city, community, builder, detected_at, old_price,
  new_price, drop, drop_pct, current_price, total_drop, cuts, beds, baths, sqft, move_in, lat, lng, card, url
- `offer_changes` (newest first): kind (`builder_offer` | `home_offer`), change (`new` | `changed`), builder,
  text, previous_text, homes (count for home offers), cities, detected_at, source_url
- `new_homes`: homes first seen this week (each builder's first day of tracking is a baseline and excluded)
- `sold_removed`: homes that left the builder's site: last_price, first_price, total_drop, cuts, days_tracked
- `past_completion`: still listed after the builder's estimated completion month
- `most_active_communities`, `builders`, `cities`: changes, price_cuts, total_cut, new_homes, sold_removed,
  offer_changes, homes_available (communities also ready_now, with_offer)

Lists are capped at 25 rows. A ready-made page is in [docs/this-week-tab.html](this-week-tab.html), live at
https://crutledge13.github.io/dfw-builder-tracker/this-week-tab.html

## Paste this into ChatGPT to build the site

> Build a website that shows new-construction homes in Dallas–Fort Worth with price drops and builder
> incentives. Load all data in the browser with `fetch()` from
> `https://crutledge13.github.io/dfw-builder-tracker/data/api/` — files: meta.json, homes.json,
> price_drops.json, new_homes.json, sold_removed.json, home_offers.json, builder_offers.json,
> summary.json, builders.json, this_week.json, price_history.json, map.json, homes.geojson. The field definitions are at
> https://github.com/Crutledge13/dfw-builder-tracker/blob/main/docs/API.md (read it).
> Never hard-code or copy the data into the site; always fetch it so the site stays current (it updates
> hourly). Pages: home (latest price drops + biggest offers), browse homes (filters: builder, city, price,
> beds, has offer, price cut) with a map view (Leaflet + OpenStreetMap tiles, no API key needed; load
> map.json for pins, cluster them, color pins with a price cut, show a lighter pin when precision is "zip",
> and filter the map together with the list). Every home listing card shows, from each home's fields:
> card.price_drop (e.g. "Down $108k since first tracked") and card.cuts ("10 cuts") prominently,
> "Checked <x> hours ago" from checked_at, plan, community, builder, card.beds, card.baths, card.sqft,
> card.move_in, and card.savings ("$133k incentive"); sort options include biggest total_drop and most cuts.
> Home detail (price history chart from price_history.json keyed by id, offer,
> link to the builder's page), a Builders tab from builders.json (one card per builder: headline_offer,
> each blanket offer and common home offer with its "on N of M homes" count, price range, homes with an
> offer, 7-day price cuts; sort by most homes, lowest rate, biggest cash offer; filter "has an offer"), and "recently sold". Show "Updated <time>"
> from meta.json. Note on each page that prices and offers come from builders' websites and must be
> confirmed with the builder.
