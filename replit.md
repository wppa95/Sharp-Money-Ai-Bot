# Sharp Money +EV Detection Bot

A professional sports betting intelligence Telegram bot — foundation for a Sharp Money +EV Detection Platform.

## Run & Operate

- **Start the bot:** Run the `Sharp Money Bot` workflow (or `python bot/main.py`)
- The bot polls Telegram continuously; it must stay running to receive commands

## Stack

- Python 3.11
- python-telegram-bot 22 (async, APScheduler job queue)
- SQLAlchemy 2 + aiosqlite (async SQLite)
- python-dotenv for local dev

## Where things live

```
bot/
├── main.py       # Entry point — builds Application, registers lifecycle hooks
├── config.py     # All settings (reads TELEGRAM_TOKEN + optional env vars)
├── commands.py   # Telegram command handlers (/start /help /status /analyze /steam /ev)
├── alerts.py     # HTML alert formatters and broadcast helpers
├── engine.py     # Vig removal, EV calculation, steam detection, AI confidence scoring
├── database.py   # Async SQLAlchemy ORM + data access layer
├── models.py     # Plain dataclasses: OddsLine, EVOpportunity, SteamAlert, etc.
└── data/         # Auto-created — sharp_money.db lives here
```

## Architecture decisions

- `run_polling()` owns the event loop in PTB v20+; never wrap it in `asyncio.run()`
- Async DB setup uses PTB's `post_init` hook (not top-level `asyncio.run`)
- Database env var is `BOT_DATABASE_URL` (not `DATABASE_URL`) to avoid collision with Replit's managed Postgres
- All analysis logic is in `engine.py`; Telegram I/O stays in `commands.py` and `alerts.py`
- Background job stubs (`_poll_odds_job`, `_steam_check_job`) are pre-wired in `main.py` — fill in when live APIs are integrated

## Product

- `/start` — welcome and overview
- `/help` — command reference
- `/status` — uptime, DB record counts, market stats
- `/analyze [sport] [selection] [odds] [opp_odds]` — on-demand line analysis (vig removal + EV + Kelly)
- `/steam` — latest detected steam / sharp moves from DB
- `/ev` — latest +EV opportunities from DB
- Automatic alert broadcasting (ready to wire to live odds feed)

## User preferences

_Populate as you build._

## Gotchas

- Install packages with `installLanguagePackages` (skill), not `pip install` directly
- PTB v20+ event loop: `Application.run_polling()` blocks and manages its own loop
- `DATABASE_URL` is Replit-managed (Postgres); use `BOT_DATABASE_URL` for the bot's SQLite

## Roadmap stubs (ready to implement)

| Feature | Location |
|---------|----------|
| Live sportsbook odds | `engine.py → fetch_live_odds()` |
| PrizePicks monitoring | `engine.py → fetch_prizepicks_lines()` |
| ML confidence model | `engine.py → run_ml_model()` |
| CLV tracking | `engine.py → compute_clv()` |
| Periodic polling | `main.py → _poll_odds_job()` / `_steam_check_job()` |
| Discord integration | `alerts.py → broadcast_alert()` |
