"""
Trade filtering logic for PolyTracker.

Implements the "Gatekeeper" stage that drops trades early based on:
1. Market whitelist/blacklist (exclude sports, crypto timeframes, etc.)
2. Minimum USD size threshold
3. LP and arbitrage detection
"""

import re
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Deque, Optional, Tuple

from .config import config, FilterConfig
from .models import Trade, TradeSide

logger = logging.getLogger(__name__)


@dataclass
class LPTradeRecord:
    """Record of a trade for LP detection."""
    wallet: str
    market: str
    side: TradeSide
    timestamp: int  # milliseconds


class TradeFilterPipeline:
    """
    Sequential filter pipeline that drops trades early to reduce processing load.

    Filter order (applied sequentially):
    1. Market keyword filter (drop excluded categories)
    2. Size filter (drop small trades below threshold)
    3. LP detection (drop balanced YES/NO trades from same wallet)
    """

    def __init__(self, filter_config: Optional[FilterConfig] = None):
        self.config = filter_config or config.filters

        # Compile keyword patterns for efficient matching
        self.exclude_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in self.config.exclude_market_keywords
        ]

        # LP detection: track recent trades per wallet
        # Key: wallet_address, Value: deque of LPTradeRecord
        self.recent_trades: Dict[str, Deque[LPTradeRecord]] = {}

        # Cleanup old entries periodically
        self.last_cleanup = time.time()
        self.cleanup_interval = 60  # seconds

        # Stats tracking
        self.stats = {
            "total_received": 0,
            "filtered_market": 0,
            "filtered_size": 0,
            "filtered_lp": 0,
            "passed": 0,
        }

    def should_pass(self, trade: Trade, market_slug: Optional[str] = None) -> Tuple[bool, str]:
        """
        Apply all filters to a trade.

        Args:
            trade: The trade to filter
            market_slug: Optional market slug/title for keyword filtering

        Returns:
            Tuple of (should_pass, reason) where reason explains why it was filtered
        """
        self.stats["total_received"] += 1

        # Run periodic cleanup
        self._maybe_cleanup()

        # Filter 1: Market keyword exclusion
        if market_slug and self._is_excluded_market(market_slug):
            self.stats["filtered_market"] += 1
            return False, f"excluded_market:{market_slug}"

        # Filter 2: Minimum size threshold
        if trade.usd_value < self.config.min_usd_size:
            self.stats["filtered_size"] += 1
            return False, f"below_min_size:{trade.usd_value:.2f}<{self.config.min_usd_size}"

        # Filter 3: LP/Arb detection
        if trade.taker_address and self._is_lp_activity(trade):
            self.stats["filtered_lp"] += 1
            return False, f"lp_activity:{trade.taker_address[:10]}"

        self.stats["passed"] += 1
        return True, "passed"

    def _is_excluded_market(self, market_slug: str) -> bool:
        """Check if market matches any exclusion patterns."""
        for pattern in self.exclude_patterns:
            if pattern.search(market_slug):
                return True
        return False

    def _is_lp_activity(self, trade: Trade) -> bool:
        """
        Detect LP (Liquidity Provider) activity.

        LP pattern: Same wallet buys both YES and NO on the same market
        within a short time window (< 200ms by default).

        This indicates market making rather than directional betting.
        """
        if not trade.taker_address:
            return False

        wallet = trade.taker_address.lower()
        market = trade.market
        window_ms = self.config.lp_detection_window_ms

        # Get or create trade history for this wallet
        if wallet not in self.recent_trades:
            self.recent_trades[wallet] = deque(maxlen=100)

        wallet_trades = self.recent_trades[wallet]
        current_time = trade.timestamp

        # Check for opposite-side trade on same market within window
        for record in wallet_trades:
            if record.market != market:
                continue

            time_diff = abs(current_time - record.timestamp)
            if time_diff > window_ms:
                continue

            # Found a trade on same market within window
            # Check if it's the opposite side
            if record.side != trade.side:
                logger.debug(
                    f"LP detected: {wallet[:10]}... traded both sides of {market[:10]} "
                    f"within {time_diff}ms"
                )
                return True

        # Record this trade for future comparison
        wallet_trades.append(LPTradeRecord(
            wallet=wallet,
            market=market,
            side=trade.side,
            timestamp=current_time,
        ))

        return False

    def _maybe_cleanup(self):
        """Periodically clean up old trade records to prevent memory bloat."""
        now = time.time()
        if now - self.last_cleanup < self.cleanup_interval:
            return

        self.last_cleanup = now
        cutoff = int((now - 60) * 1000)  # 60 seconds ago in milliseconds

        wallets_to_remove = []
        for wallet, trades in self.recent_trades.items():
            # Remove old trades from this wallet's history
            while trades and trades[0].timestamp < cutoff:
                trades.popleft()

            # Mark empty histories for removal
            if not trades:
                wallets_to_remove.append(wallet)

        for wallet in wallets_to_remove:
            del self.recent_trades[wallet]

        if wallets_to_remove:
            logger.debug(f"Cleaned up {len(wallets_to_remove)} inactive wallet histories")

    def get_stats(self) -> Dict[str, int]:
        """Return filtering statistics."""
        return self.stats.copy()

    def reset_stats(self):
        """Reset filtering statistics."""
        for key in self.stats:
            self.stats[key] = 0


# Global filter instance
trade_filter = TradeFilterPipeline()
