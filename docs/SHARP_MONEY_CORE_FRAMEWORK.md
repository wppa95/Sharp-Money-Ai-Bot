# Sharp Money Bot — Core Framework v3.0

## Purpose

This document defines the core architecture for Sharp Money Bot.

All future features, improvements, alerts, rankings, explanations, grading, dashboards, risk decisions, and AI analysis must integrate through this framework.

The goal is to evolve Sharp Money Bot from an alert generator into an AI betting analyst.

---

# Foundation Architecture

The following layers are the permanent core framework:

1. Canonical Identity Layer

2. Unified Candidate Contract

3. Error Taxonomy

4. Explanation Service

5. Confidence Separation

6. Hard Block vs Risk Warning System

7. Learning Labels & Variance Protection

8. Session Recovery & Job Isolation

9. Feature Flag System

---

# Core Rules

## No Parallel Systems

Sharp Money Bot must not maintain duplicate systems.

Do not create:

- Multiple confidence engines
- Multiple scoring systems
- Multiple explanation engines
- Multiple recommendation systems
- Parallel decision paths

Existing functionality should be extended, adapted, or deprecated through wrappers.

---

# Unified Candidate Contract

All decisions must flow through one Candidate object.

Candidate must contain:

- Player identity
- Provider IDs
- Sport and league
- Event identity
- Market identity
- Raw snapshot reference
- Data Quality Score
- Market Confidence
- Betting Edge Confidence
- Overall Confidence
- Tier
- Risk Level
- Decision Trace
- Learning Classification

All systems consume Candidate:

- Alerts
- Rankings
- Explanations
- Blocks
- Grading
- AI Analyst
- Dashboard

---

# Explanation Service

All explanations come from one centralized service.

Architecture:

Candidate  
↓  
Explanation Service  
↓  
Telegram  
Dashboard  
AI Analyst

The Explanation Service:

- Does not recalculate confidence
- Does not create separate scoring
- Does not pull live data to rewrite decisions

Explanations must come from stored decision artifacts.

---

# Confidence Separation

Replace proxy confidence systems.

Separate:

## Data Confidence

How reliable is the information?

## Market Confidence

How reliable is the movement?

## Betting Edge Confidence

Does actual betting value exist?

## Overall Confidence

Final recommendation strength.

A high data confidence score does not automatically mean a good bet.

---

# Hard Block vs Risk Warning

## Hard Block

Prevents ticket generation.

Requires:

- Reason code
- Duration
- Review date
- Explanation

## Risk Warning

Allows consideration but lowers confidence or tier.

---

# Learning Protection

Every result must classify as:

- Model Error
- Market Error
- Settlement Error
- Variance

Only Model Error should update scoring weights.

Variance should not damage the model.

---

# Error Taxonomy

Provider failures and bot failures must remain separate.

## Provider Errors

Examples:

- API failure
- Quota limits
- Invalid response
- Missing data

## Bot Errors

Examples:

- Code failure
- Database failure
- Crash
- Processing failure

Each requires separate recovery behavior.

---

# Validation Requirements

Before replacing existing systems:

- Verify replacement behavior
- Pass tests
- Preserve functionality

Deprecate old paths through adapters before removal.

---

# Testing Requirements

## Contract Tests

- Candidate Contract
- Canonical Market Identity
- Canonical Player Identity

## Snapshot Tests

Same input must produce:

- Same decision
- Same explanation
- Same confidence

## Integration Tests

Validate:

- Provider failures
- Retry logic
- Job isolation
- Crash recovery
- Session recovery
- Explanation generation
- Confidence separation
- Block system
- Learning labels
- Regression suite

---

# Implementation Priority

1. Stability and crash recovery
2. Data quality
3. Confidence separation
4. Prop intelligence
5. Risk and block systems
6. AI explanations
7. Learning loop
8. Telegram/dashboard improvements
9. Multi-sport expansion

---

# Final Goal

Sharp Money Bot should:

- Understand markets
- Understand players
- Understand risk
- Explain decisions
- Learn from mistakes
- Improve automatically


# Sharp Money Bot — Prop Intelligence System Upgrade Batch

Execute this as one complete implementation batch.

Do not start partial implementations.

Execute Phase 0 Repository Audit before making any code changes.

Do not begin implementation until you have:

1. Audited the repository.
2. Mapped the existing architecture.
3. Confirmed what already works.
4. Identified the correct integration points.
5. Created an internal implementation plan.

After completing the audit:
- Summarize the current architecture.
- Identify reusable components.
- Identify duplicate/conflicting logic.
- Proceed directly into implementation unless a true breaking issue is discovered.

Do not rebuild Sharp Money Bot.

This is an upgrade to the existing v3 architecture.

---

# Implementation Rules

Preserve working functionality.

Extend existing systems instead of creating replacements.

Do not create:

- Duplicate scoring systems.
- Duplicate confidence engines.
- Duplicate explanation systems.
- Duplicate recommendation pipelines.
- Parallel decision paths.

Existing modules should be reused, extended, or deprecated through wrappers.

Run tests after implementation.

Required validation:

- Contract tests
- Snapshot tests
- Integration tests
- Existing regression tests

---

# Current Production Context

Current working systems:

- Underdog connector is working and collecting market snapshots.
- Existing scoring pipeline exists and must be reused.
- Telegram integration exists and must be refined.
- Scheduler exists and must be preserved.
- Database history exists and must be protected.
- Previous test suite had 900+ passing tests.

