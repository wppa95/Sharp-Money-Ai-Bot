# Oracle's Elixir match-data downloads (direct fetch)

**URL:** https://oracleselixir.com/tools/downloads  
**Download link exposed by the publisher:** https://drive.google.com/drive/u/1/folders/1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH  
**Fetched:** 2026-09-26  
**Tier:** 1 (publisher-owned page; the downloadable files are a separate Google Drive host)

The publisher's page describes “a collection of data files containing match data from the LCS, LEC, LCK, LPL, PCS, CBLoL, and many more leagues.” It says files are CSV, aggregated and released by Tim Sevenhuysen of OraclesElixir.com, provided free of charge, and intended for analysts, commentators, and fans. It says files are updated **once per day** and that definitions are on the site's Definitions page. This is professional League of Legends esports data, not ordinary ranked history.

The same page provides a Google Drive folder rather than an invented or undocumented CSV URL. Its “Available Downloads” notice says Riot Games owns the game statistics and any use must follow Riot Games' terms and policies. Consequently, public download access is not evidence of an unrestricted commercial/bot license. The page does not state an API key requirement, request quota, SLA, or retention guarantee for the Drive files. Downloads are human/file-oriented rather than a documented per-player API.

The publisher's site and definitions describe game-level rows and player/team fields in the downloadable match data. Stable identifiers commonly used by the files include game/match and player identifiers; exact column names and historical file contents can change, so inspect the current definitions and file header before mapping. Completion is inferred from the file's inclusion of a finished professional game and result/win fields; the page does not publish a separate completion-state protocol. A missing or newly revised row must not be treated as an explicit “complete” signal.

**Reuse restriction:** The page explicitly incorporates Riot Games' terms/policies. Oracle's “free of charge” and intended audience language does not itself grant permission to redistribute, commercialize, or use the data in a bot. Obtain permission/confirm terms with the publisher and Riot before production use.

**Source-retrieval note:** The site's HTML is a React shell; the quoted text was verified from the publisher's publicly served JavaScript bundle. No undocumented API endpoint or guessed CSV URL is used here.