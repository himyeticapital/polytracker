"""Quick test script to send a test alert with all fields."""

import asyncio
from datetime import datetime

from src.config import config
from src.models import Trade, Signal, SignalType, TradeSide
from src.alerts import alert_manager


async def send_test_alert():
    """Send a test alert with all fields populated."""
    # Create a sample trade
    trade = Trade(
        asset_id="12345678901234567890",
        market="0x1234567890abcdef",
        price=0.65,
        size=15000,
        side=TradeSide.BUY,
        timestamp=1705420800000,
        taker_address="0x941234567890abcdef1234567890abcdef123456",
    )

    # Create a signal with all fields
    signal = Signal(
        trade=trade,
        signal_types=[SignalType.FRESH_WALLET, SignalType.WHALE, SignalType.TIMING],
        confidence=0.85,
        market_title="Will Trump win the 2024 election?",
        market_slug="trump-2024-election",
        current_yes_price=0.65,
        current_no_price=0.35,
        wallet_tx_count=3,  # Fresh wallet with 3 transactions
        hours_to_close=18.5,  # Near market close
        price_before_trade=0.62,
        price_after_trade=0.65,
        wallet_profit_loss=12500.0,
        wallet_win_rate=0.72,
        wallet_total_trades=15,
        wallet_volume=85000.0,
    )

    # Start alert manager and send
    await alert_manager.start()
    await alert_manager.send_alert(signal)

    # Give it time to process
    await asyncio.sleep(3)
    await alert_manager.stop()

    print("Test alert sent!")


if __name__ == "__main__":
    asyncio.run(send_test_alert())
