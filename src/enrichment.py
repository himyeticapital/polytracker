"""
Data enrichment for PolyTracker.

Fetches additional metadata to make alerts more informative:
- Market title and slug from Polymarket API
- Current market prices/odds
- Market end dates for timing detection
- Wallet profile data (P&L, win rate)
- Caching to minimize API calls
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, Optional, TYPE_CHECKING

import aiohttp

from .models import Signal, MarketInfo

if TYPE_CHECKING:
    from .signals import SignalDetector

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

    # Polymarket profile API
    PROFILE_API_BASE = "https://polymarket.com/api/profile"

    def __init__(self, cache_ttl: int = 3600, signal_detector: Optional["SignalDetector"] = None):
        """
        Initialize the enricher.

        Args:
            cache_ttl: Cache time-to-live in seconds (default 1 hour)
            signal_detector: Reference to signal detector for updating market cache
        """
        self.cache_ttl = cache_ttl
        self.signal_detector = signal_detector

        # Cache: condition_id -> (MarketInfo, timestamp)
        self.market_cache: Dict[str, tuple[MarketInfo, float]] = {}

        # Cache: asset_id -> condition_id mapping
        self.asset_to_market: Dict[str, str] = {}

        # Wallet profile cache: address -> (profile_data, timestamp)
        self.wallet_cache: Dict[str, tuple[dict, float]] = {}
        self.wallet_cache_ttl = 300  # 5 minutes for wallet data

        # Rate limiting
        self.last_api_call = 0
        self.min_call_interval = 0.2  # 200ms between API calls

    def set_signal_detector(self, detector: "SignalDetector"):
        """Set reference to signal detector for cache updates."""
        self.signal_detector = detector

    async def enrich_signal(self, signal: Signal) -> Signal:
        """
        Enrich a signal with market metadata and wallet profile.

        Fetches market title, slug, current prices, end date, and wallet P&L.
        Also updates the signal detector's market cache for future detections.
        """
        trade = signal.trade

        try:
            # Get market info (from cache or API)
            market_info = await self._get_market_info(trade.market, trade.asset_id)

            if market_info:
                signal.market_title = market_info.question
                signal.market_slug = market_info.slug
                signal.market_end_date = market_info.end_date

            # Get current prices
            prices = await self._get_current_prices(trade.asset_id)
            if prices:
                signal.current_yes_price = prices.get("yes")
                signal.current_no_price = prices.get("no")

                # Update signal detector's market cache for timing/contrarian detection
                if self.signal_detector:
                    self.signal_detector.update_market_metadata(
                        condition_id=trade.market,
                        end_date=market_info.end_date if market_info else None,
                        yes_price=prices.get("yes"),
                        no_price=prices.get("no"),
                    )

            # Fetch wallet profile if available
            if trade.taker_address:
                profile = await self._get_wallet_profile(trade.taker_address)
                if profile:
                    signal.wallet_profit_loss = profile.get("profit_loss")
                    signal.wallet_win_rate = profile.get("win_rate")
                    signal.wallet_total_trades = profile.get("total_trades")
                    signal.wallet_volume = profile.get("volume")

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
            # Parse end date from various possible field names
            end_date = None
            end_date_str = data.get("end_date_iso") or data.get("end_date") or data.get("closed_time")
            if end_date_str:
                try:
                    # Handle ISO format with or without timezone
                    if isinstance(end_date_str, str):
                        end_date_str = end_date_str.replace("Z", "+00:00")
                        end_date = datetime.fromisoformat(end_date_str)
                except (ValueError, TypeError):
                    pass

            return MarketInfo(
                condition_id=data.get("condition_id", ""),
                question=data.get("question", data.get("title", "")),
                slug=data.get("market_slug", data.get("slug", "")),
                yes_token_id=data.get("tokens", [{}])[0].get("token_id", ""),
                no_token_id=data.get("tokens", [{}])[1].get("token_id", "") if len(data.get("tokens", [])) > 1 else "",
                end_date=end_date,
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

    async def _get_wallet_profile(self, address: str) -> Optional[dict]:
        """
        Fetch wallet profile data from Polymarket.

        Returns profit/loss, win rate, and trading volume.
        """
        address = address.lower()

        # Check cache first
        if address in self.wallet_cache:
            profile, timestamp = self.wallet_cache[address]
            if time.time() - timestamp < self.wallet_cache_ttl:
                return profile

        await self._rate_limit()

        try:
            async with aiohttp.ClientSession() as session:
                # Polymarket profile API endpoint
                url = f"{self.PROFILE_API_BASE}/{address}"
                headers = {"Accept": "application/json"}

                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        # Extract relevant profile data
                        profile = {
                            "profit_loss": data.get("pnl") or data.get("profit_loss") or data.get("totalPnl"),
                            "win_rate": data.get("win_rate") or data.get("winRate"),
                            "total_trades": data.get("total_trades") or data.get("tradesCount") or data.get("numTrades"),
                            "volume": data.get("volume") or data.get("totalVolume"),
                        }

                        # Convert win rate to decimal if percentage
                        if profile["win_rate"] and profile["win_rate"] > 1:
                            profile["win_rate"] = profile["win_rate"] / 100

                        # Cache the result
                        self.wallet_cache[address] = (profile, time.time())

                        # Clean old cache entries
                        if len(self.wallet_cache) > 500:
                            cutoff = time.time() - self.wallet_cache_ttl
                            self.wallet_cache = {
                                k: v for k, v in self.wallet_cache.items()
                                if v[1] > cutoff
                            }

                        return profile

        except Exception as e:
            logger.debug(f"Wallet profile fetch failed for {address[:10]}...: {e}")

        return None

    async def _rate_limit(self):
        """Enforce minimum interval between API calls."""
        now = time.time()
        elapsed = now - self.last_api_call
        if elapsed < self.min_call_interval:
            await asyncio.sleep(self.min_call_interval - elapsed)
        self.last_api_call = time.time()


# Global enricher instance
market_enricher = MarketEnricher()
