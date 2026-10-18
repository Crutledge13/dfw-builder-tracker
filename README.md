# DFW New-Home Price & Incentive Tracker

Reads 21 Dallas–Fort Worth builders' own websites every hour and keeps a live, public
spreadsheet page of every quick move-in home, its full price history, every price drop, and
every published builder offer (rate buydowns, closing-cost help, flex cash, price
reductions), including when offers appear, change, or disappear.

Runs free on GitHub Actions; data is saved as CSV files in this repo and shown on GitHub
Pages, so there is nothing else to host or log into. When you build the website, read the
same CSVs (or load them into a database; the columns are already shaped for that).

## Where to see it

- **Live spreadsheet page:** `https://<your-github-username>.github.io/<repo>/` — tabs for
  Summary, Price Drops, Homes, Incentives, Changes and Run Log, with search, builder filter,
  sorting and CSV download. Refreshes itself every 15 minutes; data updates hourly.
- **Raw data:** `docs/data/*.csv` in the repo (GitHub shows these as searchable tables).
- **Google Sheet (optional):** in a blank sheet, put
  `=IMPORTDATA("https://<user>.github.io/<repo>/data/homes.csv")` in A1 (one tab per CSV).
  Google re-pulls it about every hour. No Google setup or credentials needed.

| Tab | What it holds |
|---|---|
| **Summary** | One row per builder: active homes, cuts in the last 24h / 7 days, total and biggest cut, new and removed homes, active offers, last run status. |
| **Homes** | One row per home (current state): builder, community, city, address, plan, beds, baths, sqft, price, the builder's own "was" price when shown, first price we saw, previous price, change since first seen ($ and %), number of cuts, $/sqft, status, move-in, the home's offer text, link, first/last seen, active. |
| **Price Drops** | Every price cut in the last 90 days, newest first, with that home's advertised offer. "Detected" is when the tracker saw the new price; homes are rechecked every 1–3 hours. |
| **Home Offers** | Every active home whose own listing advertises an offer: the offer text, type (rate, closing costs, price reduction, cash/credit), $ amount, rate, and since when it's been shown. |
| **Changes** | Append-only history: `new_home`, `price_cut`, `price_increase`, `price_unconfirmed`, `status_change`, `removed`, `relisted`, `offer_new`, `offer_changed`, `offer_removed`, `home_offer_changed`. |
| **Builder Offers** | Builder-wide offers found on each builder's promotions pages: type, headline $ amount, lowest rate, full text, source page, first/last seen, still active. |
| **Run Log** | Per builder, per run: pages read, homes seen, changes, `ok` / `no data` / `blocked` and why. |

Price changes are measured from the first price this tracker saw, not the builder's
original list price (the builder's own struck-through price is kept in `builder_was_price`).

## Setup

Already done for you if Claude set this up. To do it by hand: push this folder to a public
GitHub repo, enable **Settings → Pages → Deploy from branch → main / docs**, and run the
workflow once from the **Actions** tab. Optionally set a repo variable `CRAWLER_USER_AGENT`
(e.g. `YourSiteBot/1.0 (+https://yoursite.com/bot)`) so builders can see who is reading their pages.

Writing to a Google Sheet directly through the API is also supported: add `SHEET_ID` and
`GOOGLE_SERVICE_ACCOUNT_JSON` repo secrets (a Google Cloud service account with editor access
to the sheet). Not needed for the setup above.

## After the first run: check coverage

Open **Run Log**. Builders marked `verified: true` in `builders.yaml` had their listing
pages checked by hand. For the others the scraper also discovers home pages from each
site's sitemap, but a few may come back with `no data` or few homes. For each one:

1. Open the builder's DFW quick move-in page in your browser and copy its URL into
   that builder's `seeds:` in `builders.yaml`.
2. Open one individual home from that page and look at its URL. If it doesn't end in
   an address slug like `/1404-amber-way/`, set `detail_pattern:` to a regex that
   matches it (see Pacesetter and D.R. Horton in the file for examples).
3. Re-run just that builder: *Run workflow* → `only` = the builder's `slug`.

A builder marked `blocked` asked automated visitors to stop (403/429, or robots.txt).
The tracker respects that and moves on; it does not try to get around it.

## How it works

Each hourly run, per builder (builders run in parallel; requests to any one site are
spaced at least 2 seconds apart and follow its robots.txt and crawl-delay):

1. Reads the DFW listing pages and their pagination. Many builder sites embed every
   home as data in the page; those are recorded directly.
2. Every 6 hours, reads the sitemap for home pages it hasn't seen yet.
3. Refreshes individual home pages — new ones first, then the longest-unchecked —
   up to `PAGES_PER_BUILDER` (default 120) pages per run, so every home is rechecked
   every few hours and listing-page homes every hour.
4. Reads the promotions pages and diffs the offers against last time.
5. A home missing for 48 hours while its builder's site is readable is marked
   `removed` (usually sold or under contract).

Safeguards against bad reads: a price move larger than 30% must be seen twice in a
row before it's recorded (logged as `price_unconfirmed` meanwhile), and changes under
$100 are ignored.

## Run locally

```bash
pip install -r requirements.txt
python -m pytest                                  # offline tests with sample pages
python -m tracker.run --only bloomfield --budget 30   # writes CSVs to docs/data/
```

Set `SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` (the JSON text or a path to the file)
to write to the sheet instead.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `PAGES_PER_BUILDER` | 120 | Page budget per builder per run |
| `REQUEST_DELAY` | 2.0 | Minimum seconds between requests to one site |
| `WORKERS` | 8 | Builders crawled at the same time |
| `CRAWLER_USER_AGENT` | generic | How the crawler identifies itself |

Constants at the top of `tracker/run.py`: `MIN_CHANGE`, `SUSPECT_PCT`,
`REMOVE_AFTER_H`, `SITEMAP_EVERY_H`.

## Notes

- Collect only from builders' public pages, as this does. Dwellmark's own terms ask
  people not to bulk-copy its dataset, so don't feed this from their site or API.
- Hourly is more often than most trackers read builder sites (Dwellmark reads about
  once a day). If a builder objects, honor it: remove it from `builders.yaml`.
- "Century Homes" is configured as Century Communities (and Century Complete). Change
  it if you meant a different builder.