Current problems:

- Telegram receives too many PLAYER PROP MARKET ALERT messages that show availability instead of playable opportunities.
- HealthTracker shows unexpected_exit restarts.
- Crash context is not persistent enough.
- Recommendation filtering needs improvement.
- Runtime recovery needs improvement.
- Disabled provider noise affects clarity.

---

# Production Scope

Current production focus:

UNDerdog Prop Intelligence System only.

For this batch:

ACTIVE:
- Underdog monitoring
- Underdog snapshots
- Underdog line movement
- Underdog recommendations
- Underdog results tracking

DISABLED:
- DraftKings
- FanDuel
- PrizePicks
- Other sportsbook prop integrations

Disabled providers must remain isolated behind feature flags.

They must not affect:

- Runtime
- Alerts
- Startup logs
- Health checks
- Scheduler jobs
- Model decisions

Do not spend this batch restoring disabled providers.

---

# Upgrade Objectives

Complete all objectives together:

1. Cleaner Underdog-only production pipeline.

2. S/A/B recommendation alerts.

3. Rich prop analysis packets.

4. Continuous market surveillance.

5. Results tracking and learning loop.

6. Persistent crash detection and recovery.

7. Runtime watchdog and recovery behavior.

---

# Telegram Alert Rules

Telegram should only send playable recommendations.

Allowed:

⭐⭐⭐⭐⭐ S Tier

⭐⭐⭐⭐ A Tier

⭐⭐⭐ B Tier

Blocked:

⭐⭐ C Tier

⭐ D Tier


No market availability alerts.

No raw PLAYER PROP MARKET ALERT spam.

Every alert should include:

- Player
- Prop
- Line
- Tier
- Stars
- Confidence
- Risk
- Short explanation

---

# Recommendation Package

Playable recommendations should include:

Player Data:

- L5 hit rate
- L10 hit rate
- Season hit rate
- Average stat
- Recent trend

Matchup Data:

- Opponent
- Defensive matchup
- Player role
- Minutes/usage
- Injury/news impact

Market Data:

- Opening line
- Current line
- Movement direction
- Movement magnitude
- Edge vs line

Model Data:

- Projection
- Confidence
- Tier
- Star rating
- Reason the model likes the play

Keep explanations concise.

---

# Market Surveillance

Monitor Underdog markets continuously.

Track:

- Available props
- Removed props
- Opening lines
- Current lines
- Line movement history
- Movement timing

Persist market history.

Goal:

Identify edges before props disappear or move.

---

# Results Tracking And Learning Loop

Store every recommendation before games.

Store:

- Player
- Prop
- Line
- Tier
- Stars
- Confidence
- Reason

After games store:

- Result
- Correct
- Incorrect
- Push
- Void
- Cancelled
- Injury-related void
- Game interruption

Store why the result happened:

- Model Error
- Market Error
- Settlement Error
- Variance

Only Model Error should influence future scoring changes.

Do not treat normal variance as model failure.

Create rollups:

- S Tier record
- A Tier record
- B Tier record
- Sport record
- Prop type record
- Player trends

Learning updates must be feature-flagged and tested before affecting live scoring.

---

# Tier System

Final grading:

⭐⭐⭐⭐⭐ = S Tier

⭐⭐⭐⭐ = A Tier

⭐⭐⭐ = B Tier

⭐⭐ = C Tier

⭐ = D Tier


Internal systems may keep full grading.

Telegram only shows S/A/B.

---

# Crash Detection And Recovery

Add persistent runtime tracking.

Store:

- Last heartbeat
- Last running job
- Last completed task
- Last successful market scan
- Last database write
- Runtime status
- Restart reason
- Error before crash

Capture:

- Exception traceback
- Failed job
- Active module
- Pipeline stage

Telegram restart alert:

⚠️ Sharp Money Bot Restarted

Reason:
Unexpected Exit

Last Active Task:
Underdog Market Monitor

Recovery Status:
Resumed Monitoring ✅

---

# Runtime Reliability

The bot should behave like a production service.

Add:

- Watchdog monitoring
- Heartbeat system
- Runtime health checks
- Automatic recovery
- Scheduler resume after restart
- State preservation

If Replit cannot guarantee true 24/7 execution, identify the limitation and implement the strongest available workaround.

Expected behavior:

Browser closed ✅

Replit tab closed ✅

Crash detected ✅

Failure logged ✅

Telegram notified ✅

Recovery attempted ✅

Monitoring resumed ✅

---

# Recommendation Gate

Increase actionable recommendations without returning to noise.

Allow:

- S Tier
- A Tier
- Strong B Tier (3+ stars)

Filter:

- Weak B
- C Tier
- D Tier

---

# Final Execution Requirement

Before changing code:

Audit first.

Reuse existing architecture.

Protect existing data.

Implement as one integrated upgrade.

Run tests.

Provide final report:

- Files modified
- Architecture changes
- Tests added
- Tests passing
- Remaining recommendations
- Deferred improvements and reasons

Goal:

Transform Sharp Money Scanner into:

Underdog Prop Intelligence System

A system that:

- Watches markets all day
- Finds edges early
- Analyzes props
- Explains decisions
- Tracks results
- Learns over time
- Recovers automatically
- Operates continuously