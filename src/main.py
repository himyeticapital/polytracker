"""
PolyTracker - Polymarket Insider Activity Surveillance Bot

Main entry point that orchestrates:
1. WebSocket connection to Polymarket CLOB
2. Trade filtering pipeline
3. Signal detection
4. Alert notifications to Discord/Telegram
"""

import asyncio
import logging
import signal
import sys
from typing import Optional

from .config import config
from .models import Trade, Signal
from .filters import trade_filter
from .signals import signal_detector
from .alerts import alert_manager
from .enrichment import market_enricher
from .websocket_client import PolymarketWebSocket

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Reduce noise from libraries
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


class PolyTracker:
    """
    Main application class for PolyTracker.

    Coordinates all components:
    - WebSocket client for trade ingestion
    - Filter pipeline for noise reduction
    - Signal detector for pattern recognition
    - Alert manager for notifications
    """

    def __init__(self, asset_ids: Optional[list] = None):
        """
        Initialize PolyTracker.

        Args:
            asset_ids: List of token IDs to monitor.
                       If None, will need to subscribe later.
        """
        self.asset_ids = asset_ids or []
        self.ws_client: Optional[PolymarketWebSocket] = None
        self._running = False
        self._stats_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the PolyTracker bot."""
        logger.info("=" * 60)
        logger.info("PolyTracker - Polymarket Insider Activity Surveillance")
        logger.info("=" * 60)

        self._running = True

        # Log configuration
        logger.info(f"Min USD size filter: ${config.filters.min_usd_size:,.0f}")
        logger.info(f"Whale threshold: ${config.filters.whale_threshold_usd:,.0f}")
        logger.info(f"Fresh wallet max txs: {config.filters.fresh_wallet_max_txs}")
        logger.info(f"Cluster window: {config.filters.cluster_window_seconds}s")
        logger.info(f"Excluded keywords: {config.filters.exclude_market_keywords}")

        # Check alert configuration
        if config.alerts.discord_webhook_url:
            logger.info("Discord alerts: ENABLED")
        else:
            logger.warning("Discord alerts: DISABLED (no webhook URL)")

        if config.alerts.telegram_bot_token and config.alerts.telegram_chat_id:
            logger.info("Telegram alerts: ENABLED")
        else:
            logger.warning("Telegram alerts: DISABLED (missing token or chat ID)")

        # Connect enricher to signal detector for market cache updates
        market_enricher.set_signal_detector(signal_detector)

        # Start alert manager
        await alert_manager.start()

        # Create WebSocket client
        logger.info(f"Passing {len(self.asset_ids)} asset IDs to WebSocket client")
        self.ws_client = PolymarketWebSocket(
            on_trade=self._on_trade,
            asset_ids=self.asset_ids,
        )

        # Start periodic stats logging
        self._stats_task = asyncio.create_task(self._log_stats_periodically())

        # Connect and run
        logger.info("Starting WebSocket connection...")
        await self.ws_client.connect()

    async def stop(self):
        """Stop the PolyTracker bot."""
        logger.info("Shutting down PolyTracker...")
        self._running = False

        if self._stats_task:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except asyncio.CancelledError:
                pass

        if self.ws_client:
            await self.ws_client.disconnect()

        await alert_manager.stop()

        # Log final stats
        self._log_stats()
        logger.info("PolyTracker stopped")

    async def subscribe(self, asset_ids: list):
        """Subscribe to additional asset IDs."""
        self.asset_ids.extend(asset_ids)
        if self.ws_client:
            await self.ws_client.subscribe(asset_ids)

    async def _on_trade(self, trade: Trade, raw_data: dict):
        """
        Process a trade through the pipeline.

        Pipeline stages:
        1. Filter (drop noise)
        2. Detect signals
        3. Enrich with metadata
        4. Send alerts
        """
        # Log all trades >= $100 to see what's coming in
        if trade.usd_value >= 100:
            logger.info(f"Trade received: ${trade.usd_value:,.0f} {trade.side.value} @ {trade.price:.2f}")

        # Stage 1: Filter
        market_slug = raw_data.get("market_slug", "")
        should_pass, reason = trade_filter.should_pass(trade, market_slug)

        if not should_pass:
            # Log trades that were close to threshold
            if trade.usd_value >= 500:
                logger.info(f"Trade filtered (${trade.usd_value:,.0f}): {reason}")
            return

        # Stage 2: Signal Detection
        detected_signal = await signal_detector.analyze_trade(trade)

        if not detected_signal:
            logger.debug(f"No signal detected for ${trade.usd_value:,.0f} trade")
            return

        # Stage 3: Enrichment
        enriched_signal = await market_enricher.enrich_signal(detected_signal)

        # Log the signal
        logger.info(
            f"SIGNAL: {enriched_signal.signal_emoji} "
            f"${trade.usd_value:,.0f} {trade.side.value} "
            f"@ {trade.price:.2f} "
            f"[{', '.join(st.value for st in enriched_signal.signal_types)}] "
            f"conf={enriched_signal.confidence:.0%}"
        )

        # Stage 4: Alert
        await alert_manager.send_alert(enriched_signal)

    async def _log_stats_periodically(self):
        """Log statistics every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)  # 5 minutes
            self._log_stats()

    def _log_stats(self):
        """Log current statistics from all components."""
        ws_stats = self.ws_client.get_stats() if self.ws_client else {}
        filter_stats = trade_filter.get_stats()
        signal_stats = signal_detector.get_stats()
        alert_stats = alert_manager.get_stats()

        logger.info("-" * 40)
        logger.info("STATISTICS")
        logger.info(f"  WebSocket: {ws_stats.get('trades_received', 0)} trades received")
        logger.info(
            f"  Filtered: {filter_stats.get('passed', 0)}/{filter_stats.get('total_received', 0)} passed "
            f"(market={filter_stats.get('filtered_market', 0)}, "
            f"size={filter_stats.get('filtered_size', 0)}, "
            f"lp={filter_stats.get('filtered_lp', 0)})"
        )
        logger.info(
            f"  Signals: {signal_stats.get('signals_generated', 0)} generated "
            f"(whale={signal_stats.get('whale_signals', 0)}, "
            f"fresh={signal_stats.get('fresh_wallet_signals', 0)}, "
            f"cluster={signal_stats.get('cluster_signals', 0)}, "
            f"timing={signal_stats.get('timing_signals', 0)}, "
            f"odds={signal_stats.get('odds_movement_signals', 0)}, "
            f"contrarian={signal_stats.get('contrarian_signals', 0)})"
        )
        logger.info(
            f"  Alerts: {alert_stats.get('alerts_sent', 0)} sent "
            f"(discord={alert_stats.get('discord_sent', 0)}, "
            f"telegram={alert_stats.get('telegram_sent', 0)}, "
            f"queue={alert_stats.get('queue_size', 0)})"
        )
        logger.info("-" * 40)


