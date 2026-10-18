"""Polite HTTP client: identifies itself, obeys robots.txt and crawl-delay,
spaces requests per host, and stops on a host for the rest of the run when
the host says no (401/403/429/503). It never retries around a block."""
from __future__ import annotations

import gzip
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

STOP_STATUSES = {401, 403, 429, 503}


@dataclass
class Response:
    url: str
    status: int
    text: str


class PoliteClient:
    def __init__(self, user_agent: str, min_delay: float = 2.0, timeout: float = 25.0,
                 max_bytes: int = 15_000_000):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._delay: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self.blocked: dict[str, str] = {}   # host -> reason
        self.requests_made = 0

    # -- robots ---------------------------------------------------------------
    def _host(self, url: str) -> str:
        return urlparse(url).netloc.lower()

    def _robots_for(self, url: str):
        host = self._host(url)
        if host in self._robots:
            return self._robots[host]
        p = urlparse(url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = self.session.get(robots_url, timeout=self.timeout)
            self.requests_made += 1
            if r.status_code >= 500:
                rp = None  # server trouble: treat as "don't crawl" this run
                self.blocked[host] = f"robots.txt returned {r.status_code}"
            elif r.status_code >= 400:
                rp.parse([])  # no robots.txt: everything allowed
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException as e:
            rp = None
            self.blocked[host] = f"robots.txt unreachable: {e.__class__.__name__}"
        self._robots[host] = rp
        delay = self.min_delay
        if rp is not None:
            cd = rp.crawl_delay(self.user_agent) or rp.crawl_delay("*")
            if cd:
                delay = max(delay, float(cd))
        self._delay[host] = delay
        return rp

    def sitemaps_from_robots(self, url: str) -> list[str]:
        rp = self._robots_for(url)
        return list(rp.site_maps() or []) if rp else []

    def allowed(self, url: str) -> bool:
        host = self._host(url)
        rp = self._robots_for(url)
        if host in self.blocked or rp is None:
            return False
        return rp.can_fetch(self.user_agent, url)

    # -- fetch ----------------------------------------------------------------
    def get(self, url: str) -> Response | None:
        """Returns a Response, or None when the URL may not / cannot be fetched."""
        host = self._host(url)
        if not self.allowed(url):
            log.info("skip (robots/blocked) %s", url)
            return None
        wait = self._last.get(host, 0) + self._delay.get(host, self.min_delay) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            r = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True)
            self.requests_made += 1
            body = r.raw.read(self.max_bytes + 1, decode_content=True)
            r.close()
        except requests.RequestException as e:
            log.warning("fetch error %s: %s", url, e)
            self._last[host] = time.monotonic()
            return Response(url, 0, "")
        finally:
            self._last[host] = time.monotonic()
        if r.status_code in STOP_STATUSES:
            self.blocked[host] = f"HTTP {r.status_code} on {url}"
            log.warning("host %s said %s; stopping this host for the run", host, r.status_code)
        if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
        text = body[: self.max_bytes].decode(r.encoding or "utf-8", errors="replace")
        return Response(r.url, r.status_code, text)
