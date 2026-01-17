# PolyTracker Cloud Deployment Guide

Deploy your Polymarket Insider Activity Surveillance Bot to run 24/7 on a cloud VPS.

## Quick Comparison

| Provider | Cost | Pros | Setup Difficulty |
|----------|------|------|------------------|
| **DigitalOcean** | $4-6/mo | Simple, good docs | Easy |
| **Hetzner** | $3-4/mo | Cheapest, EU-based | Easy |
| **AWS Lightsail** | $3.50/mo | AWS ecosystem | Medium |
| **Vultr** | $5/mo | Global locations | Easy |
| **Railway** | Free tier | No server management | Easiest |

---

## Option 1: DigitalOcean Droplet (Recommended)

### Step 1: Create Account & Droplet

1. Sign up at [digitalocean.com](https://digitalocean.com)
2. Create a new Droplet:
   - **Image**: Ubuntu 22.04 LTS
   - **Plan**: Basic $4/mo (512MB RAM) or $6/mo (1GB RAM recommended)
   - **Region**: Choose closest to you (NYC, SFO, LON, etc.)
   - **Authentication**: SSH key (recommended) or password

### Step 2: Connect to Your Server

```bash
ssh root@YOUR_DROPLET_IP
```

### Step 3: Initial Server Setup

```bash
# Update system
apt update && apt upgrade -y

# Install Python and dependencies
apt install -y python3 python3-pip python3-venv git

# Create a non-root user (optional but recommended)
adduser polytracker
usermod -aG sudo polytracker
su - polytracker
```

### Step 4: Clone/Upload Your Bot

**Option A: Git (if you have a repo)**
```bash
git clone https://github.com/YOUR_USERNAME/polymarket-bot.git
cd polymarket-bot
```

**Option B: Upload via SCP (from your local machine)**
```bash
# Run this on your LOCAL machine
scp -r "/Users/milann.eth/Desktop/polymarket bot" root@YOUR_DROPLET_IP:/home/polytracker/
```

### Step 5: Setup Python Environment

```bash
cd "/home/polytracker/polymarket bot"

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Step 6: Configure Environment Variables

```bash
# Create .env file with your credentials
cat > .env << 'EOF'
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_CHAT_ID
EOF
```

### Step 7: Test the Bot

```bash
source venv/bin/activate
python -m src.main
```

If it connects and shows trades, press `Ctrl+C` to stop.

### Step 8: Setup Systemd Service (Auto-start & Auto-restart)

```bash
sudo nano /etc/systemd/system/polytracker.service
```

Paste this content:

```ini
[Unit]
Description=PolyTracker - Polymarket Insider Bot
After=network.target

[Service]
Type=simple
User=polytracker
WorkingDirectory=/home/polytracker/polymarket bot
Environment=PATH=/home/polytracker/polymarket bot/venv/bin
ExecStart=/home/polytracker/polymarket bot/venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable polytracker
sudo systemctl start polytracker
```

### Step 9: Check Status & Logs

```bash
# Check if running
sudo systemctl status polytracker

# View live logs
sudo journalctl -u polytracker -f

# View last 100 lines
sudo journalctl -u polytracker -n 100
```

### Step 10: Useful Commands

```bash
# Restart bot
sudo systemctl restart polytracker

# Stop bot
sudo systemctl stop polytracker

# Start bot
sudo systemctl start polytracker

# View logs
sudo journalctl -u polytracker -f
```

---

## Option 2: Railway (Easiest - No Server Management)

Railway handles all infrastructure automatically.

### Step 1: Prepare Your Code

Create a `Procfile` in your project root:

```
worker: python -m src.main
```

Create/update `requirements.txt`:

```bash
cd "/Users/milann.eth/Desktop/polymarket bot"
pip freeze > requirements.txt
```

### Step 2: Deploy to Railway

1. Sign up at [railway.app](https://railway.app)
2. Click "New Project" → "Deploy from GitHub repo"
3. Connect your GitHub and select the repo
4. Add environment variables in Railway dashboard:
   - `DISCORD_WEBHOOK_URL`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. Railway auto-deploys on every git push

### Step 3: Check Logs

View logs directly in Railway dashboard under "Deployments" → "View Logs"

---

## Option 3: Hetzner (Cheapest)

Similar to DigitalOcean but cheaper (~$3.29/mo for CX11).

1. Sign up at [hetzner.com/cloud](https://www.hetzner.com/cloud)
2. Create CX11 server (1 vCPU, 2GB RAM)
3. Follow same steps as DigitalOcean (Steps 2-10)

---

## Option 4: AWS Lightsail

1. Go to [lightsail.aws.amazon.com](https://lightsail.aws.amazon.com)
2. Create instance → Linux → Ubuntu 22.04
3. Choose $3.50/mo plan
4. Follow same setup steps as DigitalOcean

---

## Troubleshooting

### Bot not starting?

```bash
# Check logs for errors
sudo journalctl -u polytracker -n 50

# Test manually
cd "/home/polytracker/polymarket bot"
source venv/bin/activate
python -m src.main
```

### Connection timeouts?

Cloud servers usually don't have Polymarket blocked. If they do:

```bash
# Install Cloudflare WARP on Linux
curl -fsSL https://pkg.cloudflarewarp.com/pubkey.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflarewarp.com/ jammy main" | sudo tee /etc/apt/sources.list.d/cloudflare-client.list
sudo apt update && sudo apt install cloudflare-warp
warp-cli register
warp-cli connect
```

### High memory usage?

Reduce monitored markets in `src/main.py`:

```python
asset_ids = await fetch_top_markets(limit=100)  # Reduce from 200
```

### Bot keeps crashing?

The systemd service auto-restarts. Check why it's crashing:

```bash
sudo journalctl -u polytracker --since "1 hour ago"
```

---

## Monitoring & Alerts

### Option A: UptimeRobot (Free)

1. Sign up at [uptimerobot.com](https://uptimerobot.com)
2. Create a "Heartbeat" monitor
3. Add heartbeat ping to your bot code (optional enhancement)

### Option B: Check via Discord/Telegram

Your bot sends alerts - if alerts stop coming, the bot is down.

### Option C: Simple Cron Health Check

```bash
# Add to crontab (crontab -e)
*/5 * * * * systemctl is-active polytracker || systemctl restart polytracker
```

---

## Updating the Bot

### If using Git:

```bash
cd "/home/polytracker/polymarket bot"
git pull
sudo systemctl restart polytracker
```

### If uploading manually:

```bash
# From your local machine
scp -r "/Users/milann.eth/Desktop/polymarket bot/src" root@YOUR_IP:/home/polytracker/polymarket\ bot/
ssh root@YOUR_IP "systemctl restart polytracker"
```

---

## Cost Summary

| Setup | Monthly Cost | Annual Cost |
|-------|--------------|-------------|
| DigitalOcean Basic | $4-6 | $48-72 |
| Hetzner CX11 | $3.29 | ~$40 |
| AWS Lightsail | $3.50 | $42 |
| Railway (Free tier) | $0 | $0 |

---

## Quick Start Command Summary

```bash
# 1. Create droplet and SSH in
ssh root@YOUR_IP

# 2. One-liner setup (copy-paste this)
apt update && apt install -y python3 python3-pip python3-venv && \
mkdir -p /home/polytracker && cd /home/polytracker

# 3. Upload your bot from local machine
scp -r "/Users/milann.eth/Desktop/polymarket bot" root@YOUR_IP:/home/polytracker/

# 4. Setup and run
cd "/home/polytracker/polymarket bot" && \
python3 -m venv venv && \
source venv/bin/activate && \
pip install -r requirements.txt && \
python -m src.main
```

---

**Questions?** The bot is designed to auto-reconnect on failures. Once deployed, it should run indefinitely with minimal maintenance.
