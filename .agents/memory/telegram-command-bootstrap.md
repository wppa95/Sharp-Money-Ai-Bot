---
name: Telegram command bootstrap
description: Distinguishes Telegram credential bootstrap failures from command registration failures.
---

An invalid Telegram bot token prevents Application initialization and polling before any CommandHandler can receive updates, making every command appear broken at once. Validate the Telegram bootstrap/polling path before debugging individual callbacks; keep the production command map centralized so the live Application registration can be verified independently.

**Why:** A prior bot session failed during `getMe` with an invalid-token response, while the command callbacks and registrations were otherwise present. A later session with an accepted workspace secret reached polling normally.

**How to apply:** Check for successful `getMe`, webhook cleanup, `Application started`, and repeated `getUpdates` before diagnosing command logic. Never store or print the token; request credential repair through the workspace secret flow if bootstrap rejects it.