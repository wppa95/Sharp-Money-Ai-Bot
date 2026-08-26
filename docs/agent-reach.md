# Agent Reach analyst research

Sharp Money Bot uses Agent Reach as an optional, analyst-stage research
capability. It is not a scoring engine, a recommendation engine, or a
replacement for Underdog. Quantitative decisions are made first and remain
authoritative.

## Runtime and boundaries

The project uses the official `agent-reach` package at version 1.5.0. The
adapter invokes the documented CLI doctor command:

```bash
uv run agent-reach doctor --json
```

Agent Reach selects and health-checks upstream tools; application code does not
import a fabricated `agent_reach` research API. Research calls use the
doctor-reported upstream backend directly:

- RSS: `feedparser`, using public sport-specific RSS feeds by default.
- Web: `curl` + Jina Reader, only for URLs explicitly supplied through
  `AGENT_REACH_WEB_URLS` (Jina Reader is a reader, not a search engine).
- Exa search: `mcporter`, only when Agent Reach reports an active Exa backend.
- YouTube: `yt-dlp`, only when requested in `AGENT_REACH_SOURCES` and the
  doctor reports an active backend.

The adapter never installs missing tools during a research request, logs in to
websites, reads browser cookies, or accepts credentials through bot commands.
An unavailable or unauthenticated channel is recorded in the research trace.

## Replit channel check

The initial check in this environment found:

- Available: RSS (`feedparser`), Jina web reading, public V2EX, and public
  Bilibili search.
- Installed with a warning: YouTube (`yt-dlp`, missing its optional JS runtime).
- Executable but not authenticated: GitHub CLI.
- Not configured: Exa search, Twitter/X, Reddit, Facebook, Instagram,
  Xiaohongshu, LinkedIn, Xueqiu, and Xiaoyuzhou.

The bot defaults to `rss,exa_search`. This means RSS can contribute real
findings immediately, while Exa is explicitly reported unavailable until its
documented MCP configuration exists. Authenticated channels are not promised
or silently substituted.

## Configuration

All settings are optional and can be provided as environment variables:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `AGENT_REACH_ENABLED` | `true` | Enable analyst research requests |
| `AGENT_REACH_SOURCES` | `rss,exa_search` | Ordered source list |
| `AGENT_REACH_TIMEOUT_SECONDS` | `20` | Overall request timeout |
| `AGENT_REACH_SOURCE_TIMEOUT_SECONDS` | `10` | Per-source timeout |
| `AGENT_REACH_CACHE_TTL_SECONDS` | `1800` | Cache lifetime by canonical prop |
| `AGENT_REACH_COOLDOWN_SECONDS` | `900` | Minimum repeat-request interval |
| `AGENT_REACH_MAX_CONCURRENCY` | `2` | Concurrent research requests |
| `AGENT_REACH_MAX_FINDINGS` | `5` | Findings retained per request |
| `AGENT_REACH_WEB_URLS` | empty | Explicit URLs for Jina Reader |

`/analyst` is the current request boundary. It researches only the recent
alerted picks it is already preparing to explain, outside the Underdog scan
loop. Each candidate receives a separate
`decision_trace["external_research"]` object containing:

- the exact player/sport/stat/line/event request,
- attempted and successful sources,
- source-labelled findings and URLs,
- explicit per-source failures,
- timestamps and cache state,
- `success`, `partial`, `unavailable`, `failed`, or `skipped` status.

The analyst narrative labels external evidence separately from quantitative and
historical/market evidence. Missing external research never creates a positive
or negative betting signal.