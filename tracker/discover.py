"""Find home-detail URLs: links on listing pages, pagination, and sitemaps."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

PAGINATION_RE = re.compile(r"([?&](page|pg|p)=\d+|/page/\d+/?$)", re.I)


def canonical_url(url: str) -> str:
    p = urlparse(url)
    path = re.sub(r"/{2,}", "/", p.path).rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), path, "", "", ""))


def same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h
    return strip(ha) == strip(hb)


def links_from_html(html: str, base_url: str) -> tuple[set[str], set[str]]:
    """Returns (all same-site links, pagination links)."""
    soup = BeautifulSoup(html, "lxml")
    links, pages = set(), set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absu = urljoin(base_url, href)
        if not absu.startswith("http") or not same_site(absu, base_url):
            continue
        full = absu.split("#")[0]
        rel = " ".join(a.get("rel") or []).lower()
        text = a.get_text(" ", strip=True).lower()
        if "next" in rel or PAGINATION_RE.search(full) or text in {"next", "next page", "›", "»"}:
            pages.add(full)
        links.add(canonical_url(full))
    return links, pages


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Returns (child sitemaps, page urls)."""
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, flags=re.I | re.S)
    locs = [l.replace("&amp;", "&") for l in locs]
    if re.search(r"<sitemapindex", xml, re.I):
        return locs, []
    return [], locs


def discover_from_sitemaps(client, site: str, detail_re: re.Pattern, filter_re: re.Pattern | None,
                           max_sitemaps: int = 40, max_urls: int = 20000) -> set[str]:
    roots = client.sitemaps_from_robots(site) or [urljoin(site, "/sitemap.xml"),
                                                  urljoin(site, "/sitemap_index.xml")]
    queue, seen, found = list(dict.fromkeys(roots)), set(), set()
    while queue and len(seen) < max_sitemaps and len(found) < max_urls:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        # skip obviously irrelevant child sitemaps (images, blog, etc.)
        if re.search(r"(image|video|blog|post|news|author|tag|category)", sm, re.I) and seen != {sm}:
            continue
        r = client.get(sm)
        if not r or r.status != 200:
            continue
        children, urls = parse_sitemap(r.text)
        if filter_re is not None:
            children = sorted(children, key=lambda c: 0 if filter_re.search(c) else 1)
        queue.extend(children)
        for u in urls:
            cu = canonical_url(u)
            if detail_re.search(urlparse(cu).path) and (filter_re is None or filter_re.search(cu)):
                found.add(cu)
    return found
