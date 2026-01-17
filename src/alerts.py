"""
Alert notification system for PolyTracker.

Handles dual-channel notifications to Discord and Telegram with:
- Rate limiting (LeakyBucket queue)
- Rich formatting (Discord embeds, Telegram HTML)
- Automatic retry on failures
- Wallet nickname generation
- Market thumbnail images
"""

import asyncio
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, List

import aiohttp
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .config import config, AlertConfig
from .models import Signal, SignalType

logger = logging.getLogger(__name__)

# Word lists for generating wallet nicknames (like "Utter-Ease")
ADJECTIVES = [
    "Swift", "Calm", "Bold", "Keen", "Wise", "Fair", "Pure", "Deep",
    "Warm", "Cool", "Soft", "Firm", "Quick", "Slow", "High", "Low",
    "Bright", "Dark", "Light", "Heavy", "Sharp", "Smooth", "Rough", "Fine",
    "Grand", "Small", "Great", "Tiny", "Vast", "Wide", "Narrow", "Thick",
    "Utter", "Prime", "Noble", "Royal", "Brave", "Quiet", "Loud", "Silent",
    "Rapid", "Steady", "Lucky", "Happy", "Merry", "Jolly", "Witty", "Clever",
]

NOUNS = [
    "Ease", "Grace", "Power", "Force", "Light", "Dawn", "Dusk", "Star",
    "Moon", "Sun", "Sky", "Sea", "Wave", "Wind", "Fire", "Frost",
    "Stone", "Peak", "Vale", "Glen", "Brook", "Lake", "River", "Ocean",
    "Eagle", "Hawk", "Wolf", "Bear", "Lion", "Tiger", "Falcon", "Raven",
    "Sage", "Knight", "Baron", "Duke", "Count", "Lord", "King", "Queen",
    "Spark", "Flame", "Storm", "Cloud", "Rain", "Snow", "Thunder", "Flash",
]


