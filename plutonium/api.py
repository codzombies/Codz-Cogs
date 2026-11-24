"""
Minimal, polite client for the public gameserve.rs API (https://gameserve.rs/docs).

Rate limiting / caching policy (per the API's "Rate limits and caching" notes):
  * Every response is cached for the TTL advertised in its Cache-Control header,
    never less than 30 seconds (the documented TTL for live server data).
  * Requests are serialised (one in flight at a time) and spaced at least
    MIN_REQUEST_SPACING seconds apart, across every guild using the cog.
  * Configured addresses are de-duplicated bot-wide before fetching.
  * Small address sets use /server/lookup (one tiny request each); large sets
    switch to the paginated /servers listing, which is fewer requests.
  * HTTP 429 / 5xx / network errors trigger exponential back-off (honouring
    Retry-After), during which no requests are made at all.
  * Request timeouts are only logged once TIMEOUT_LOG_AFTER happen in a row,
    since an occasional slow response is normal and not worth a warning.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set, Tuple

import aiohttp

log = logging.getLogger("red.codz.plutonium.api")

API_BASE = "https://gameserve.rs/api/v1"
SITE_BASE = "https://gameserve.rs"
USER_AGENT = "Red-DiscordBot Plutonium cog (+https://github.com/codzombies/Codz-Cogs)"

MIN_TTL = 30.0                # seconds; documented TTL for live server data
MIN_REQUEST_SPACING = 0.5     # seconds between requests (<= 2 req/s)
REQUEST_TIMEOUT = 15          # seconds
BULK_THRESHOLD = 15           # more stale addresses than this -> use bulk listing
BULK_PAGE_LIMIT = 500         # API maximum
BULK_MAX_PAGES = 10
BACKOFF_START = 30.0
BACKOFF_MAX = 900.0
OFFLINE_AFTER_MISSES = 2      # consecutive "not listed" results before a known server is shown offline
CACHE_PRUNE_AGE = 3600.0
TIMEOUT_LOG_AFTER = 3         # consecutive timeouts before they're logged

_ADDRESS_RE = re.compile(r"^(?P<host>[a-z0-9.-]+):(?P<port>\d{1,5})$")


class APIError(Exception):
    """The API could not be reached or returned something unusable."""


def normalize_address(raw: str) -> Optional[str]:
    """Return a canonical ``host:port`` string, or None if invalid."""
    raw = (raw or "").strip().lower()
    match = _ADDRESS_RE.match(raw)
    if not match:
        return None
    port = int(match["port"])
    if not 0 < port < 65536:
        return None
    return f"{match['host']}:{port}"


def server_url(address: str, server_id: Optional[str] = None) -> str:
    """gameserve.rs page for a server. IDs are base64("plutonium:ip:port")."""
    if not server_id:
        server_id = (
            base64.urlsafe_b64encode(f"plutonium:{address}".encode()).decode().rstrip("=")
        )
    return f"{SITE_BASE}/server/{server_id}"


@dataclass
class _CacheEntry:
    data: Optional[dict]  # None means "not listed" (offline)
    fetched_at: float
    ttl: float

    @property
    def fresh(self) -> bool:
        return time.monotonic() - self.fetched_at < self.ttl


class GameServeClient:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, _CacheEntry] = {}
        self._last_seen: Dict[str, dict] = {}
        self._misses: Dict[str, int] = {}
        self._req_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._last_request = 0.0
        self._backoff = 0.0
        self._backoff_until = 0.0
        self._timeouts = 0            # consecutive timed-out requests
        self._failure_logged = False
        self.last_success: Optional[float] = None  # wall-clock (time.time())
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ http

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        return self._session

    @property
    def backoff_remaining(self) -> float:
        return max(0.0, self._backoff_until - time.monotonic())

    @staticmethod
    def _ttl_from_headers(headers) -> float:
        directives = {}
        for part in headers.get("Cache-Control", "").split(","):
            key, _, value = part.strip().partition("=")
            if value.isdigit():
                directives[key.lower()] = float(value)
        ttl = directives.get("s-maxage", directives.get("max-age", MIN_TTL))
        return max(MIN_TTL, ttl)

    def _fail(
        self, reason: str, retry_after: Optional[float] = None, timeout: bool = False
    ) -> APIError:
        self._backoff = min(BACKOFF_MAX, max(BACKOFF_START, self._backoff * 2))
        delay = max(self._backoff, retry_after or 0.0)
        self._backoff_until = time.monotonic() + delay
        self.last_error = f"{reason} (backing off {int(delay)}s)"
        self._timeouts = self._timeouts + 1 if timeout else 0
        if not timeout or self._timeouts >= TIMEOUT_LOG_AFTER:
            suffix = f" [{self._timeouts} timeouts in a row]" if timeout else ""
            log.warning("gameserve.rs request failed: %s%s", self.last_error, suffix)
            self._failure_logged = True
        return APIError(reason)

    def _ok(self) -> None:
        if self._failure_logged:
            log.info("gameserve.rs requests are succeeding again.")
        self._failure_logged = False
        self._timeouts = 0
        self._backoff = 0.0
        self._backoff_until = 0.0
        self.last_success = time.time()

    async def _get(self, path: str, params: dict) -> Tuple[int, dict, float]:
        if self.backoff_remaining:
            raise APIError(f"backing off for another {int(self.backoff_remaining)}s")
        async with self._req_lock:
            wait = self._last_request + MIN_REQUEST_SPACING - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            try:
                async with self._get_session().get(API_BASE + path, params=params) as resp:
                    ttl = self._ttl_from_headers(resp.headers)
                    if resp.status == 429 or resp.status >= 500:
                        retry = resp.headers.get("Retry-After", "")
                        raise self._fail(
                            f"HTTP {resp.status}", float(retry) if retry.isdigit() else None
                        )
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = None
                    if resp.status not in (200, 404) or not isinstance(body, dict):
                        raise self._fail(f"unexpected HTTP {resp.status} response")
                    self._ok()
                    return resp.status, body, ttl
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise self._fail(
                    str(exc) or type(exc).__name__,
                    timeout=isinstance(exc, asyncio.TimeoutError),
                ) from exc

    # ----------------------------------------------------------------- cache

    def _store(self, address: str, data: Optional[dict], ttl: float, now: float) -> None:
        self._cache[address] = _CacheEntry(data, now, ttl)
        if data is not None:
            self._last_seen[address] = data
            self._misses[address] = 0
        else:
            self._misses[address] = self._misses.get(address, 0) + 1

    def state(self, address: str) -> Tuple[str, Optional[dict]]:
        """
        Returns (state, server_dict) where state is "online", "offline" or
        "unknown" (never fetched). server_dict is the live data, or the last
        known data for offline servers (may be None).
        """
        entry = self._cache.get(address)
        last = self._last_seen.get(address)
        if entry is None:
            return "unknown", last
        if entry.data is not None:
            return "online", entry.data
        # Ride out a single missed master-server poll for servers we've seen.
        if last is not None and self._misses.get(address, 0) < OFFLINE_AFTER_MISSES:
            return "online", last
        return "offline", last

    def _prune(self, keep: Iterable[str]) -> None:
        keep = set(keep)
        cutoff = time.monotonic() - CACHE_PRUNE_AGE
        for addr in [a for a, e in self._cache.items() if a not in keep and e.fetched_at < cutoff]:
            self._cache.pop(addr, None)
            self._last_seen.pop(addr, None)
            self._misses.pop(addr, None)

    # ------------------------------------------------------------ endpoints

    async def lookup(self, address: str) -> Optional[dict]:
        """Return server data for one address (None if not listed). Cached."""
        entry = self._cache.get(address)
        if entry and entry.fresh:
            return entry.data
        status, body, ttl = await self._get("/server/lookup", {"address": address})
        if status == 404:
            data = None
        elif body.get("success"):
            data = (body.get("data") or {}).get("server")
        else:
            raise APIError(body.get("message") or body.get("error") or "lookup failed")
        self._store(address, data, ttl, time.monotonic())
        return data

    async def _bulk(self, wanted: Set[str]) -> None:
        found: Dict[str, dict] = {}
        complete = False
        ttl = MIN_TTL
        offset = 0
        for _ in range(BULK_MAX_PAGES):
            status, body, ttl = await self._get(
                "/servers", {"platform": "plutonium", "limit": BULK_PAGE_LIMIT, "offset": offset}
            )
            if status != 200 or not body.get("success"):
                raise APIError("unexpected response from /servers")
            data = body.get("data") or {}
            for server in data.get("servers") or []:
                addr = normalize_address(str(server.get("address", "")))
                if addr in wanted:
                    found[addr] = server
            if found.keys() >= wanted or not (data.get("pagination") or {}).get("hasMore"):
                complete = True
                break
            offset += BULK_PAGE_LIMIT
        now = time.monotonic()
        for addr in wanted:
            # Only mark missing servers offline if we actually saw the full list.
            if addr in found or complete:
                self._store(addr, found.get(addr), ttl, now)

    async def refresh(self, addresses: Iterable[str]) -> None:
        """Make sure every address has fresh data. Raises APIError on failure."""
        addresses = set(addresses)
        async with self._refresh_lock:
            self._prune(addresses)
            stale = {a for a in addresses if not (a in self._cache and self._cache[a].fresh)}
            if not stale:
                return
            if len(stale) > BULK_THRESHOLD:
                await self._bulk(stale)
            else:
                for addr in sorted(stale):
                    await self.lookup(addr)
