---
name: Telegram transport error handling
description: How transient Telegram polling network failures are wrapped and logged.
---

Python-telegram-bot can wrap an underlying `httpx.ReadError` in `telegram.error.NetworkError` before passing it to the application error handler. Inspect the exception cause/context, not only the top-level type, before treating it as an application failure.

The bot and test harness opt into python-telegram-bot's `timedelta` form for
`RetryAfter` before importing Telegram modules. Normalize both legacy numeric
seconds and `timedelta` values to seconds at the transport boundary before
storing cooldowns or delivery telemetry.

**Why:** A transient Telegram connection reset previously reached the generic command error logger even though the polling loop recovered and the bot stayed healthy.

**Why:** The library is deprecating integer `RetryAfter` values; opting into
`timedelta` removes that warning while preserving cooldown durations.

**How to apply:** Classify wrapped read failures as transient warnings; reserve error-level handling for command/application failures and polling conflicts. Keep the Telegram compatibility setting before imports and normalize retry durations to seconds.