async def fetch_top_markets(limit: int = 50) -> list:
    """
    Fetch top markets by volume from Polymarket API.

    Returns list of asset IDs to subscribe to.
    """
    import aiohttp

    logger.info(f"Fetching top {limit} markets by volume...")

    try:
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Fetch active markets
            url = "https://gamma-api.polymarket.com/markets"
            params = {
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            }

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Failed to fetch markets: {resp.status}")
                    return []

                markets = await resp.json()
                asset_ids = []

                for market in markets:
                    # Get token IDs from market
                    # clobTokenIds can be a JSON string or a list
                    clob_token_ids = market.get("clobTokenIds", [])
                    if isinstance(clob_token_ids, str):
                        # Parse JSON string to list
                        import json as json_module
                        try:
                            clob_token_ids = json_module.loads(clob_token_ids)
                        except:
                            clob_token_ids = []
                    if clob_token_ids:
                        asset_ids.extend(clob_token_ids)

                logger.info(f"Found {len(asset_ids)} asset IDs from {len(markets)} markets")
                return asset_ids

    except Exception as e:
        logger.error(f"Error fetching markets: {e}")
        logger.info("Using fallback popular market IDs...")
        # Fallback: Popular market token IDs (Trump, crypto, sports, etc.)
        # These are real token IDs from active Polymarket markets
        return get_fallback_asset_ids()


def get_fallback_asset_ids() -> list:
    """Return hardcoded fallback asset IDs for popular markets."""
    # These are real token IDs from popular Polymarket markets
    # Update periodically for best coverage
    return [
        # Trump 2024 markets
        "21742633143463906290569050155826241533067272736897614950488156847949938836455",
        "48331043336612883890938759509493159234755048973500640148014422747788308965732",
        # Bitcoin price markets
        "52114319501245915516055106046884209969926127482827954674443846427813813082042",
        "69236923620077691027083946871148646972011131466059644796654161903044970987404",
        # Other popular markets - add more as needed
        "16678291189211314787145083999015737376658799626183230671758641503291735614088",
        "1343197538147866997676150392353",
        "21742633143463906290569050155826241533067272736897614950488156847949938836455",
    ]


async def send_test_watched_wallet_alert():
    """Send a test watched wallet alert on startup."""
    import time
    from .models import Trade, Signal, SignalType, TradeSide

    logger.info("Sending test WATCHED WALLET alert...")

    trade = Trade(
        asset_id="98765432109876543210",
        market="0xdeadbeef12345678",
        price=0.72,
        size=25000,
        side=TradeSide.BUY,
        timestamp=int(time.time() * 1000),
        taker_address="0xSuspectedInsider1234567890abcdef12345678",
    )

    signal = Signal(
        trade=trade,
        signal_types=[SignalType.WATCHED_WALLET, SignalType.WHALE, SignalType.TIMING],
        confidence=0.92,
        market_title="[TEST] Will Bitcoin reach $150,000 by March 2026?",
        market_slug="bitcoin-150k-march-2026",
        current_yes_price=0.72,
        current_no_price=0.28,
        wallet_tx_count=47,
        hours_to_close=6.5,
        price_before_trade=0.68,
        price_after_trade=0.72,
        wallet_profit_loss=87500.0,
        wallet_win_rate=0.81,
        wallet_total_trades=62,
        wallet_volume=425000.0,
    )

    await alert_manager.start()
    await alert_manager.send_alert(signal)
    await asyncio.sleep(3)
    await alert_manager.stop()
    logger.info("Test alert sent!")


async def main():
    """Main entry point."""
    import os

    # Check if this is just a test alert run
    if os.getenv("SEND_TEST_ALERT", "").lower() in ("1", "true", "yes"):
        await send_test_watched_wallet_alert()
        return

    # Fetch top markets to subscribe to (200 markets = ~400 assets)
    # Note: Higher numbers can cause WebSocket message size limits
    asset_ids = await fetch_top_markets(limit=200)

    if not asset_ids:
        logger.warning("No markets fetched from API. Using fallback asset IDs.")
        asset_ids = get_fallback_asset_ids()

    # Create and start tracker
    tracker = PolyTracker(asset_ids=asset_ids)

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def shutdown_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(tracker.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    try:
        await tracker.start()
    except KeyboardInterrupt:
        pass
    finally:
        await tracker.stop()


def run():
    """Entry point for console script."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
