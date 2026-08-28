---
name: Workflow process verification
description: Operational safeguard for managed bot workflow restarts that can leave an older process alive.
---

After restarting a managed bot workflow, verify that exactly one intended bot process owns the scheduler before relying on runtime or Telegram observations.

**Why:** A restart can leave an older process alive alongside the fresh process; both schedulers can poll providers and deliver duplicate Telegram messages.

**How to apply:** Check process count and startup times after every bot restart. Stop only the older orphan when a duplicate is confirmed, then recheck heartbeat, provider activity, and job completion.