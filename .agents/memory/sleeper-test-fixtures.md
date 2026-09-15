---
name: Sleeper test fixtures
description: Keep Sleeper NFL provider tests deterministic across season rollovers
---

Sleeper NFL tests preload the historical fixture season and must explicitly cache the current season as empty unless current-season data is part of the test. Otherwise date-dependent provider logic can make uncached live-week requests and add results to unit tests.

**Why:** The provider intentionally includes the current NFL season after the calendar rolls forward, while the tests are designed around a fixed historical slate.

**How to apply:** When the provider's season window changes, update the fixture cache for every non-fixture season and keep `_get_json` out of fetch-result tests unless a test is explicitly exercising prefetch behavior.