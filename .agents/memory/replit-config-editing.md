---
name: Validated Replit config edits
description: Replit workspace constraint for changing the .replit configuration.
---

Direct edits to `.replit` and `replit.nix` are rejected by this workspace. Write the complete updated TOML to a temporary file inside the workspace, then use `verifyAndReplaceDotReplit` with that file's absolute path.

**Why:** The workspace validates Replit configuration before replacing it; ordinary file-edit tools cannot bypass that validation.

**How to apply:** Preserve the existing configuration, make the smallest change, write the full TOML to a temporary workspace file, and call the verifier rather than editing `.replit` directly.