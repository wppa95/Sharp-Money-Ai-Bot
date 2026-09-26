# Riot Developer Portal: API keys and rate limits

**URL:** https://developer.riotgames.com/docs/portal  
**Fetched:** 2026-09-26 by direct public HTTPS request  
**Tier:** 1 (first-party Riot documentation)

The portal distinguishes temporary development keys, personal keys, and production keys. Development keys deactivate every 24 hours. Personal keys are intended for the developer or a small private community; the page states limits of 20 requests per second and 100 requests per two minutes, enforced per region. Riot says a personal key may not be used for an application intended for public consumption.

The portal says production keys require product registration and approval. It gives starting production limits of 500 requests per 10 seconds and 30,000 requests per 10 minutes, per region; expanded limits require a separate application. These are published starting limits, not a guarantee that a specific product will be approved or receive them.

The page also describes application and method limits and advises clients to handle rate-limit responses. It does not promise match-history retention or establish that a player-history route contains professional tournament games.

## Evidence excerpt

Directly fetched portal text states that development keys “deactivate every 24 hours,” personal keys allow “20 requests every 1 second” and “100 requests every 2 minutes,” and personal keys may not run applications for public consumption. It lists production starting limits of “500 requests every 10 seconds” and “30,000 requests every 10 minutes.”

**Scope note:** This is documentation evidence only. It does not authorize this bot or substitute for a Riot product registration and applicable policy review.