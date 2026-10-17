---
name: Workflow process verification
description: Operational safeguard for managed bot workflow restarts that can leave an older process alive.
---

After restarting a managed bot workflow, verify that exactly one intended bot process owns the scheduler before relying on runtime or Telegram observations.

**Why:** A restart can leave an older process alive alongside the fresh process; both schedulers can poll providers and deliver duplicate Telegram messages.

**How to apply:** Check process count and startup times after every bot restart. Stop only the older orphan when a duplicate is confirmed, then recheck heartbeat, provider activity, and job completion.

Keep long verification logs in an ignored workspace location, not solely `/tmp`.
A lost background task is not a passing check.

**Why:** An unexpected workspace restart discarded the temporary test log and
stopped all managed workflows, even though project files and SQLite survived.

**How to apply:** Rerun interrupted verification with durable output, then restore
the previously running services and verify process ownership and health again.