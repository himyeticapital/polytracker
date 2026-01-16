"""
Data models for PolyTracker.
Defines the trade and signal data structures used throughout the application.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class TradeSide(Enum):
    """Trade side enumeration."""
    BUY = "BUY"
    SELL = "SELL"


class SignalType(Enum):
    """Signal classification types."""
    WHALE = "whale"           # Large trade size
    FRESH_WALLET = "fresh"    # New wallet with few transactions
    CLUSTER = "cluster"       # Multiple wallets trading same direction
    SIZE_ANOMALY = "anomaly"  # Trade significantly above average
    TIMING = "timing"         # Trade near market close or key event
    ODDS_MOVEMENT = "odds"    # Trade that moves the line significantly
    CONTRARIAN = "contrarian" # Large trade against current consensus


@dataclass
class Trade:
    """
    Represents a trade received from the Polymarket WebSocket.

    Attributes:
        asset_id: Token ID of the traded asset
        market: Condition ID (market identifier)
        price: Trade price (0-1 representing cents)
        size: Number of shares traded
        side: BUY or SELL
        timestamp: Unix timestamp in milliseconds
        taker_address: Wallet address of the trade taker (if available)
        maker_address: Wallet address of the trade maker (if available)
    """
    asset_id: str
    market: str
    price: float
    size: float
    side: TradeSide
    timestamp: int
    taker_address: Optional[str] = None
    maker_address: Optional[str] = None

    @property
    def usd_value(self) -> float:
        """Calculate the USD value of the trade."""
        return self.price * self.size

    @property
    def datetime(self) -> datetime:
        """Convert timestamp to datetime object."""
        return datetime.fromtimestamp(self.timestamp / 1000)

    @classmethod
    def from_ws_message(cls, data: dict) -> "Trade":
        """
        Create a Trade instance from a WebSocket message.

        Expected message format:
        {
            "event_type": "last_trade_price",
            "asset_id": "...",
            "market": "0x...",
            "price": "0.52",
            "size": "100",
            "side": "BUY",
            "timestamp": 1234567890000
        }
        """
        return cls(
            asset_id=data.get("asset_id", ""),
            market=data.get("market", ""),
            price=float(data.get("price", 0)),
            size=float(data.get("size", 0)),
            side=TradeSide(data.get("side", "BUY")),
            timestamp=int(data.get("timestamp", 0)),
            taker_address=data.get("taker", data.get("taker_address")),
            maker_address=data.get("maker", data.get("maker_address")),
        )


@dataclass
class Signal:
    """
    Represents a detected trading signal worthy of alerting.

    Attributes:
        trade: The underlying trade that triggered the signal
        signal_types: List of signal classifications that apply
        wallet_tx_count: Transaction count of the wallet (for fresh wallet detection)
        cluster_wallets: List of wallet addresses in a cluster (for cluster detection)
        avg_trade_size: Average trade size for anomaly comparison
        confidence: Signal confidence level (0-1)
    """
    trade: Trade
    signal_types: List[SignalType] = field(default_factory=list)
    wallet_tx_count: Optional[int] = None
    cluster_wallets: List[str] = field(default_factory=list)
    avg_trade_size: Optional[float] = None
    confidence: float = 0.5

    # Enrichment data (populated after detection)
    market_title: Optional[str] = None
    market_slug: Optional[str] = None
    current_yes_price: Optional[float] = None
    current_no_price: Optional[float] = None
    price_before_trade: Optional[float] = None
    price_after_trade: Optional[float] = None
    market_end_date: Optional[datetime] = None
    hours_to_close: Optional[float] = None

    # Wallet profile enrichment
    wallet_profit_loss: Optional[float] = None
    wallet_win_rate: Optional[float] = None
    wallet_total_trades: Optional[int] = None
    wallet_volume: Optional[float] = None

    @property
    def is_high_confidence(self) -> bool:
        """Determine if this is a high confidence signal."""
        # High confidence if multiple signals or whale + fresh wallet
        if len(self.signal_types) >= 2:
            return True
        if SignalType.WHALE in self.signal_types and SignalType.FRESH_WALLET in self.signal_types:
            return True
        if self.trade.usd_value >= 25000:
            return True
        return False

    @property
    def signal_emoji(self) -> str:
        """Get emoji representation of signal types."""
        emojis = []
        if SignalType.WHALE in self.signal_types:
            emojis.append("\U0001F40B")  # whale
        if SignalType.FRESH_WALLET in self.signal_types:
            emojis.append("\u2728")  # sparkles
        if SignalType.CLUSTER in self.signal_types:
            emojis.append("\U0001F465")  # busts in silhouette
        if SignalType.SIZE_ANOMALY in self.signal_types:
            emojis.append("\U0001F4C8")  # chart increasing
        if SignalType.TIMING in self.signal_types:
            emojis.append("\u23F0")  # alarm clock
        if SignalType.ODDS_MOVEMENT in self.signal_types:
            emojis.append("\U0001F4CA")  # bar chart
        if SignalType.CONTRARIAN in self.signal_types:
            emojis.append("\U0001F500")  # twisted arrows
        return " ".join(emojis) if emojis else "\U0001F514"  # bell


@dataclass
class MarketInfo:
    """
    Market metadata for enriching signals.
    """
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_date: Optional[datetime] = None
    volume: Optional[float] = None
    liquidity: Optional[float] = None
