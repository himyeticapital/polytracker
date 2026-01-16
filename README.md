# PolyTracker

**Polymarket Insider Activity Surveillance Bot**

Real-time monitoring of Polymarket's CLOB (Central Limit Order Book) for institutional/insider order flow. Detects whale trades, fresh wallet activity, and coordinated buying patterns, then broadcasts alerts to Discord and Telegram.

## Features

- **Real-time WebSocket Connection** - Direct feed from Polymarket CLOB
- **Intelligent Filtering** - Removes noise (small trades, LP activity)
- **Multi-Signal Detection** - Whale trades, fresh wallets, cluster activity
- **Dual-Channel Alerts** - Discord embeds + Telegram messages
- **Auto-Reconnect** - Resilient connection with exponential backoff
- **Rate Limiting** - Prevents API bans with LeakyBucket queue

## Signal Detection

### Signal A: Whale Trade
Triggers when a trade exceeds size thresholds:
- **Absolute:** Trade value > $10,000
- **Relative:** Trade value > 5x the average of last 100 trades

### Signal B: Fresh Wallet
Detects potential insider/burner accounts:
- Wallet has < 10 transactions on Polygon
- High-value trade from a nearly empty wallet suggests insider activity

### Signal C: Cluster Activity
Identifies coordinated buying:
- 3+ distinct wallets buy the same outcome
- Within a 60-second window
- Suggests organized accumulation

### Signal D: Timing (Near Market Close)
Detects trades placed close to market resolution:
- Trade placed within 24 hours of market close
- Last-minute informed trading indicator
- Configurable threshold via `TIMING_HOURS_THRESHOLD`

### Signal E: Odds Movement
Detects trades that significantly move the market:
- Price moves by 5+ cents after the trade
- Indicates market-moving size or impact
- Configurable threshold via `ODDS_MOVEMENT_THRESHOLD`

### Signal F: Contrarian
Detects large bets against market consensus:
- When market is 70%+ in one direction
- Large trade ($5k+) betting the opposite way
- Often indicates confident insider information

## Filtering Pipeline

Trades pass through sequential filters before signal detection:

```
Trade Received
     |
     v
[Market Filter] -- Excluded keywords? --> Discard
     |
     v
[Size Filter] -- < $2,000? --> Discard
     |
     v
[LP Detection] -- Balanced YES/NO positions? --> Discard
     |
     v
Signal Detection
```

### Default Exclusions
- Trades below $2,000 USD value
- LP/arbitrage activity (same wallet buying both sides within 200ms)

**Note:** All markets are tracked by default, including sports. To exclude specific categories, set `EXCLUDE_MARKET_KEYWORDS` in your `.env` file (e.g., `EXCLUDE_MARKET_KEYWORDS=["crypto", "price"]`).

## Assets Tracked

The bot automatically fetches and monitors the **top 100 markets by 24h volume** from Polymarket. This includes:
- Political prediction markets
- Sports betting (NBA, NFL, Soccer, etc.)
- Crypto price predictions
- Economic indicators
- Current events
- Entertainment/media outcomes

Markets are refreshed on each bot restart to ensure coverage of the most active markets.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Polymarket CLOB WebSocket                │
└─────────────────────────────────────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│                      Trade Ingestion                        │
│              (WebSocket with auto-reconnect)                │
└─────────────────────────────────────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│                    Filtering Pipeline                       │
│         Market Filter → Size Filter → LP Detection          │
└─────────────────────────────────────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│                    Signal Detection                         │
│   Whale │ Fresh Wallet │ Cluster │ Timing │ Odds │ Contrarian│
└─────────────────────────────────────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│                      Enrichment                             │
│    Market title, current odds, end date, wallet P&L         │
└─────────────────────────────────────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│                    Alert Dispatch                           │
│              Discord Webhook + Telegram Bot                 │
│                 (Rate limited: 1/sec)                       │
└─────────────────────────────────────────────────────────────┘
```

## Installation

### Prerequisites
- Python 3.10+
- Polygon RPC endpoint (Alchemy/Infura recommended)
- Discord webhook URL
- Telegram bot token + chat ID

### Setup

```bash
# Clone the repository
git clone https://github.com/himyeticapital/polytracker.git
cd polytracker

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials
```

### Configuration

Create a `.env` file with:

```ini
# Blockchain (Polygon RPC for wallet checks)
RPC_URL="https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY"

# Discord Webhook
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

# Telegram Bot
TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
TELEGRAM_CHAT_ID="-100..."

# Filter Thresholds (optional - defaults shown)
MIN_USD_SIZE=2000
WHALE_THRESHOLD_USD=10000
WHALE_MULTIPLIER=5.0
FRESH_WALLET_MAX_TXS=10
CLUSTER_WINDOW_SECONDS=60
CLUSTER_MIN_WALLETS=3
# EXCLUDE_MARKET_KEYWORDS=[]  # Empty = track all markets (default)
```

## Usage

### Run in foreground
```bash
python3 -m src.main
```

### Run in background (persistent)
```bash
nohup python3 -m src.main > polytracker.log 2>&1 &
```

### Monitor logs
```bash
tail -f polytracker.log
```

### Stop the bot
```bash
# Find PID
ps aux | grep "src.main"

# Kill process
kill <PID>
```

## Alert Format

### Discord
Rich embeds with color-coded confidence:
- **Red** = High confidence (multiple signals or >$25k)
- **Orange** = Medium confidence

Includes: Market name, trade details, signal types, wallet info, current odds, and links.

### Telegram
HTML-formatted messages with:
- Trade side and price
- USD amount
- Signal indicators
- Clickable links to market and wallet

## Project Structure

```
polytracker/
├── src/
│   ├── __init__.py
│   ├── main.py           # Entry point & orchestration
│   ├── config.py         # Environment configuration
│   ├── models.py         # Data classes (Trade, Signal)
│   ├── filters.py        # Trade filtering pipeline
│   ├── signals.py        # Signal detection logic
│   ├── alerts.py         # Discord/Telegram notifications
│   ├── enrichment.py     # Market metadata fetching
│   └── websocket_client.py  # Polymarket WebSocket client
├── .env.example
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_USD_SIZE` | 2000 | Minimum trade value to consider |
| `WHALE_THRESHOLD_USD` | 10000 | Absolute whale threshold |
| `WHALE_MULTIPLIER` | 5.0 | Relative whale threshold (vs avg) |
| `FRESH_WALLET_MAX_TXS` | 10 | Max txs to be "fresh" |
| `CLUSTER_WINDOW_SECONDS` | 60 | Time window for cluster detection |
| `CLUSTER_MIN_WALLETS` | 3 | Min wallets for cluster signal |
| `LP_DETECTION_WINDOW_MS` | 200 | Window for LP detection |
| `TIMING_HOURS_THRESHOLD` | 24.0 | Hours before close to trigger timing signal |
| `ODDS_MOVEMENT_THRESHOLD` | 0.05 | Min price change (5 cents) for odds movement |
| `CONTRARIAN_CONSENSUS_THRESHOLD` | 0.70 | Min consensus level (70%) for contrarian |
| `CONTRARIAN_MIN_SIZE_USD` | 5000 | Min trade size for contrarian signal |

## API References

- [Polymarket CLOB WebSocket](https://docs.polymarket.com/developers/CLOB/websocket/wss-overview)
- [Polymarket Data API](https://docs.polymarket.com/)
- [Polygon RPC](https://polygon.technology/)

## License

MIT

## Disclaimer

This bot is for informational purposes only. Trading on prediction markets involves risk. The signals detected do not constitute financial advice. Always do your own research before trading.
