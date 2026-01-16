"""
Data enrichment for PolyTracker.

Fetches additional metadata to make alerts more informative:
- Market title and slug from Polymarket API
- Current market prices/odds
- Caching to minimize API calls
"""

import asyncio
import logging
import time
from typing import Dict, Optional

import aiohttp

from .models import Signal, MarketInfo

logger = logging.getLogger(__name__)


class MarketEnricher:
    """
    Enriches signals with market metadata from Polymarket API.

    Caches market info to minimize API calls.
    """

    # Polymarket Data API base URL
    API_BASE = "https://data-api.polymarket.com"

    # CLOB API for market data
    CLOB_API_BASE = "https://clob.polymarket.com"

    def __init__(self, cache_ttl: int = 3600):
        """
        Initialize the enricher.

        Args:
            cache_ttl: Cache time-to-live in seconds (default 1 hour)
        """
        self.cache_ttl = cache_ttl

        # Cache: condition_id -> (MarketInfo, timestamp)
        self.market_cache: Dict[str, tuple[MarketInfo, float]] = {}

        # Cache: asset_id -> condition_id mapping
        self.asset_to_market: Dict[str, str] = {}

        # Rate limiting
        self.last_api_call = 0
        self.min_call_interval = 0.2  # 200ms between API calls

    async def enrich_signal(self, signal: Signal) -> Signal:
        """
        Enrich a signal with market metadata.

        Fetches market title, slug, and current prices.
        """
        trade = signal.trade

        try:
            # Get market info (from cache or API)
            market_info = await self._get_market_info(trade.market, trade.asset_id)

            if market_info:
                signal.market_title = market_info.question
                signal.market_slug = market_info.slug

            # Get current prices
            prices = await self._get_current_prices(trade.asset_id)
            if prices:
                signal.current_yes_price = prices.get("yes")
                signal.current_no_price = prices.get("no")

        except Exception as e:
            logger.warning(f"Enrichment failed: {e}")

        return signal

    async def _get_market_info(self, condition_id: str, asset_id: str) -> Optional[MarketInfo]:
        """Get market info from cache or API."""
        # Check cache first
        cached = self._get_cached_market(condition_id)
        if cached:
            return cached

        # Fetch from API
        await self._rate_limit()

        try:
            async with aiohttp.ClientSession() as session:
                # Try CLOB API first for market metadata
                url = f"{self.CLOB_API_BASE}/markets/{condition_id}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        market_info = self._parse_market_data(data)
                        if market_info:
                            self._cache_market(condition_id, market_info)
                            return market_info

        except Exception as e:
            logger.debug(f"API fetch failed for {condition_id[:10]}...: {e}")

        return None

    async def _get_current_prices(self, asset_id: str) -> Optional[Dict[str, float]]:
        """Get current market prices for an asset."""
        await self._rate_limit()

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.CLOB_API_BASE}/price"
                params = {"token_id": asset_id}

                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = float(data.get("price", 0))
                        return {
                            "yes": price,
                            "no": 1 - price,
                        }

        except Exception as e:
            logger.debug(f"Price fetch failed for {asset_id[:10]}...: {e}")

        return None

    def _parse_market_data(self, data: dict) -> Optional[MarketInfo]:
        """Parse market data from API response."""
        try:
            return MarketInfo(
                condition_id=data.get("condition_id", ""),
                question=data.get("question", data.get("title", "")),
                slug=data.get("market_slug", data.get("slug", "")),
                yes_token_id=data.get("tokens", [{}])[0].get("token_id", ""),
                no_token_id=data.get("tokens", [{}])[1].get("token_id", "") if len(data.get("tokens", [])) > 1 else "",
            )
        except Exception as e:
            logger.debug(f"Failed to parse market data: {e}")
            return None

    def _get_cached_market(self, condition_id: str) -> Optional[MarketInfo]:
        """Get market info from cache if still valid."""
        if condition_id not in self.market_cache:
            return None

        market_info, timestamp = self.market_cache[condition_id]
        if time.time() - timestamp > self.cache_ttl:
            del self.market_cache[condition_id]
            return None

        return market_info

    def _cache_market(self, condition_id: str, market_info: MarketInfo):
        """Cache market info."""
        self.market_cache[condition_id] = (market_info, time.time())

        # Prevent unbounded cache growth
        if len(self.market_cache) > 1000:
            # Remove oldest entries
            sorted_entries = sorted(
                self.market_cache.items(),
                key=lambda x: x[1][1]
            )
            for cid, _ in sorted_entries[:100]:
                del self.market_cache[cid]

    async def _rate_limit(self):
        """Enforce minimum interval between API calls."""
        now = time.time()
        elapsed = now - self.last_api_call
        if elapsed < self.min_call_interval:
            await asyncio.sleep(self.min_call_interval - elapsed)
        self.last_api_call = time.time()


# Global enricher instance
market_enricher = MarketEnricher()
