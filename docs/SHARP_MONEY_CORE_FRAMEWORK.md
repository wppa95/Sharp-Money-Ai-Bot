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