def generate_wallet_nickname(address: str) -> str:
    """
    Generate a memorable nickname from a wallet address.

    Uses a hash of the address to deterministically pick words,
    so the same address always gets the same nickname.
    """
    if not address:
        return "Unknown"

    # Use hash to get consistent indices
    addr_hash = hashlib.md5(address.lower().encode()).hexdigest()
    adj_idx = int(addr_hash[:8], 16) % len(ADJECTIVES)
    noun_idx = int(addr_hash[8:16], 16) % len(NOUNS)

    # Get short address suffix for disambiguation
    short_addr = address[-4:].upper() if len(address) >= 4 else address.upper()

    return f"{ADJECTIVES[adj_idx]}-{NOUNS[noun_idx]} ({short_addr})"


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
        Only sends alerts when confidence > 60%.
        """
        if signal.confidence <= 0.60:
            logger.debug(f"Skipping alert - confidence {signal.confidence:.0%} <= 60%")
            return

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

        # Send to all channels concurrently
        tasks = []

        # Collect all Discord webhook URLs
        discord_urls = []
        if self.config.discord_webhook_url:
            discord_urls.append(self.config.discord_webhook_url)
        if self.config.discord_webhook_urls:
            discord_urls.extend(self.config.discord_webhook_urls)

        # Remove duplicates while preserving order
        discord_urls = list(dict.fromkeys(discord_urls))

        # Send to all Discord webhooks
        for webhook_url in discord_urls:
            tasks.append(self._send_discord(signal, webhook_url))

        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            tasks.append(self._send_telegram(signal))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Alert send error: {result}")
                    self.stats["errors"] += 1

            self.stats["alerts_sent"] += 1

    async def _send_discord(self, signal: Signal, webhook_url: str = None):
        """Send a rich Discord webhook embed."""
        url = webhook_url or self.config.discord_webhook_url
        embed = self._build_discord_embed(signal)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
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
        """Build a Discord embed object for a signal (styled like Polymarket Watch)."""
        trade = signal.trade

        # Color based on signal type
        if SignalType.WATCHED_WALLET in signal.signal_types:
            color = 0x9B59B6  # Purple for watched wallet
        elif SignalType.FRESH_WALLET in signal.signal_types:
            color = 0x2ECC71  # Green for fresh wallet
        elif signal.is_high_confidence:
            color = 0xFF0000  # Red for high confidence
        else:
            color = 0xFFA500  # Orange for medium

        # Title with signal type indicator and confidence
        signal_label = self._get_primary_signal_label(signal)
        confidence_emoji = self._get_confidence_emoji(signal.confidence)
        market_name = signal.market_title or f"Market {trade.market[:16]}..."

        # Determine outcome based on trade side
        outcome = "Yes" if trade.side.value == "BUY" else "No"

        # Generate wallet nickname
        wallet_nickname = generate_wallet_nickname(trade.taker_address) if trade.taker_address else "Unknown"

        # Build fields in column layout like friend's bot
        fields = [
            {
                "name": "Trader",
                "value": f"[{wallet_nickname}](https://polygonscan.com/address/{trade.taker_address})",
                "inline": True,
            },
            {
                "name": "Side",
                "value": trade.side.value,
                "inline": True,
            },
            {
                "name": "Trade",
                "value": f"{trade.size:,.0f} shares @ {trade.price * 100:.1f}¢",
                "inline": True,
            },
            {
                "name": "Notional",
                "value": f"${trade.usd_value:,.0f}",
                "inline": True,
            },
            {
                "name": "Unique markets\n(lifetime est.)",
                "value": f"{signal.wallet_total_trades or 'n/a'}",
                "inline": True,
            },
            {
                "name": "Win Rate (resolved)",
                "value": f"{signal.wallet_win_rate:.0%}" if signal.wallet_win_rate else "n/a",
                "inline": True,
            },
        ]

        # Always show Signals Detected with confidence
        fields.append({
            "name": "Signals Detected",
            "value": self._format_signal_types(signal),
            "inline": True,
        })

        # Show Confidence Score prominently
        fields.append({
            "name": "Confidence Score",
            "value": f"{confidence_emoji} **{signal.confidence:.0%}**",
            "inline": True,
        })

        # Add cluster info if applicable
        if SignalType.CLUSTER in signal.signal_types:
            fields.append({
                "name": "Cluster Activity",
                "value": f"👥 {len(signal.cluster_wallets)} wallets in {config.filters.cluster_window_seconds}s",
                "inline": True,
            })

        # Always show Market Close (even if n/a)
        if signal.hours_to_close is not None and signal.hours_to_close > 0:
            market_close_value = f"⏰ {signal.hours_to_close:.1f}h remaining"
        else:
            market_close_value = "n/a"
        fields.append({
            "name": "Market Close",
            "value": market_close_value,
            "inline": True,
        })

        # Always show Price Movement (even if n/a)
        if signal.price_before_trade is not None and signal.price_after_trade is not None:
            price_change = signal.price_after_trade - signal.price_before_trade
            direction = "📈" if price_change > 0 else "📉"
            price_movement_value = f"{direction} {signal.price_before_trade * 100:.1f}¢ → {signal.price_after_trade * 100:.1f}¢ ({price_change * 100:+.1f}¢)"
        else:
            price_movement_value = "n/a"
        fields.append({
            "name": "Price Movement",
            "value": price_movement_value,
            "inline": True,
        })

        # Add market probabilities if available
        if signal.current_yes_price is not None:
            fields.append({
                "name": "Current Odds",
                "value": f"YES: {signal.current_yes_price:.0%} | NO: {signal.current_no_price:.0%}",
                "inline": True,
            })

        # Always show Wallet Profile section
        pnl_value = f"${signal.wallet_profit_loss:+,.0f}" if signal.wallet_profit_loss is not None else "n/a"
        pnl_emoji = "💰" if signal.wallet_profit_loss and signal.wallet_profit_loss > 0 else "📉" if signal.wallet_profit_loss and signal.wallet_profit_loss < 0 else ""
        fields.append({
            "name": "Wallet P/L",
            "value": f"{pnl_emoji} {pnl_value}".strip(),
            "inline": True,
        })

        # Always show Wallet transaction count
        tx_count = signal.wallet_tx_count if signal.wallet_tx_count is not None else "n/a"
        tx_label = f"{tx_count} transactions" if isinstance(tx_count, int) else tx_count
        fields.append({
            "name": "Wallet",
            "value": f"🔗 {tx_label}",
            "inline": True,
        })

        # Market link and thumbnail
        market_link = f"https://polymarket.com/event/{signal.market_slug}" if signal.market_slug else None

        # Build the embed
        embed = {
            "title": f"{signal_label} [{signal.confidence:.0%}]",
            "description": f"**{market_name}**\nOutcome: **{outcome}**",
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"PolyTracker • {trade.datetime.strftime('%m/%d/%Y, %I:%M:%S %p')}",
            },
        }

        if market_link:
            embed["url"] = market_link

        # Add market thumbnail if we have a slug (Polymarket image URL pattern)
        if signal.market_slug:
            embed["thumbnail"] = {
                "url": f"https://polymarket-upload.s3.us-east-2.amazonaws.com/{signal.market_slug}.png"
            }

        return embed

    def _get_primary_signal_label(self, signal: Signal) -> str:
        """Get the primary signal label for the embed title."""
        # Prioritize certain signals for the title (watched wallet first!)
        if SignalType.WATCHED_WALLET in signal.signal_types:
            return "👁️ Watched Wallet"
        if SignalType.FRESH_WALLET in signal.signal_types:
            return "✨ Low Activity Wallet"
        if SignalType.WHALE in signal.signal_types:
            return "🐋 Whale Alert"
        if SignalType.CLUSTER in signal.signal_types:
            return "👥 Cluster Activity"
        if SignalType.CONTRARIAN in signal.signal_types:
            return "🔀 Contrarian Bet"
        if SignalType.TIMING in signal.signal_types:
            return "⏰ Near Market Close"
        if SignalType.ODDS_MOVEMENT in signal.signal_types:
            return "📈 Odds Movement"
        if SignalType.SIZE_ANOMALY in signal.signal_types:
            return "📊 Size Anomaly"
        return "⚠️ Alert"

    def _get_confidence_emoji(self, confidence: float) -> str:
        """Get emoji indicator based on confidence level."""
        if confidence >= 0.85:
            return "🔴"  # Very high - red hot
        elif confidence >= 0.75:
            return "🟠"  # High - orange
        elif confidence >= 0.65:
            return "🟡"  # Medium-high - yellow
        elif confidence >= 0.55:
            return "🟢"  # Medium - green
        else:
            return "⚪"  # Low - white

    def _build_telegram_message(self, signal: Signal) -> str:
        """Build an HTML-formatted Telegram message (styled like Polymarket Watch)."""
        trade = signal.trade

        # Signal label header with confidence
        signal_label = self._get_primary_signal_label(signal)
        confidence_emoji = self._get_confidence_emoji(signal.confidence)
        market_name = signal.market_title or f"Market {trade.market[:16]}..."
        outcome = "Yes" if trade.side.value == "BUY" else "No"

        # Generate wallet nickname
        wallet_nickname = generate_wallet_nickname(trade.taker_address) if trade.taker_address else "Unknown"

        header = f"<b>{signal_label}</b> [{signal.confidence:.0%}]\n\n<b>{market_name}</b>\nOutcome: <b>{outcome}</b>"

        # Trade details in column format
        trade_info = (
            f"\n\n<b>Trader</b>          <b>Side</b>          <b>Trade</b>\n"
            f"<a href='https://polygonscan.com/address/{trade.taker_address}'>{wallet_nickname}</a>    "
            f"{trade.side.value}    "
            f"{trade.size:,.0f} shares @ {trade.price * 100:.1f}¢\n\n"
            f"<b>Notional</b>          <b>Unique markets</b>          <b>Win Rate</b>\n"
            f"${trade.usd_value:,.0f}          "
            f"{signal.wallet_total_trades or 'n/a'}          "
            f"{f'{signal.wallet_win_rate:.0%}' if signal.wallet_win_rate else 'n/a'}"
        )

        # Always show signals with confidence
        signals_section = f"\n\n<b>Signals:</b> {self._format_signal_types_text(signal)}\n{confidence_emoji} <b>Confidence:</b> {signal.confidence:.0%}"

        # Always show extra info (even if n/a)
        extra_info = ""

        # Market close
        if signal.hours_to_close is not None and signal.hours_to_close > 0:
            extra_info += f"\n⏰ Market Close: {signal.hours_to_close:.1f}h remaining"
        else:
            extra_info += "\n⏰ Market Close: n/a"

        # Price movement
        if signal.price_before_trade is not None and signal.price_after_trade is not None:
            price_change = signal.price_after_trade - signal.price_before_trade
            direction = "📈" if price_change > 0 else "📉"
            extra_info += f"\n{direction} Price Movement: {signal.price_before_trade * 100:.1f}¢ → {signal.price_after_trade * 100:.1f}¢"
        else:
            extra_info += "\n📊 Price Movement: n/a"

        # Wallet P/L
        if signal.wallet_profit_loss is not None:
            pnl_emoji = "💰" if signal.wallet_profit_loss > 0 else "📉"
            extra_info += f"\n{pnl_emoji} Wallet P/L: ${signal.wallet_profit_loss:+,.0f}"
        else:
            extra_info += "\n💵 Wallet P/L: n/a"

        # Wallet transaction count
        tx_count = signal.wallet_tx_count if signal.wallet_tx_count is not None else "n/a"
        tx_label = f"{tx_count} transactions" if isinstance(tx_count, int) else tx_count
        extra_info += f"\n🔗 Wallet: {tx_label}"

        # Links
        market_link = f"https://polymarket.com/event/{signal.market_slug}" if signal.market_slug else "#"

        links = f'\n\n<a href="{market_link}">View Market</a>'

        return f"{header}{trade_info}{signals_section}{extra_info}{links}"

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
            elif st == SignalType.TIMING:
                lines.append("⏰ Near Market Close")
            elif st == SignalType.ODDS_MOVEMENT:
                lines.append("📈 Odds Movement")
            elif st == SignalType.CONTRARIAN:
                lines.append("🔀 Contrarian Bet")
            elif st == SignalType.WATCHED_WALLET:
                lines.append("👁️ Watched Wallet")
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
            elif st == SignalType.TIMING:
                parts.append(f"Near Close ({signal.hours_to_close:.0f}h)")
            elif st == SignalType.ODDS_MOVEMENT:
                parts.append("Odds Movement")
            elif st == SignalType.CONTRARIAN:
                parts.append("Contrarian")
            elif st == SignalType.WATCHED_WALLET:
                parts.append("Watched Wallet")
        return " + ".join(parts) if parts else "Unknown"

    def get_stats(self) -> dict:
        """Return alert statistics."""
        return {
            **self.stats,
            "queue_size": self.queue.size(),
        }


# Global alert manager instance
alert_manager = AlertManager()
