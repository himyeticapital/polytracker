"""
WebSocket client for Polymarket CLOB.

Handles:
- Connection management with auto-reconnect
- Heartbeat/ping to keep connection alive
- Subscription to market channels
- Message parsing and routing
"""

import asyncio
import json
import logging
from typing import Callable, List, Optional, Set

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import config
from .models import Trade

logger = logging.getLogger(__name__)


class PolymarketWebSocket:
    """
    Async WebSocket client for Polymarket CLOB market data.

    Subscribes to trade events and routes them to a callback handler.
    Implements auto-reconnect and heartbeat keepalive.
    """

    # WebSocket URLs
    MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Heartbeat interval (Polymarket requires ping every 10 seconds)
    PING_INTERVAL = 10

    # Reconnect settings
    RECONNECT_DELAY_INITIAL = 1
    RECONNECT_DELAY_MAX = 60
    RECONNECT_DELAY_MULTIPLIER = 2

    def __init__(
        self,
        on_trade: Callable[[Trade, dict], asyncio.coroutine],
        asset_ids: Optional[List[str]] = None,
    ):
        """
        Initialize the WebSocket client.

        Args:
            on_trade: Async callback function called for each trade.
                      Signature: async def on_trade(trade: Trade, raw_data: dict)
            asset_ids: List of token IDs to subscribe to.
                       If None, must call subscribe() after connecting.
        """
        self.on_trade = on_trade
        self.subscribed_assets: Set[str] = set(asset_ids or [])

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_delay = self.RECONNECT_DELAY_INITIAL

        # Tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

        # Stats
        self.stats = {
            "messages_received": 0,
            "trades_received": 0,
            "reconnects": 0,
            "errors": 0,
        }

    async def connect(self):
        """Establish WebSocket connection and start processing."""
        self._running = True
        await self._connect_loop()

    async def disconnect(self):
        """Gracefully disconnect from WebSocket."""
        self._running = False

        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("WebSocket disconnected")

    async def subscribe(self, asset_ids: List[str]):
        """
        Subscribe to additional asset IDs.

        Can be called before or after connection is established.
        """
        new_assets = set(asset_ids) - self.subscribed_assets
        if not new_assets:
            return

        self.subscribed_assets.update(new_assets)

        if self._ws and self._ws.open:
            await self._send_subscription(list(new_assets), operation="subscribe")

    async def unsubscribe(self, asset_ids: List[str]):
        """Unsubscribe from asset IDs."""
        to_remove = set(asset_ids) & self.subscribed_assets
        if not to_remove:
            return

        self.subscribed_assets -= to_remove

        if self._ws and self._ws.open:
            await self._send_subscription(list(to_remove), operation="unsubscribe")

    async def _connect_loop(self):
        """Main connection loop with auto-reconnect."""
        while self._running:
            try:
                await self._establish_connection()
                self._reconnect_delay = self.RECONNECT_DELAY_INITIAL  # Reset on success

                # Run message receiver
                await self._receive_messages()

            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
            except WebSocketException as e:
                logger.error(f"WebSocket error: {e}")
                self.stats["errors"] += 1
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                self.stats["errors"] += 1

            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)

                # Exponential backoff
                self._reconnect_delay = min(
                    self._reconnect_delay * self.RECONNECT_DELAY_MULTIPLIER,
                    self.RECONNECT_DELAY_MAX,
                )
                self.stats["reconnects"] += 1

    async def _establish_connection(self):
        """Connect to WebSocket and send initial subscription."""
        logger.info(f"Connecting to {self.MARKET_WS_URL}...")

        self._ws = await websockets.connect(
            self.MARKET_WS_URL,
            ping_interval=None,  # We handle pings manually
            ping_timeout=None,
        )

        logger.info("WebSocket connected")

        # Send initial subscription if we have assets
        if self.subscribed_assets:
            await self._send_initial_subscription()

        # Start ping task
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def _send_initial_subscription(self):
        """Send initial subscription message."""
        message = {
            "assets_ids": list(self.subscribed_assets),
            "type": "market",
        }
        await self._ws.send(json.dumps(message))
        logger.info(f"Subscribed to {len(self.subscribed_assets)} assets")

    async def _send_subscription(self, asset_ids: List[str], operation: str):
        """Send subscribe/unsubscribe message."""
        message = {
            "assets_ids": asset_ids,
            "operation": operation,
        }
        await self._ws.send(json.dumps(message))
        logger.debug(f"{operation.capitalize()}d {len(asset_ids)} assets")

    async def _ping_loop(self):
        """Send periodic pings to keep connection alive."""
        while self._running and self._ws and self._ws.open:
            try:
                await asyncio.sleep(self.PING_INTERVAL)
                await self._ws.send("PING")
            except Exception as e:
                logger.debug(f"Ping failed: {e}")
                break

    async def _receive_messages(self):
        """Receive and process messages from WebSocket."""
        async for message in self._ws:
            self.stats["messages_received"] += 1

            # Skip PONG responses
            if message == "PONG":
                continue

            try:
                data = json.loads(message)
                await self._handle_message(data)
            except json.JSONDecodeError:
                logger.debug(f"Non-JSON message: {message[:100]}")
            except Exception as e:
                logger.error(f"Error handling message: {e}")
                self.stats["errors"] += 1

    async def _handle_message(self, data: dict):
        """Route message to appropriate handler based on event_type."""
        event_type = data.get("event_type")

        if event_type == "last_trade_price":
            await self._handle_trade(data)
        elif event_type == "book":
            pass  # Order book update - not used in MVP
        elif event_type == "price_change":
            pass  # Price change - not used in MVP
        elif event_type == "tick_size_change":
            pass  # Tick size change - not used in MVP
        else:
            logger.debug(f"Unknown event type: {event_type}")

    async def _handle_trade(self, data: dict):
        """Process a trade message."""
        self.stats["trades_received"] += 1

        try:
            trade = Trade.from_ws_message(data)
            await self.on_trade(trade, data)
        except Exception as e:
            logger.error(f"Error processing trade: {e}")
            self.stats["errors"] += 1

    def get_stats(self) -> dict:
        """Return connection statistics."""
        return {
            **self.stats,
            "subscribed_assets": len(self.subscribed_assets),
            "connected": self._ws is not None and self._ws.open,
        }
