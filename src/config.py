"""
Configuration management for PolyTracker.
Loads environment variables and provides type-safe access to settings.
"""

import os
import re
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()


@dataclass
class PolymarketConfig:
    """Polymarket API configuration."""
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    api_key: str = ""
    api_pass: str = ""
    api_secret: str = ""


@dataclass
class BlockchainConfig:
    """Polygon RPC configuration."""
    rpc_url: str = "https://polygon-rpc.com"


@dataclass
class AlertConfig:
    """Discord and Telegram notification configuration."""
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


@dataclass
class FilterConfig:
    """Trade filtering configuration."""
    min_usd_size: float = 2000.0
    whale_threshold_usd: float = 10000.0
    whale_multiplier: float = 5.0
    fresh_wallet_max_txs: int = 10
    cluster_window_seconds: int = 60
    cluster_min_wallets: int = 3
    lp_detection_window_ms: int = 200
    exclude_market_keywords: List[str] = field(default_factory=list)


@dataclass
class Config:
    """Main configuration container."""
    polymarket: PolymarketConfig
    blockchain: BlockchainConfig
    alerts: AlertConfig
    filters: FilterConfig


def parse_list_env(value: str) -> List[str]:
    """Parse a comma-separated or JSON-style list from environment variable."""
    if not value:
        return []
    # Handle JSON-style lists: ["Sports", "Football"]
    if value.startswith("["):
        value = value.strip("[]")
        items = re.findall(r'"([^"]*)"', value)
        if items:
            return items
    # Handle comma-separated lists
    return [item.strip().strip('"\'') for item in value.split(",") if item.strip()]


def load_config() -> Config:
    """Load configuration from environment variables."""
    exclude_keywords = parse_list_env(
        os.getenv("EXCLUDE_MARKET_KEYWORDS", '["Sports", "Football", "NBA", "NFL"]')
    )

    return Config(
        polymarket=PolymarketConfig(
            ws_url=os.getenv("POLY_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            api_key=os.getenv("POLY_API_KEY", ""),
            api_pass=os.getenv("POLY_PASS", ""),
            api_secret=os.getenv("POLY_SECRET", ""),
        ),
        blockchain=BlockchainConfig(
            rpc_url=os.getenv("RPC_URL", "https://polygon-rpc.com"),
        ),
        alerts=AlertConfig(
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        ),
        filters=FilterConfig(
            min_usd_size=float(os.getenv("MIN_USD_SIZE", "2000")),
            whale_threshold_usd=float(os.getenv("WHALE_THRESHOLD_USD", "10000")),
            whale_multiplier=float(os.getenv("WHALE_MULTIPLIER", "5.0")),
            fresh_wallet_max_txs=int(os.getenv("FRESH_WALLET_MAX_TXS", "10")),
            cluster_window_seconds=int(os.getenv("CLUSTER_WINDOW_SECONDS", "60")),
            cluster_min_wallets=int(os.getenv("CLUSTER_MIN_WALLETS", "3")),
            lp_detection_window_ms=int(os.getenv("LP_DETECTION_WINDOW_MS", "200")),
            exclude_market_keywords=exclude_keywords,
        ),
    )


# Global config instance
config = load_config()
