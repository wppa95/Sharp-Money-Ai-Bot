---
name: PropLine endpoint safety
description: How the authorized PropLine PrizePicks adapter handles endpoint configuration.
---

PropLine's documented base is `https://api.prop-line.com`; the client uses it by default and permits `PROPLINE_API_URL` only as an authorized override. Active sports come from the documented `/v1/sports` discovery route, then current PrizePicks lines come from the sport-scoped odds route filtered to `bookmakers=prizepicks`, with standard DFS lines retained.

**Why:** The onboarding endpoint initially returned service metadata rather than lines. Its official docs identify the supported `/v1/sports/{sport_key}/odds` route; the response includes standard, goblin, and demon variants that must not be collapsed into one monitoring line.

**How to apply:** Never guess an API path or host. Discover active sports, use one sport-scoped request at a time, keep the existing pacing/cache/backoff and daily budget, and use fetch time for confirmation freshness because a current response can carry an older line-change timestamp.