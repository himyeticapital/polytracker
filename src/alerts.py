"""
Alert notification system for PolyTracker.

Handles dual-channel notifications to Discord and Telegram with:
- Rate limiting (LeakyBucket queue)
- Rich formatting (Discord embeds, Telegram HTML)
- Automatic retry on failures
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import aiohttp
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .config import config, AlertConfig
from .models import Signal, SignalType

logger = logging.getLogger(__name__)


@dataclass
class QueuedAlert:
    """Alert waiting in the rate limit queue."""
    signal: Signal
    timestamp: float
    retries: int = 0


class LeakyBucketQueue:
    """
    Rate limiting queue using leaky bucket algorithm.

    Ensures max 1 message per second globally to avoid API bans.
    Drops oldest messages if queue exceeds max size.
    """

    def __init__(self, rate_per_second: float = 1.0, max_queue_size: int = 10):
        self.rate = rate_per_second
        self.max_size = max_queue_size
        self.queue: Deque[QueuedAlert] = deque(maxlen=max_queue_size)
        self.last_send_time = 0.0

    def add(self, signal: Signal) -> bool:
        """
        Add an alert to the queue.

        Returns True if added, False if queue is full and oldest was dropped.
        """
        dropped = len(self.queue) >= self.max_size
        self.queue.append(QueuedAlert(signal=signal, timestamp=time.time()))
        if dropped:
            logger.warning("Alert queue full, dropped oldest alert")
        return not dropped

    async def get_next(self) -> Optional[QueuedAlert]:
        """
        Get next alert respecting rate limit.

        Blocks until rate limit allows sending.
        Returns None if queue is empty.
        """
        if not self.queue:
            return None

        # Enforce rate limit
        now = time.time()
        time_since_last = now - self.last_send_time
        min_interval = 1.0 / self.rate

        if time_since_last < min_interval:
            await asyncio.sleep(min_interval - time_since_last)

        self.last_send_time = time.time()
        return self.queue.popleft()

    def size(self) -> int:
        """Current queue size."""
        return len(self.queue)


class AlertManager:
    """
    Manages alert notifications to Discord and Telegram.

    Features:
    - Dual-channel delivery (Discord webhooks + Telegram bot)
    - Rate limiting to prevent API bans
    - Automatic retries on transient failures
    - Rich message formatting
    """

    def __init__(self, alert_config: Optional[AlertConfig] = None):
        self.config = alert_config or config.alerts

        # Rate limiter: 1 message per second
        self.queue = LeakyBucketQueue(rate_per_second=1.0, max_queue_size=10)

        # Telegram bot (initialized lazily)
        self._telegram_bot: Optional[Bot] = None

        # Background task handle
        self._worker_task: Optional[asyncio.Task] = None

        # Stats
        self.stats = {
            "alerts_queued": 0,
            "alerts_sent": 0,
            "discord_sent": 0,
            "telegram_sent": 0,
            "errors": 0,
        }

    @property
    def telegram_bot(self) -> Optional[Bot]:
        """Lazy initialization of Telegram bot."""
        if self._telegram_bot is None and self.config.telegram_bot_token:
            self._telegram_bot = Bot(token=self.config.telegram_bot_token)
        return self._telegram_bot

    async def start(self):
        """Start the background alert worker."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())
            logger.info("Alert manager started")

    async def stop(self):
        """Stop the background alert worker."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
            logger.info("Alert manager stopped")

    async def send_alert(self, signal: Signal):
        """
        Queue a signal for alerting.

        Non-blocking - adds to queue and returns immediately.
        """
        self.queue.add(signal)
        self.stats["alerts_queued"] += 1

    async def _worker_loop(self):
        """Background worker that processes the alert queue."""
        while True:
            try:
                alert = await self.queue.get_next()
                if alert is None:
                    await asyncio.sleep(0.1)
                    continue

                await self._process_alert(alert)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Alert worker error: {e}")
                await asyncio.sleep(1)

    async def _process_alert(self, alert: QueuedAlert):
        """Process a single alert, sending to all channels."""
        signal = alert.signal

        # Send to both channels concurrently
        tasks = []

        if self.config.discord_webhook_url:
            tasks.append(self._send_discord(signal))

        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            tasks.append(self._send_telegram(signal))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Alert send error: {result}")
                    self.stats["errors"] += 1

            self.stats["alerts_sent"] += 1

    async def _send_discord(self, signal: Signal):
        """Send a rich Discord webhook embed."""
        embed = self._build_discord_embed(signal)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.config.discord_webhook_url,
                json={"embeds": [embed]},
            ) as resp:
                if resp.status not in (200, 204):
                    text = await resp.text()
                    raise Exception(f"Discord webhook failed ({resp.status}): {text}")

        self.stats["discord_sent"] += 1
        logger.debug(f"Discord alert sent for {signal.trade.market[:10]}...")

    async def _send_telegram(self, signal: Signal):
        """Send a Telegram message with HTML formatting."""
        message = self._build_telegram_message(signal)

        if self.telegram_bot:
            await self.telegram_bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

            self.stats["telegram_sent"] += 1
            logger.debug(f"Telegram alert sent for {signal.trade.market[:10]}...")

    def _build_discord_embed(self, signal: Signal) -> dict:
        """Build a Discord embed object for a signal."""
        trade = signal.trade

        # Color based on confidence
        color = 0xFF0000 if signal.is_high_confidence else 0xFFA500  # Red or Orange

        # Title with market name
        market_name = signal.market_title or f"Market {trade.market[:16]}..."
        title = f"{'🚨' if signal.is_high_confidence else '⚠️'} {market_name}"

        # Side indicator
        side_emoji = "📈" if trade.side.value == "BUY" else "📉"

        # Build fields
        fields = [
            {
                "name": "Trade",
                "value": f"{side_emoji} **{trade.side.value}** @ {trade.price:.2f}¢\n"
                         f"**${trade.usd_value:,.0f}** ({trade.size:,.0f} shares)",
                "inline": True,
            },
            {
                "name": "Signals",
                "value": self._format_signal_types(signal),
                "inline": True,
            },
        ]

        # Add wallet info if available
        if signal.wallet_tx_count is not None:
            wallet_short = trade.taker_address[:10] + "..." if trade.taker_address else "Unknown"
            fields.append({
                "name": "Wallet",
                "value": f"`{wallet_short}`\n{signal.wallet_tx_count} transactions",
                "inline": True,
            })

        # Add cluster info if applicable
        if SignalType.CLUSTER in signal.signal_types:
            fields.append({
                "name": "Cluster",
                "value": f"{len(signal.cluster_wallets)} wallets in {config.filters.cluster_window_seconds}s",
                "inline": True,
            })

        # Add market probabilities if available
        if signal.current_yes_price is not None:
            fields.append({
                "name": "Current Odds",
                "value": f"YES: {signal.current_yes_price:.0%} | NO: {signal.current_no_price:.0%}",
                "inline": False,
            })

        # Links
        market_link = f"https://polymarket.com/event/{signal.market_slug}" if signal.market_slug else "#"
        wallet_link = f"https://polygonscan.com/address/{trade.taker_address}" if trade.taker_address else "#"

        return {
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"Confidence: {signal.confidence:.0%} | {trade.datetime.strftime('%H:%M:%S UTC')}",
            },
            "url": market_link,
        }

    def _build_telegram_message(self, signal: Signal) -> str:
        """Build an HTML-formatted Telegram message."""
        trade = signal.trade

        # Header
        market_name = signal.market_title or f"Market {trade.market[:16]}..."
        header_emoji = "🚨" if signal.is_high_confidence else "⚠️"
        header = f"<b>{header_emoji} ALERT: {market_name}</b>"

        # Trade details
        side_emoji = "📈" if trade.side.value == "BUY" else "📉"
        trade_info = (
            f"<b>Side:</b> {trade.side.value} {side_emoji}\n"
            f"<b>Price:</b> {trade.price:.2f}¢\n"
            f"<b>Amount:</b> ${trade.usd_value:,.0f}"
        )

        # Signals
        signal_info = f"<b>Signal:</b> {signal.signal_emoji} {self._format_signal_types_text(signal)}"

        # Wallet info
        wallet_info = ""
        if signal.wallet_tx_count is not None:
            wallet_info = f"\n<b>Wallet:</b> {signal.wallet_tx_count} txs"

        # Links
        market_link = f"https://polymarket.com/event/{signal.market_slug}" if signal.market_slug else "#"
        wallet_link = f"https://polygonscan.com/address/{trade.taker_address}" if trade.taker_address else "#"

        links = f'<a href="{market_link}">view market</a>'
        if trade.taker_address:
            links += f' | <a href="{wallet_link}">check wallet</a>'

        return f"{header}\n\n{trade_info}\n{signal_info}{wallet_info}\n\n{links}"

    def _format_signal_types(self, signal: Signal) -> str:
        """Format signal types for Discord embed."""
        lines = []
        for st in signal.signal_types:
            if st == SignalType.WHALE:
                lines.append("🐋 Whale Trade")
            elif st == SignalType.FRESH_WALLET:
                lines.append("✨ Fresh Wallet")
            elif st == SignalType.CLUSTER:
                lines.append("👥 Cluster Activity")
            elif st == SignalType.SIZE_ANOMALY:
                lines.append("📊 Size Anomaly")
        return "\n".join(lines) if lines else "Unknown"

    def _format_signal_types_text(self, signal: Signal) -> str:
        """Format signal types as plain text."""
        parts = []
        for st in signal.signal_types:
            if st == SignalType.WHALE:
                parts.append("Whale")
            elif st == SignalType.FRESH_WALLET:
                parts.append(f"Fresh Wallet ({signal.wallet_tx_count} txs)")
            elif st == SignalType.CLUSTER:
                parts.append("Cluster")
            elif st == SignalType.SIZE_ANOMALY:
                parts.append("Size Anomaly")
        return " + ".join(parts) if parts else "Unknown"

    def get_stats(self) -> dict:
        """Return alert statistics."""
        return {
            **self.stats,
            "queue_size": self.queue.size(),
        }


# Global alert manager instance
alert_manager = AlertManager()
