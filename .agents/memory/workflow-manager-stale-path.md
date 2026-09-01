---
name: Workflow manager stale-path failure
description: Environment-specific failure mode when managed workflow tooling scans a deleted secondary-skill path.
---

Managed workflow restart and status operations may fail before launching or inspecting an app if the workspace index references a deleted path under `.local/secondary_skills`.

**Why:** The workflow manager's ripgrep scan exits nonzero for the missing path, so the application command is never reached; manually launching a second bot would bypass the managed workflow and risk duplicate processes.

**How to apply:** Confirm the app process count, avoid manual replacement processes, and report the platform-side blocker until the workspace index is repaired or refreshed.