"""Pull published offers (rate buydowns, closing-cost help, price reductions, flex cash)
from a builder's promotions pages, and diff them against what was seen before."""
from __future__ import annotations

import difflib
import hashlib
import re

from bs4 import BeautifulSoup

from .extract import INCENTIVE_WORDS

KINDS = [
    ("rate", re.compile(r"(rate|buy ?down|apr|arm\b|fixed|financing)", re.I)),
    ("closing_costs", re.compile(r"closing cost", re.I)),
    ("price_reduction", re.compile(r"(price reduc|reduced|\$[\d,]+k? off|discount|savings|save)", re.I)),
    ("cash_credit", re.compile(r"(flex ?cash|credit|cash|allowance|upgrade)", re.I)),
]


def classify(text: str) -> str:
    for kind, rx in KINDS:
        if rx.search(text):
            return kind
    return "other"


def headline_amount(text: str) -> int | None:
    vals = []
    for m in re.finditer(r"\$\s?(\d[\d,]*)(\s?[kK])?", text):
        n = int(m.group(1).replace(",", ""))
        if m.group(2):
            n *= 1000
        if 500 <= n <= 250_000:
            vals.append(n)
    return max(vals) if vals else None


def lowest_rate(text: str) -> float | None:
    vals = [float(m) for m in re.findall(r"(\d{1,2}\.\d{1,3})\s?%", text)]
    vals = [v for v in vals if 0.5 <= v <= 12]
    return min(vals) if vals else None


def offer_id(builder_slug: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.lower()).strip()
    return builder_slug + ":" + hashlib.sha1(norm.encode()).hexdigest()[:12]


UI_WORDS = re.compile(r"\s*\b(tap to expand|get offer|learn more|see details|view details|read more|"
                      r"click here|shop now|explore offers?)\b\s*[→>»]*", re.I)


def extract_offers(html: str, max_offers: int = 25) -> list[dict]:
    from .extract import incentive_snippets
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "form", "select", "nav"]):
        t.decompose()
    text = soup.get_text("  ", strip=True)
    offers, seen = [], set()
    for s in incentive_snippets(text, limit=max_offers * 2):
        s = UI_WORDS.sub(" ", s).strip(" *|")
        s = re.sub(r"\s{2,}", " ", s)
        key = re.sub(r"[^a-z0-9$%]", "", s.lower())
        if len(s) < 6 or key in seen or any((key in k or k in key) and min(len(k), len(key)) > 24 for k in seen):
            continue
        seen.add(key)
        offers.append({"text": s, "kind": classify(s), "headline_amount": headline_amount(s), "rate": lowest_rate(s)})
        if len(offers) >= max_offers:
            break
    return offers


def pair_changes(removed: list[dict], added: list[dict], threshold: float = 0.6):
    """Match removed and added offers that look like edits of each other."""
    pairs, used = [], set()
    for r in removed:
        best, best_score = None, threshold
        for i, a in enumerate(added):
            if i in used or a["source_url"] != r["source_url"]:
                continue
            score = difflib.SequenceMatcher(None, r["text"], a["text"]).ratio()
            if score > best_score:
                best, best_score = i, score
        if best is not None:
            used.add(best)
            pairs.append((r, added[best]))
    paired_r = {id(p[0]) for p in pairs}
    rest_r = [r for r in removed if id(r) not in paired_r]
    rest_a = [a for i, a in enumerate(added) if i not in used]
    return pairs, rest_r, rest_a
