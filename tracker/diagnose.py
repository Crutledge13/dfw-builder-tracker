"""Save raw pages per builder for debugging extraction: seed, promo, two queued home
pages, and the sitemap roots. Output: diag/<slug>/*.html + index.tsv"""
from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path

from .fetch import PoliteClient
from .run import load_config


def main():
    only = sys.argv[1:]
    out = Path("diag")
    out.mkdir(exist_ok=True)
    ua = os.environ.get("CRAWLER_USER_AGENT") or \
        "DFWBuilderTracker/1.0 (+https://github.com/Crutledge13/dfw-builder-tracker)"
    frontier = list(csv.DictReader(open("docs/data/frontier.csv"))) if Path("docs/data/frontier.csv").exists() else []
    index = open(out / "index.tsv", "w")
    for b in load_config():
        if only and b["slug"] not in only:
            continue
        c = PoliteClient(ua, min_delay=1.5)
        d = out / b["slug"]
        d.mkdir(exist_ok=True)
        homes = [f["url"] for f in frontier if f["builder"] == b["name"] and f["kind"] == "home"][:2]
        targets = [("seed", u) for u in (b.get("seeds") or [])[:2]] + \
                  [("promo", u) for u in (b.get("promos") or [])[:1]] + [("home", u) for u in homes]
        for sm in c.sitemaps_from_robots(b["site"])[:2]:
            targets.append(("sitemap", sm))
        for i, (kind, url) in enumerate(targets):
            r = c.get(url)
            status = r.status if r else "skipped"
            name = f"{i:02d}-{kind}.html"
            if r:
                (d / name).write_text(r.text[:3_000_000], encoding="utf-8")
            index.write(f"{b['slug']}\t{kind}\t{status}\t{len(r.text) if r else 0}\t{name}\t{url}\n")
            index.flush()
        print(b["slug"], "done", c.blocked or "")


if __name__ == "__main__":
    main()
