"""Coordinates for homes whose builder page didn't provide them.

1. U.S. Census Geocoder, batch endpoint (free, no key): street + city + zip -> rooftop/street-range point.
   Brand-new streets are sometimes not in the Census database yet; those get retried every 7 days.
2. ZIP code center (Census ZCTA gazetteer), marked approximate, so every home can still be mapped.

location_precision on each home: "builder" (from the builder's own page), "address" (Census match),
"zip" (ZIP code center, approximate).
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .extract import valid_coords

log = logging.getLogger(__name__)
CENSUS_BATCH = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
GAZETTEER = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip"
ZIP_FILE = Path(__file__).with_name("zip_centroids.csv")
RETRY_DAYS = 7
BATCH_MAX = 9000          # Census limit is 10,000 per request


def _session(user_agent: str):
    s = requests.Session()
    s.headers["User-Agent"] = user_agent
    return s


def census_batch(rows: list[tuple], session) -> dict:
    """rows: (id, street, city, state, zip). Returns id -> (lat, lng) for matches."""
    if not rows:
        return {}
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    r = session.post(CENSUS_BATCH, data={"benchmark": "Public_AR_Current"},
                     files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")}, timeout=600)
    r.raise_for_status()
    return parse_census(r.text)


def parse_census(text: str) -> dict:
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 6 and row[2].strip().lower() == "match" and "," in row[5]:
            lng, lat = row[5].split(",", 1)
            c = valid_coords(lat, lng)
            if c:
                out[row[0]] = c
    return out


def zip_centroids(session=None) -> dict:
    """zip -> (lat, lng) for Texas 75xxx/76xxx; downloaded once and kept next to this file."""
    if not ZIP_FILE.exists() and session is not None:
        try:
            r = session.get(GAZETTEER, timeout=120)
            r.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(r.content))
            txt = z.read(z.namelist()[0]).decode("utf-8", "replace")
            rows = []
            for line in txt.splitlines()[1:]:
                f = [x.strip() for x in line.split("\t")]
                if f and f[0][:2] in ("75", "76") and len(f) >= 7:
                    rows.append((f[0], f[5], f[6]))
            with ZIP_FILE.open("w", newline="") as fh:
                csv.writer(fh).writerows([("zip", "lat", "lng")] + rows)
        except Exception as e:  # never let the fallback break a run
            log.warning("zip centroid download failed: %s", e)
    if not ZIP_FILE.exists():
        return {}
    out = {}
    for r in csv.DictReader(ZIP_FILE.open()):
        c = valid_coords(r["lat"], r["lng"])
        if c:
            out[r["zip"]] = c
    return out


def fill_missing(homes: dict, now: datetime, fmt: str, user_agent: str, session=None) -> dict:
    """Give coordinates to active homes that lack builder-supplied ones. Returns counts."""
    session = session or _session(user_agent)
    todo = []
    for hid, h in homes.items():
        if not h.get("active") or h.get("location_precision") in ("builder", "address"):
            continue
        if not h.get("address") or not (h.get("city") or h.get("zip")):
            continue
        tried = h.get("geocode_tried")
        if tried:
            try:
                if now - datetime.strptime(str(tried), fmt).replace(tzinfo=now.tzinfo) < timedelta(days=RETRY_DAYS):
                    continue
            except ValueError:
                pass
        todo.append(hid)
    found = {}
    for i in range(0, len(todo), BATCH_MAX):
        chunk = todo[i:i + BATCH_MAX]
        rows = [(hid, homes[hid]["address"], homes[hid].get("city") or "", "TX", homes[hid].get("zip") or "")
                for hid in chunk]
        try:
            found.update(census_batch(rows, session))
        except Exception as e:
            log.warning("census geocoder failed: %s", e)
            break
        for hid in chunk:
            homes[hid]["geocode_tried"] = now.strftime(fmt)
    for hid, (lat, lng) in found.items():
        homes[hid].update(lat=lat, lng=lng, location_precision="address")

    zips, by_zip = None, 0
    for h in homes.values():
        if h.get("active") and h.get("lat") in (None, "") and h.get("zip"):
            if zips is None:
                zips = zip_centroids(session)
            c = zips.get(str(h["zip"])[:5])
            if c:
                h.update(lat=c[0], lng=c[1], location_precision="zip")
                by_zip += 1
    return {"census_tried": len(todo), "census_matched": len(found), "zip_fallback": by_zip}
