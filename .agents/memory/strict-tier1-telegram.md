---
name: Telegram sport scope
description: Football-family Telegram scope and canonical evidence requirements for actionable alerts.
---

No football-family competition or alias is eligible for Telegram delivery, including NFL, CFB/NCAAF, FCS, CFL, AFL/AFLW, UFL, and rugby. Continue collecting and retaining supported football data for database monitoring, analysis, history, and grading; the delivery block must not erase evidence or disable provider ingestion. Supported non-football sports—including soccer, FIFA, CS2 (canonical CS), LoL, WNBA/NBA, UFC, VAL(ORANT), Dota, KBO, and NPB—remain eligible when their live feed supplies a valid market and explicit OVER/UNDER direction. MLB, NBA, NFL, WNBA, and NHL retain their existing completed-history rules; other sports retain their current sport-specific history policy. Do not invent official market mappings for grading. Stars, score, confidence, Bet Quality, Market Quality, and S/A/B/C tier remain internal/reporting metadata and must not be rendered in Telegram delivery. Existing safety, live-game, deduplication, freshness, scope, rate-limit, and validity protections remain operational safeguards.

**Why:** The user now requires all football-related sports to remain database-monitored but blocked from Telegram; this supersedes the earlier NFL/CFB delivery allowance. KBO/NPB and other non-football sport eligibility/history rules remain unchanged.

**How to apply:** Enforce the football-family prohibition centrally at the final Telegram send boundary and before alert queue retries. Keep provider observation, persistence, history, and grading paths independent from this delivery rule. Do not alter non-football eligibility, history requirements, timing, freshness, tracking, deduplication, or operator-disable checks.