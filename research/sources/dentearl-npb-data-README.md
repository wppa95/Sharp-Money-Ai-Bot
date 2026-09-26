# NPB Data Pipeline (`dentearl/npb-data`)

Automated regular-season schedule and game results pipeline for Nippon Professional Baseball (NPB).

## Purpose
This repository serves static, daily-updated JSON datasets of NPB regular-season games (including tie games, scores, and standings) across seasons 2021 through 2026 for both the Central League (セ・リーグ) and Pacific League (パ・リーグ) via `raw.githubusercontent.com`.

The live baseball division visualizer dynamically fetches from:
```
https://raw.githubusercontent.com/dentearl/npb-data/main/data/npb/{season}.json
```
Available seasons: `2021`, `2022`, `2023`, `2024`, `2025`, `2026` (each season contains exactly 143 regular season games per team across all 12 clubs, 858 games total).

## Security & Architecture
- **Zero Secrets / Zero Tokens**: Uses standard unauthenticated HTTP GET requests on the frontend and ephemeral, default `GITHUB_TOKEN` (`contents: write`) in GitHub Actions.
- **Server Friendly**: Intelligent monthly caching prevents excessive requests to official NPB servers (`npb.jp`).
- **Automated Nightly Run**: Scheduled via GitHub Actions daily at `15:30 UTC` (`00:30 JST`), shortly after night games finish.

## Manual Run
```bash
npm install

# Scrape current active season
node scripts/scrapeNPB.js --season 2026

# Scrape specific seasons or all historical seasons
node scripts/scrapeNPB.js --seasons 2021,2022,2023,2024,2025,2026
node scripts/scrapeNPB.js --all
```
