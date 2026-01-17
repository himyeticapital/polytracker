"""Test script to send a watched wallet alert."""

import asyncio
import time
from datetime import datetime

from src.config import config
from src.models import Trade, Signal, SignalType, TradeSide
from src.alerts import alert_manager


async def send_watched_wallet_alert():
    """Send a test alert specifically for a watched wallet signal."""

    # Use a fake watched wallet address for testing
    watched_wallet = "0xSuspectedInsider1234567890abcdef12345678"

    # Create a sample trade from the watched wallet
    trade = Trade(
        asset_id="98765432109876543210",
        market="0xdeadbeef12345678",
        price=0.72,
        size=25000,
        side=TradeSide.BUY,
        timestamp=int(time.time() * 1000),  # Current timestamp
        taker_address=watched_wallet,
    )

    # Create a signal with WATCHED_WALLET as the primary signal
    # Watched wallets often have additional signals too
    signal = Signal(
        trade=trade,
        signal_types=[
            SignalType.WATCHED_WALLET,  # Primary signal - this is a tracked wallet
            SignalType.WHALE,           # Large trade
            SignalType.TIMING,          # Close to market resolution
        ],
        confidence=0.92,  # High confidence due to watched wallet bonus
        market_title="Will Bitcoin reach $150,000 by March 2026?",
        market_slug="bitcoin-150k-march-2026",
        current_yes_price=0.72,
        current_no_price=0.28,
        wallet_tx_count=47,  # Established wallet
        hours_to_close=6.5,  # Very close to resolution
        price_before_trade=0.68,
        price_after_trade=0.72,  # Price moved 4 cents
        wallet_profit_loss=87500.0,  # Profitable history
        wallet_win_rate=0.81,  # 81% win rate - suspiciously good
        wallet_total_trades=62,
        wallet_volume=425000.0,
    )

    print(f"Sending WATCHED WALLET test alert...")
    print(f"  Wallet: {watched_wallet[:10]}...{watched_wallet[-6:]}")
    print(f"  Market: {signal.market_title}")
    print(f"  Trade: {trade.size:,} shares @ {trade.price:.0%} = ${trade.size * trade.price:,.0f}")
    print(f"  Signals: {[s.value for s in signal.signal_types]}")
    print(f"  Confidence: {signal.confidence:.0%}")
    print()

    # Start alert manager and send
    await alert_manager.start()
    await alert_manager.send_alert(signal)

    # Give it time to process
    await asyncio.sleep(3)
    await alert_manager.stop()

    print("Watched wallet alert sent!")


if __name__ == "__main__":
    asyncio.run(send_watched_wallet_alert())
