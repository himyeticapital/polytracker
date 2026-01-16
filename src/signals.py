"""
Signal detection logic for PolyTracker.

Implements the "Intelligence" stage that identifies noteworthy trading patterns:
- Signal A: Whale detection (size anomaly)
- Signal B: Fresh wallet detection (insider indicator)
- Signal C: Cluster detection (coordinated buying)
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Deque, List, Optional, Set

from web3 import Web3
from web3.exceptions import Web3Exception

from .config import config, FilterConfig
from .models import Trade, Signal, SignalType, TradeSide

logger = logging.getLogger(__name__)


@dataclass
class ClusterRecord:
    """Record of a trade for cluster detection."""
    wallet: str
    side: TradeSide
    timestamp: int  # milliseconds
    usd_value: float


class WalletChecker:
    """
    Efficient wallet verification using Polygon RPC.

    Caches wallet transaction counts to minimize RPC calls.
    Uses async batching for multiple wallet checks.
    """

    def __init__(self, rpc_url: str, max_txs_threshold: int = 10):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.max_txs = max_txs_threshold

        # Cache: wallet_address -> (tx_count, cache_timestamp)
        self.cache: Dict[str, tuple[int, float]] = {}
        self.cache_ttl = 3600  # 1 hour cache TTL

        # Rate limiting
        self.last_rpc_call = 0
        self.min_call_interval = 0.1  # 100ms between RPC calls

    async def is_fresh_wallet(self, address: str) -> tuple[bool, int]:
        """
        Check if a wallet is "fresh" (low transaction count).

        A fresh wallet with a large trade is a strong insider indicator,
        as it suggests a burner account created specifically for this trade.

        Args:
            address: Ethereum/Polygon wallet address

        Returns:
            Tuple of (is_fresh, tx_count)
        """
        if not address:
            return False, -1

        address = Web3.to_checksum_address(address)

        # Check cache first
        cached = self._get_cached(address)
        if cached is not None:
            return cached <= self.max_txs, cached

        # Rate limit RPC calls
        await self._rate_limit()

        try:
            # get_transaction_count returns the nonce (number of sent transactions)
            tx_count = await asyncio.to_thread(
                self.w3.eth.get_transaction_count, address
            )

            # Cache the result
            self._set_cached(address, tx_count)

            is_fresh = tx_count <= self.max_txs
            if is_fresh:
                logger.info(f"Fresh wallet detected: {address[:10]}... ({tx_count} txs)")

            return is_fresh, tx_count

        except Web3Exception as e:
            logger.warning(f"RPC error checking wallet {address[:10]}...: {e}")
            return False, -1
        except Exception as e:
            logger.error(f"Unexpected error checking wallet: {e}")
            return False, -1

    async def batch_check_wallets(self, addresses: List[str]) -> Dict[str, tuple[bool, int]]:
        """
        Check multiple wallets efficiently.

        Args:
            addresses: List of wallet addresses to check

        Returns:
            Dict mapping address to (is_fresh, tx_count)
        """
        results = {}
        for address in addresses:
            if address:
                results[address] = await self.is_fresh_wallet(address)
        return results

    def _get_cached(self, address: str) -> Optional[int]:
        """Get cached transaction count if still valid."""
        if address not in self.cache:
            return None

        tx_count, timestamp = self.cache[address]
        if time.time() - timestamp > self.cache_ttl:
            del self.cache[address]
            return None

        return tx_count

    def _set_cached(self, address: str, tx_count: int):
        """Cache a wallet's transaction count."""
        self.cache[address] = (tx_count, time.time())

        # Prevent unbounded cache growth
        if len(self.cache) > 10000:
            # Remove oldest entries
            sorted_entries = sorted(
                self.cache.items(),
                key=lambda x: x[1][1]
            )
            for addr, _ in sorted_entries[:1000]:
                del self.cache[addr]

    async def _rate_limit(self):
        """Enforce minimum interval between RPC calls."""
        now = time.time()
        elapsed = now - self.last_rpc_call
        if elapsed < self.min_call_interval:
            await asyncio.sleep(self.min_call_interval - elapsed)
        self.last_rpc_call = time.time()


