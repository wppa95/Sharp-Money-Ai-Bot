# Riot Games Match-V5 and Developer Portal (direct fetch)

**URL:** https://developer.riotgames.com/apis#match-v5  
**Portal/policy URL:** https://developer.riotgames.com/docs/portal  
**Fetched:** 2026-09-26  
**Tier:** 1 (first-party Riot documentation)

The official APIs page lists `match-v5` as a Riot API service. The endpoint detail is loaded dynamically by the portal; the page's static HTML therefore proves the service listing but not a complete rendered schema in this saved artifact. The documented Match-V5 routes (on the same official portal) are the match-by-ID route and the match IDs by PUUID route. Match data is keyed by Riot's `matchId`; the response's `info` contains the game's participants and their per-player statistics (including the participant PUUID and game result fields). This is ordinary League match history, not a professional-esports feed.

The portal says signing in generates a development key. It says development keys deactivate every 24 hours. A personal key requires registering a product; the stated personal limits are 20 requests/second and 100 requests/2 minutes, per region. The portal explicitly says personal keys are for a developer or small private community and may not be used for a public-consumption application. Production products require their own application/approval. Riot's general policy page remains the authority for reuse and product restrictions; possession of a publicly visible endpoint does not grant a bot license.

The portal also explains that application and method limits are per API key and region, and that a 429 may be returned by underlying services. The page does not promise indefinite history or a retention period for Match-V5. It documents API access controls and limits, not a downloadable historical professional-match archive.

## Evidence excerpt

Directly fetched portal HTML states:

* “Anyone who signs into the developer portal will automatically be granted an API key.”
* Development keys “deactivate every 24 hours.”
* Personal keys: “20 requests every 1 second” and “100 requests every 2 minutes.”
* “You may not run your application for public consumption using a personal key.”
* Rate limits are enforced per region and include application and method limits.

**Uncertainty:** Because the API detail block is dynamically loaded, this artifact does not reproduce the rendered JSON schema. Validate field names against the current Match-V5 page and an authorized response before implementation. The conclusion that Match-V5 is ordinary ranked/player history follows from its PUUID-based match-history model and should not be mistaken for professional tournament coverage.