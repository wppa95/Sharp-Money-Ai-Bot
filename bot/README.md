# Sharp Money +EV Detection Bot

A professional sports betting intelligence Telegram bot — the foundation of a
**Sharp Money +EV Detection Platform**.

## What It Does

- 🚨 Detects steam moves and sharp money line changes across sportsbooks  
- 🎯 Removes sportsbook vig to calculate **fair probabilities**  
- 💰 Calculates **Expected Value (+EV)** for any line  
- 🤖 Scores **AI confidence** using multi-signal analysis  
- 📊 Stores all alerts and odds history in a local SQLite database  
- ✅ Sends high-confidence alerts directly to Telegram  

---

## Project Structure

```
bot/
├── main.py        # Bot startup, Telegram Application, background jobs
├── config.py      # All settings loaded from environment / .env
├── commands.py    # Telegram command handlers (/start, /help, /status, etc.)
├── alerts.py      # Alert formatters and Telegram message dispatch
├── engine.py      # Analysis engine: vig removal, EV, steam detection, AI score
├── database.py    # Async SQLAlchemy + SQLite (ORM models + data access)
├── models.py      # Plain Python dataclasses: OddsLine, EVOpportunity, etc.
├── .env.example   # Template — copy to .env and fill in values
└── data/          # Auto-created — SQLite database lives here
```

---

## Quick Start

### 1. Get a Telegram Bot Token

Message [@BotFather](https://t.me/BotFather) on Telegram:

```
/newbot
```

Copy the token it gives you.

### 2. Set the Environment Variable

**On Replit:** Add `TELEGRAM_BOT_TOKEN` as a Secret (not a plain env var).

**Locally:**

```bash
cp bot/.env.example bot/.env
# Edit bot/.env and set TELEGRAM_BOT_TOKEN
```

### 3. Run the Bot

```bash
python bot/main.py
```

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and overview |
| `/help` | Show all available commands |
| `/status` | Bot uptime, market stats, DB record counts |
| `/analyze [sport] [selection] [odds] [opp_odds]` | Manually analyze any line |
| `/steam` | Show latest detected steam moves |
| `/ev` | Show latest +EV opportunities |

### /analyze Examples

```
/analyze NFL Chiefs-3 -110 -110
/analyze NBA LeBron_Over_25.5_Points -115 -105
/analyze MLB Dodgers +120 -140
```

---

## Alert Format

```
🚨 SHARP MONEY ALERT 🚨

Sport:   NFL
Market:  Spread
Event:   Chiefs vs Raiders
Line:    Chiefs -3 (−110)

📈 Odds Movement
  Opening:  -105
  Current:  -115
  Change:   -10

🔥 Steam Score:  82/100
📊 Books Moved:  DraftKings, FanDuel, BetMGM

🕐 2025-01-15 14:32 UTC
```

---

## Analysis Engine

### Vig Removal
Uses the **multiplicative method** to de-vig a two-sided market, producing a
fair probability that sums to exactly 1.0 across both sides.

### Expected Value
```
EV% = (fair_probability × decimal_odds − 1) × 100
```
Positive EV = the market is offering more than the true probability warrants.

### Kelly Criterion
```
Kelly = (b × p − q) / b
```
Where `b` = net profit per unit, `p` = fair probability, `q` = 1 − p.
Half-Kelly is provided for conservative bankroll management.

### Steam Score (0–100)
| Signal | Max Points |
|--------|-----------|
| Odds movement magnitude | 40 |
| Number of books moved | 30 |
| Line (spread/total) change | 20 |
| Public % vs movement (placeholder) | 10 |

### AI Confidence (0–100)
| Signal | Max Points |
|--------|-----------|
| EV magnitude | 25 |
| Steam score | 25 |
| Line shopping gap | 20 |
| Historical model (placeholder) | 20 |
| Market liquidity (placeholder) | 10 |

---

## Roadmap / Placeholder Stubs

The engine is pre-wired with placeholder stubs for:

| Feature | Location |
|---------|----------|
| Live sportsbook odds | `engine.py → fetch_live_odds()` |
| PrizePicks monitoring | `engine.py → fetch_prizepicks_lines()` |
| ML model integration | `engine.py → run_ml_model()` |
| CLV tracking | `engine.py → compute_clv()` |
| Background polling job | `main.py → _poll_odds_job()` |
| Steam sweep job | `main.py → _steam_check_job()` |
| Public betting % signal | `engine.py → SteamDetector` comments |
| Discord integration | Add to `alerts.py → broadcast_alert()` |
| Dashboard analytics | Build a FastAPI + React frontend |

---

## Configuration Reference

See `.env.example` for the full list of environment variables.

Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | **Required.** From @BotFather. |
| `ALLOWED_USER_IDS` | (all) | Comma-separated Telegram user IDs |
| `MIN_EV_THRESHOLD` | 3.0 | Minimum EV% to flag an opportunity |
| `MIN_STEAM_SCORE` | 70 | Minimum steam score to fire an alert |
| `MIN_AI_CONFIDENCE` | 60 | Minimum AI confidence for alerts |
| `ODDS_POLL_INTERVAL` | 60 | Seconds between odds polls |
| `STEAM_CHECK_INTERVAL` | 30 | Seconds between steam sweeps |
| `DATABASE_URL` | SQLite | Full SQLAlchemy async URL |
| `LOG_LEVEL` | INFO | DEBUG / INFO / WARNING / ERROR |