class SignalDetector:
    """
    Main signal detection engine.

    Analyzes filtered trades for patterns indicating informed trading:
    - Whale trades (unusual size)
    - Fresh wallets (likely burner accounts)
    - Coordinated cluster buying
    """

    def __init__(self, filter_config: Optional[FilterConfig] = None, rpc_url: Optional[str] = None):
        self.config = filter_config or config.filters

        # Initialize wallet checker
        rpc = rpc_url or config.blockchain.rpc_url
        self.wallet_checker = WalletChecker(
            rpc_url=rpc,
            max_txs_threshold=self.config.fresh_wallet_max_txs
        )

        # Rolling window of recent trade sizes for anomaly detection
        self.recent_trade_sizes: Deque[float] = deque(maxlen=100)

        # Cluster detection: market -> asset_id -> list of ClusterRecords
        self.cluster_tracker: Dict[str, Dict[str, Deque[ClusterRecord]]] = {}

        # Stats
        self.stats = {
            "trades_analyzed": 0,
            "signals_generated": 0,
            "whale_signals": 0,
            "fresh_wallet_signals": 0,
            "cluster_signals": 0,
        }

    async def analyze_trade(self, trade: Trade) -> Optional[Signal]:
        """
        Analyze a trade for signal patterns.

        Runs all detectors and returns a Signal if any pattern matches.

        Args:
            trade: The trade to analyze

        Returns:
            Signal object if patterns detected, None otherwise
        """
        self.stats["trades_analyzed"] += 1
        signal_types: List[SignalType] = []
        wallet_tx_count: Optional[int] = None
        cluster_wallets: List[str] = []

        # Update rolling window for average calculation
        self.recent_trade_sizes.append(trade.usd_value)

        # Detection A: Whale / Size Anomaly
        is_whale, is_anomaly = self._detect_whale(trade)
        if is_whale:
            signal_types.append(SignalType.WHALE)
            self.stats["whale_signals"] += 1
        if is_anomaly:
            signal_types.append(SignalType.SIZE_ANOMALY)

        # Detection B: Fresh Wallet (async RPC call)
        if trade.taker_address:
            is_fresh, tx_count = await self.wallet_checker.is_fresh_wallet(trade.taker_address)
            wallet_tx_count = tx_count
            if is_fresh:
                signal_types.append(SignalType.FRESH_WALLET)
                self.stats["fresh_wallet_signals"] += 1

        # Detection C: Cluster
        cluster_detected, wallets = self._detect_cluster(trade)
        if cluster_detected:
            signal_types.append(SignalType.CLUSTER)
            cluster_wallets = wallets
            self.stats["cluster_signals"] += 1

        # Return Signal only if at least one pattern matched
        if not signal_types:
            return None

        self.stats["signals_generated"] += 1

        # Calculate confidence based on signal combination
        confidence = self._calculate_confidence(signal_types, trade)

        return Signal(
            trade=trade,
            signal_types=signal_types,
            wallet_tx_count=wallet_tx_count,
            cluster_wallets=cluster_wallets,
            avg_trade_size=self._get_avg_trade_size(),
            confidence=confidence,
        )

    def _detect_whale(self, trade: Trade) -> tuple[bool, bool]:
        """
        Detect whale activity based on trade size.

        Returns:
            Tuple of (is_absolute_whale, is_relative_anomaly)
        """
        usd_value = trade.usd_value

        # Absolute whale threshold
        is_whale = usd_value >= self.config.whale_threshold_usd

        # Relative anomaly: significantly above average
        avg_size = self._get_avg_trade_size()
        is_anomaly = False
        if avg_size and avg_size > 0:
            is_anomaly = usd_value >= (avg_size * self.config.whale_multiplier)

        return is_whale, is_anomaly

    def _detect_cluster(self, trade: Trade) -> tuple[bool, List[str]]:
        """
        Detect coordinated cluster buying.

        Pattern: 3+ distinct wallets buy the same outcome
        in the same market within 60 seconds.
        """
        if not trade.taker_address:
            return False, []

        market = trade.market
        asset_id = trade.asset_id
        window_ms = self.config.cluster_window_seconds * 1000
        min_wallets = self.config.cluster_min_wallets

        # Initialize tracking structures
        if market not in self.cluster_tracker:
            self.cluster_tracker[market] = {}
        if asset_id not in self.cluster_tracker[market]:
            self.cluster_tracker[market][asset_id] = deque(maxlen=500)

        records = self.cluster_tracker[market][asset_id]
        current_time = trade.timestamp

        # Clean old records
        while records and (current_time - records[0].timestamp) > window_ms:
            records.popleft()

        # Add current trade
        records.append(ClusterRecord(
            wallet=trade.taker_address.lower(),
            side=trade.side,
            timestamp=current_time,
            usd_value=trade.usd_value,
        ))

        # Count distinct wallets trading in same direction within window
        same_direction_wallets: Set[str] = set()
        for record in records:
            if record.side == trade.side:
                same_direction_wallets.add(record.wallet)

        if len(same_direction_wallets) >= min_wallets:
            logger.info(
                f"Cluster detected: {len(same_direction_wallets)} wallets "
                f"trading {trade.side.value} on {market[:10]}..."
            )
            return True, list(same_direction_wallets)

        return False, []

    def _get_avg_trade_size(self) -> float:
        """Calculate average trade size from rolling window."""
        if not self.recent_trade_sizes:
            return 0.0
        return sum(self.recent_trade_sizes) / len(self.recent_trade_sizes)

    def _calculate_confidence(self, signal_types: List[SignalType], trade: Trade) -> float:
        """
        Calculate signal confidence score (0-1).

        Higher confidence for:
        - Multiple signal types
        - Larger trade sizes
        - Fresh wallet + whale combination
        """
        base_confidence = 0.5

        # Bonus for multiple signals
        if len(signal_types) >= 3:
            base_confidence += 0.3
        elif len(signal_types) >= 2:
            base_confidence += 0.2

        # Special combo: whale + fresh wallet
        if SignalType.WHALE in signal_types and SignalType.FRESH_WALLET in signal_types:
            base_confidence += 0.15

        # Size bonus
        if trade.usd_value >= 50000:
            base_confidence += 0.15
        elif trade.usd_value >= 25000:
            base_confidence += 0.1
        elif trade.usd_value >= 10000:
            base_confidence += 0.05

        return min(base_confidence, 1.0)

    def get_stats(self) -> Dict[str, int]:
        """Return detection statistics."""
        return self.stats.copy()

    def cleanup_old_clusters(self, max_age_seconds: int = 300):
        """Remove old cluster tracking data to prevent memory bloat."""
        cutoff = int((time.time() - max_age_seconds) * 1000)
        markets_to_remove = []

        for market, assets in self.cluster_tracker.items():
            assets_to_remove = []
            for asset_id, records in assets.items():
                while records and records[0].timestamp < cutoff:
                    records.popleft()
                if not records:
                    assets_to_remove.append(asset_id)

            for asset_id in assets_to_remove:
                del assets[asset_id]

            if not assets:
                markets_to_remove.append(market)

        for market in markets_to_remove:
            del self.cluster_tracker[market]


# Global signal detector instance
signal_detector = SignalDetector()
