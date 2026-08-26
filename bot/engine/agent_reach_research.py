"""
Agent Reach research adapter for the analyst layer.

Agent Reach is intentionally used as its official capability router/doctor.  It
is not imported as an application API and it does not participate in scoring.
After the doctor reports a backend, this module invokes the documented upstream
tool directly (feedparser, mcporter, yt-dlp, or curl + Jina Reader).

The public entry point is ``research_candidate``.  It is asynchronous, bounded,
cacheable, and failure-isolated so callers can request context without making
external research part of the market scan hot path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


_DEFAULT_RSS_FEEDS: dict[str, tuple[str, ...]] = {
    "NBA": (
        "https://www.cbssports.com/rss/headlines/nba/",
        "https://sports.yahoo.com/nba/rss/",
    ),
    "WNBA": ("https://sports.yahoo.com/wnba/rss/",),
    "NFL": (
        "https://www.cbssports.com/rss/headlines/nfl/",
        "https://sports.yahoo.com/nfl/rss/",
    ),
    "MLB": ("https://www.espn.com/espn/rss/mlb/news",),
    "NHL": ("https://sports.yahoo.com/nhl/rss/",),
    "SOCCER": ("https://sports.yahoo.com/soccer/rss/",),
}

_SUPPORTED_SOURCES = frozenset({"rss", "exa_search", "youtube", "web"})
_STAT_SANITIZER = re.compile(r"[^a-z0-9]+")


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _text(value: Any, limit: int = 800) -> str:
    """Return bounded, whitespace-normalized text from an upstream response."""
    if value is None:
        return ""
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value[:limit]


def _source_name(value: Any) -> str:
    return _text(value, 80).lower().replace(" ", "_")


def _safe_error(exc: BaseException) -> str:
    """Keep subprocess/network errors useful without reading secret values."""
    return _text(exc, 240) or exc.__class__.__name__


def _g(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


@dataclass(frozen=True)
class NormalizedPropSource:
    """Canonical identity plus source-specific observation data."""

    provider: str
    player_name: str
    sport: str
    stat_type: str
    line: float
    odds: Optional[int] = None
    event: str = ""
    event_key: Optional[str] = None
    source_id: str = ""
    observed_at: Optional[str] = None

    @property
    def canonical_key(self) -> str:
        stat = _STAT_SANITIZER.sub("_", self.stat_type.lower()).strip("_")
        player = _STAT_SANITIZER.sub("_", self.player_name.lower()).strip("_")
        event = _STAT_SANITIZER.sub("_", (self.event_key or self.event).lower()).strip("_")
        suffix = f":{event}" if event else ""
        return f"{self.sport.upper()}:{player}:{stat}:{self.line:g}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "player_name": self.player_name,
            "sport": self.sport.upper(),
            "stat_type": self.stat_type,
            "line": self.line,
            "odds": self.odds,
            "event": self.event,
            "event_key": self.event_key,
            "source_id": self.source_id,
            "observed_at": self.observed_at,
            "canonical_key": self.canonical_key,
        }


def normalize_prop_source(
    record: Any,
    *,
    provider: Optional[str] = None,
) -> NormalizedPropSource:
    """
    Normalize Underdog/PrizePicks/sportsbook-like records without changing them.

    This is deliberately additive.  A source-specific record remains the source
    of truth; the normalized value only provides a stable comparison identity.
    """
    player = _g(record, "player_name", None) or _g(record, "player", None) or _g(record, "selection", "")
    stat = _g(record, "stat_type", None) or _g(record, "market_type", "")
    line = _g(record, "line_value", None)
    if line is None:
        line = _g(record, "line", 0.0)
    try:
        line_float = float(line)
    except (TypeError, ValueError):
        raise ValueError(f"prop source line must be numeric, got {line!r}") from None

    observed = _g(record, "fetched_at", None) or _g(record, "recorded_at", None)
    if isinstance(observed, datetime):
        observed = observed.isoformat()

    return NormalizedPropSource(
        provider=str(provider or _g(record, "provider", "") or "Unknown"),
        player_name=str(player or "unknown").strip(),
        sport=str(_g(record, "sport", "") or "UNKNOWN").upper(),
        stat_type=str(stat or "unknown").strip(),
        line=line_float,
        odds=_g(record, "odds", None) if _g(record, "odds", None) is not None else _g(record, "american_odds", None),
        event=str(_g(record, "event", "") or _g(record, "game_description", "") or ""),
        event_key=_g(record, "event_key", None) or _g(record, "game_id", None),
        source_id=str(_g(record, "external_id", "") or _g(record, "id", "") or ""),
        observed_at=str(observed) if observed else None,
    )


@dataclass(frozen=True)
class ResearchRequest:
    """Specific, reproducible research request for one prop candidate."""

    player_name: str
    sport: str
    stat_type: str
    line: float
    decision: str = "PASS"
    event_key: Optional[str] = None
    event: str = ""
    opponent: str = ""
    team: str = ""
    game_time: Optional[datetime] = None
    provider: str = "Underdog"

    @property
    def canonical_key(self) -> str:
        return NormalizedPropSource(
            provider=self.provider,
            player_name=self.player_name,
            sport=self.sport,
            stat_type=self.stat_type,
            line=self.line,
            event=self.event,
            event_key=self.event_key,
        ).canonical_key

    @property
    def query(self) -> str:
        context = " ".join(
            value for value in (
                self.player_name,
                self.sport,
                self.stat_type,
                f"line {self.line:g}",
                self.opponent,
                self.team,
                self.event,
            ) if value
        )
        return _text(f"{context} injury lineup availability matchup news", 500)

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_name": self.player_name,
            "sport": self.sport.upper(),
            "stat_type": self.stat_type,
            "line": self.line,
            "decision": self.decision,
            "event_key": self.event_key,
            "event": self.event,
            "opponent": self.opponent,
            "team": self.team,
            "game_time": self.game_time.isoformat() if self.game_time else None,
            "provider": self.provider,
            "canonical_key": self.canonical_key,
            "query": self.query,
        }


@dataclass(frozen=True)
class ResearchFinding:
    source: str
    title: str
    summary: str
    url: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    evidence_type: str = "external"

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "evidence_type": self.evidence_type,
        }


@dataclass(frozen=True)
class ResearchResult:
    """Serializable external research outcome, including explicit failures."""

    status: str
    request: ResearchRequest
    attempted_sources: tuple[str, ...] = ()
    successful_sources: tuple[str, ...] = ()
    findings: tuple[ResearchFinding, ...] = ()
    failures: Mapping[str, str] = field(default_factory=dict)
    requested_at: str = field(default_factory=_now_iso)
    completed_at: str = field(default_factory=_now_iso)
    cached: bool = False
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "request": self.request.to_dict(),
            "attempted_sources": list(self.attempted_sources),
            "successful_sources": list(self.successful_sources),
            "findings": [finding.to_dict() for finding in self.findings],
            "failures": dict(self.failures),
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "cached": self.cached,
            "skipped_reason": self.skipped_reason,
        }


def build_research_request(candidate: Any) -> ResearchRequest:
    """Build a prop-specific request from a Candidate without recalculating it."""
    trace = getattr(candidate, "decision_trace", {}) or {}
    return ResearchRequest(
        player_name=str(getattr(candidate, "player_name", "") or "unknown"),
        sport=str(getattr(candidate, "sport", "") or "UNKNOWN").upper(),
        stat_type=str(getattr(candidate, "stat_type", "") or "unknown"),
        line=float(getattr(candidate, "line", 0.0) or 0.0),
        decision=str(getattr(candidate, "decision", "PASS") or "PASS"),
        event_key=getattr(candidate, "event_key", None),
        event=str(trace.get("event", "") or trace.get("game", "") or ""),
        opponent=str(trace.get("opponent", "") or ""),
        team=str(trace.get("team", "") or ""),
        game_time=getattr(candidate, "game_time", None),
        provider=str(getattr(candidate, "provider", "Underdog") or "Underdog"),
    )


def build_historical_market_evidence(candidate: Any) -> dict[str, Any]:
    """
    Project already-stored quantitative/history artifacts for analyst display.

    No DB or provider call occurs here.  Missing values remain absent/unknown;
    this function never invents a source, result, or hit rate.
    """
    trace = getattr(candidate, "decision_trace", {}) or {}
    evidence: dict[str, Any] = {"category": "historical_market", "available": False, "items": []}
    items: list[dict[str, Any]] = []

    pi = trace.get("prop_intelligence")
    if isinstance(pi, Mapping):
        hist = pi.get("historical")
        if isinstance(hist, Mapping):
            item = {
                key: hist[key]
                for key in ("n", "sample_strength", "hit_rate", "avg_vs_line", "variance")
                if key in hist and hist[key] is not None
            }
            if item:
                item["source"] = "stored_prop_intelligence"
                items.append(item)
        for window in ("l5", "l10", "l20", "l30", "season"):
            windows = hist.get("windows", {}) if isinstance(hist, Mapping) else {}
            window_data = windows.get(window) if isinstance(windows, Mapping) else None
            if isinstance(window_data, Mapping):
                items.append({
                    "source": "stored_prop_intelligence",
                    "window": window,
                    **{
                        key: window_data[key]
                        for key in ("n", "hit_rate", "avg_vs_line", "variance")
                        if key in window_data and window_data[key] is not None
                    },
                })

    for key in ("line_history", "market_comparison", "cross_source", "odds_confirmation", "clv"):
        value = trace.get(key)
        if value not in (None, "", {}, [], ()):
            items.append({"source": key, "value": value})

    evidence["items"] = items
    evidence["available"] = bool(items)
    return evidence


async def _run_process(
    args: Sequence[str],
    *,
    timeout: float,
    cwd: Optional[str] = None,
) -> str:
    """Run a documented upstream CLI with bounded output and timeout."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TimeoutError(f"command timed out after {timeout:.1f}s") from None
    if proc.returncode:
        detail = _text(stderr.decode(errors="replace") or stdout.decode(errors="replace"), 300)
        raise RuntimeError(f"command exited {proc.returncode}: {detail}")
    return stdout.decode(errors="replace")[:200_000]


class AgentReachAdapter:
    """
    Capability-aware adapter around Agent Reach's doctor and upstream tools.

    The adapter does not try to install missing tools at request time.  Installation
    and authentication are operator actions; unavailable channels are returned in
    the structured result.
    """

    def __init__(
        self,
        *,
        sources: Optional[Iterable[str]] = None,
        timeout_seconds: Optional[float] = None,
        source_timeout_seconds: Optional[float] = None,
        max_findings: Optional[int] = None,
        rss_feeds: Optional[Mapping[str, Sequence[str]]] = None,
        web_urls: Optional[Sequence[str]] = None,
    ) -> None:
        configured_sources = (
            sources
            if sources is not None
            else os.environ.get("AGENT_REACH_SOURCES", "rss,exa_search").split(",")
        )
        if isinstance(configured_sources, str):
            configured_sources = configured_sources.split(",")
        self.sources = tuple(
            source.strip()
            for source in configured_sources
            if source and source.strip() in _SUPPORTED_SOURCES
        )
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None
            else os.environ.get("AGENT_REACH_TIMEOUT_SECONDS", "20")
        )
        self.source_timeout_seconds = float(
            source_timeout_seconds if source_timeout_seconds is not None
            else os.environ.get("AGENT_REACH_SOURCE_TIMEOUT_SECONDS", "10")
        )
        self.max_findings = max(
            1,
            int(max_findings if max_findings is not None
                else os.environ.get("AGENT_REACH_MAX_FINDINGS", "5")),
        )
        self.rss_feeds = {
            sport.upper(): tuple(urls)
            for sport, urls in (rss_feeds or _DEFAULT_RSS_FEEDS).items()
        }
        configured_urls = web_urls
        if configured_urls is None:
            configured_urls = os.environ.get("AGENT_REACH_WEB_URLS", "").split(",")
        self.web_urls = tuple(url.strip() for url in configured_urls if url.strip())
        self._doctor_cache: Optional[tuple[float, dict[str, Any]]] = None

    async def doctor(self, *, force: bool = False) -> dict[str, Any]:
        """Return Agent Reach's real JSON doctor report, cached briefly."""
        if self._doctor_cache and not force and time.monotonic() - self._doctor_cache[0] < 300:
            return self._doctor_cache[1]
        try:
            raw = await _run_process(
                [sys.executable, "-m", "agent_reach.cli", "doctor", "--json"],
                timeout=min(self.timeout_seconds, 30),
            )
            report = json.loads(raw)
            if not isinstance(report, dict):
                raise ValueError("doctor output was not an object")
        except Exception as exc:  # doctor is diagnostic, never a hard dependency
            logger.warning("Agent Reach doctor unavailable: %s", _safe_error(exc))
            report = {"_runtime": {"status": "unavailable", "message": _safe_error(exc)}}
        self._doctor_cache = (time.monotonic(), report)
        return report

    @staticmethod
    def _backend_available(report: Mapping[str, Any], source: str) -> bool:
        entry = report.get(source)
        if not isinstance(entry, Mapping):
            return False
        return bool(entry.get("active_backend"))

    async def _search_rss(self, request: ResearchRequest) -> tuple[list[ResearchFinding], Optional[str]]:
        try:
            import feedparser
        except ImportError as exc:
            return [], _safe_error(exc)

        urls = self.rss_feeds.get(request.sport.upper(), ())
        if not urls:
            return [], f"no RSS feed configured for sport {request.sport.upper()}"

        tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z0-9']+", request.player_name)
            if len(token) >= 3
        }
        if not tokens:
            return [], "player name has no searchable tokens"

        def parse() -> list[ResearchFinding]:
            findings: list[ResearchFinding] = []
            feed_errors: list[str] = []
            for feed_url in urls:
                parsed = feedparser.parse(feed_url)
                status = _g(parsed, "status", None)
                entries = getattr(parsed, "entries", []) or []
                if isinstance(status, int) and status >= 400 and not entries:
                    feed_errors.append(f"{feed_url}: HTTP {status}")
                    continue
                if _g(parsed, "bozo", False) and not entries:
                    feed_errors.append(
                        f"{feed_url}: {_text(_g(parsed, 'bozo_exception', 'invalid feed'), 160)}"
                    )
                for entry in entries:
                    title = _text(_g(entry, "title", ""), 240)
                    summary = _text(_g(entry, "summary", "") or _g(entry, "description", ""), 700)
                    haystack = f"{title} {summary}".lower()
                    if not any(token in haystack for token in tokens):
                        continue
                    findings.append(ResearchFinding(
                        source="rss",
                        title=title or "Untitled RSS item",
                        summary=summary or "No summary supplied by the feed.",
                        url=_text(_g(entry, "link", ""), 500),
                        published_at=_text(
                            _g(entry, "published", "") or _g(entry, "updated", ""), 100
                        ),
                        retrieved_at=_now_iso(),
                        evidence_type="external_news",
                    ))
                    if len(findings) >= self.max_findings:
                        return findings
            if feed_errors and len(feed_errors) == len(urls):
                raise RuntimeError("; ".join(feed_errors))
            return findings

        try:
            return (
                await asyncio.wait_for(
                    asyncio.to_thread(parse),
                    timeout=self.source_timeout_seconds,
                ),
                None,
            )
        except Exception as exc:
            return [], _safe_error(exc)

    async def _search_exa(self, request: ResearchRequest) -> tuple[list[ResearchFinding], Optional[str]]:
        mcporter = shutil.which("mcporter")
        if not mcporter:
            return [], "mcporter executable is not installed"
        try:
            raw = await _run_process(
                [
                    mcporter,
                    "call",
                    "exa.web_search_exa",
                    f"query={request.query}",
                    f"numResults={self.max_findings}",
                ],
                timeout=self.source_timeout_seconds,
            )
            parsed: Any
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"text": raw}
            candidates = parsed.get("results", []) if isinstance(parsed, Mapping) else []
            findings: list[ResearchFinding] = []
            if isinstance(candidates, list):
                for item in candidates[:self.max_findings]:
                    if not isinstance(item, Mapping):
                        continue
                    findings.append(ResearchFinding(
                        source="exa_search",
                        title=_text(item.get("title", ""), 240) or "Untitled result",
                        summary=_text(item.get("text", "") or item.get("snippet", ""), 700),
                        url=_text(item.get("url", ""), 500),
                        published_at=_text(item.get("publishedDate", ""), 100),
                        retrieved_at=_now_iso(),
                        evidence_type="external_search",
                    ))
            if not findings and isinstance(parsed, Mapping) and parsed.get("text"):
                findings.append(ResearchFinding(
                    source="exa_search",
                    title="Exa search response",
                    summary=_text(parsed["text"], 700),
                    retrieved_at=_now_iso(),
                    evidence_type="external_search",
                ))
            return findings, None
        except Exception as exc:
            return [], _safe_error(exc)

    async def _search_youtube(self, request: ResearchRequest) -> tuple[list[ResearchFinding], Optional[str]]:
        try:
            raw = await _run_process(
                [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    f"ytsearch{self.max_findings}:{request.query}",
                    "--dump-single-json",
                    "--flat-playlist",
                    "--skip-download",
                    "--no-warnings",
                ],
                timeout=self.source_timeout_seconds,
            )
            parsed = json.loads(raw)
            entries = parsed.get("entries", []) if isinstance(parsed, Mapping) else []
            findings = [
                ResearchFinding(
                    source="youtube",
                    title=_text(entry.get("title", ""), 240) or "Untitled video",
                    summary=_text(entry.get("description", ""), 700),
                    url=_text(entry.get("webpage_url", "") or entry.get("url", ""), 500),
                    published_at=_text(entry.get("upload_date", ""), 100),
                    retrieved_at=_now_iso(),
                    evidence_type="external_video",
                )
                for entry in entries[:self.max_findings]
                if isinstance(entry, Mapping)
            ]
            return findings, None
        except Exception as exc:
            return [], _safe_error(exc)

    async def _read_web(self, request: ResearchRequest) -> tuple[list[ResearchFinding], Optional[str]]:
        if not self.web_urls:
            return [], "no URLs configured; Jina Reader reads URLs and does not provide search"
        curl = shutil.which("curl")
        if not curl:
            return [], "curl executable is not installed"
        findings: list[ResearchFinding] = []
        try:
            for url in self.web_urls[:self.max_findings]:
                target = f"https://r.jina.ai/{url}"
                raw = await _run_process(
                    [curl, "-L", "--fail", "--silent", "--show-error", "--max-time", str(int(self.source_timeout_seconds)), target],
                    timeout=self.source_timeout_seconds + 2,
                )
                findings.append(ResearchFinding(
                    source="web",
                    title=f"Jina Reader: {url}",
                    summary=_text(raw, 700),
                    url=url,
                    retrieved_at=_now_iso(),
                    evidence_type="external_web",
                ))
            return findings, None
        except Exception as exc:
            return findings, _safe_error(exc)

    async def research(self, request: ResearchRequest) -> ResearchResult:
        requested_at = _now_iso()
        report = await self.doctor()
        attempted: list[str] = []
        successful: list[str] = []
        findings: list[ResearchFinding] = []
        failures: dict[str, str] = {}
        handlers = {
            "rss": self._search_rss,
            "exa_search": self._search_exa,
            "youtube": self._search_youtube,
            "web": self._read_web,
        }

        for source in self.sources:
            attempted.append(source)
            if not self._backend_available(report, source):
                entry = report.get(source, {})
                message = _text(_g(entry, "message", "Agent Reach reports no active backend"), 300)
                failures[source] = message or "Agent Reach reports no active backend"
                continue
            try:
                source_findings, error = await asyncio.wait_for(
                    handlers[source](request),
                    timeout=self.source_timeout_seconds + 2,
                )
                if error:
                    failures[source] = error
                else:
                    successful.append(source)
                    findings.extend(source_findings)
            except Exception as exc:
                failures[source] = _safe_error(exc)
            if len(findings) >= self.max_findings:
                findings = findings[:self.max_findings]
                break

        if findings and failures:
            status = "partial"
        elif findings:
            status = "success"
        elif attempted and failures and len(failures) == len(attempted):
            status = "unavailable"
        elif attempted:
            status = "success"
        else:
            status = "unavailable"

        return ResearchResult(
            status=status,
            request=request,
            attempted_sources=tuple(attempted),
            successful_sources=tuple(successful),
            findings=tuple(findings[:self.max_findings]),
            failures=failures,
            requested_at=requested_at,
            completed_at=_now_iso(),
        )


class AgentReachResearchService:
    """Bounded cache/cooldown/concurrency layer for analyst research requests."""

    def __init__(
        self,
        adapter: Optional[AgentReachAdapter] = None,
        *,
        enabled: Optional[bool] = None,
        cache_ttl_seconds: Optional[float] = None,
        cooldown_seconds: Optional[float] = None,
        max_concurrency: Optional[int] = None,
    ) -> None:
        self.adapter = adapter or AgentReachAdapter()
        self.enabled = (
            enabled if enabled is not None
            else os.environ.get("AGENT_REACH_ENABLED", "true").lower() not in {"0", "false", "no", "off"}
        )
        self.cache_ttl_seconds = float(
            cache_ttl_seconds if cache_ttl_seconds is not None
            else os.environ.get("AGENT_REACH_CACHE_TTL_SECONDS", "1800")
        )
        self.cooldown_seconds = float(
            cooldown_seconds if cooldown_seconds is not None
            else os.environ.get("AGENT_REACH_COOLDOWN_SECONDS", "900")
        )
        self._semaphore = asyncio.Semaphore(max(
            1,
            int(max_concurrency if max_concurrency is not None
                else os.environ.get("AGENT_REACH_MAX_CONCURRENCY", "2")),
        ))
        self._cache: dict[str, tuple[float, ResearchResult]] = {}
        self._inflight: dict[str, asyncio.Task[ResearchResult]] = {}
        self._last_requested: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _disabled_result(self, request: ResearchRequest, reason: str) -> ResearchResult:
        now = _now_iso()
        return ResearchResult(
            status="skipped",
            request=request,
            requested_at=now,
            completed_at=now,
            skipped_reason=reason,
        )

    async def _perform(self, request: ResearchRequest) -> ResearchResult:
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    self.adapter.research(request),
                    timeout=self.adapter.timeout_seconds,
                )
            except Exception as exc:
                now = _now_iso()
                return ResearchResult(
                    status="failed",
                    request=request,
                    attempted_sources=tuple(self.adapter.sources),
                    failures={"runtime": _safe_error(exc)},
                    requested_at=now,
                    completed_at=now,
                )

    async def research(
        self,
        request: ResearchRequest,
        *,
        force: bool = False,
    ) -> ResearchResult:
        if not self.enabled:
            return self._disabled_result(request, "AGENT_REACH_ENABLED is false")
        key = request.canonical_key
        now_mono = time.monotonic()
        async with self._lock:
            # Join an existing request before applying cooldown.  Otherwise a
            # second caller arriving during the first request would be told it
            # was cooling down instead of receiving the same result.
            task = self._inflight.get(key)
            if task is not None:
                pass
            else:
                task = None
            cached = self._cache.get(key)
            if task is None and cached and not force and now_mono - cached[0] < self.cache_ttl_seconds:
                result = cached[1]
                return ResearchResult(
                    status=result.status,
                    request=result.request,
                    attempted_sources=result.attempted_sources,
                    successful_sources=result.successful_sources,
                    findings=result.findings,
                    failures=result.failures,
                    requested_at=result.requested_at,
                    completed_at=result.completed_at,
                    cached=True,
                    skipped_reason=result.skipped_reason,
                )
            if task is None and (
                not force
                and key in self._last_requested
                and now_mono - self._last_requested[key] < self.cooldown_seconds
            ):
                return self._disabled_result(request, "research cooldown active")
            if task is None:
                self._last_requested[key] = now_mono
                task = asyncio.create_task(self._perform(request))
                self._inflight[key] = task

        try:
            result = await task
            async with self._lock:
                self._cache[key] = (time.monotonic(), result)
            return result
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)


_service: Optional[AgentReachResearchService] = None


def get_agent_reach_research_service() -> AgentReachResearchService:
    global _service
    if _service is None:
        _service = AgentReachResearchService()
    return _service


async def research_candidate(
    candidate: Any,
    *,
    service: Optional[AgentReachResearchService] = None,
    force: bool = False,
) -> Any:
    """
    Research a Candidate and return a new Candidate with external evidence.

    Candidate's quantitative fields are never modified.  A duck-typed
    ``with_external_research`` method is used to avoid a circular import.
    """
    request = build_research_request(candidate)
    result = await (service or get_agent_reach_research_service()).research(request, force=force)
    if hasattr(candidate, "with_external_research"):
        return candidate.with_external_research(result)
    return candidate


def research_status_summary(result: ResearchResult) -> str:
    """Short, user-safe status line for /analyst and diagnostics."""
    if result.status == "success":
        return f"External research: {len(result.findings)} finding(s) from {', '.join(result.successful_sources)}."
    if result.status == "partial":
        failed = ", ".join(result.failures) or "some sources"
        return f"External research: partial ({len(result.findings)} finding(s)); unavailable: {failed}."
    if result.status == "skipped":
        return f"External research skipped: {result.skipped_reason}."
    if result.status == "unavailable":
        return "External research unavailable: no configured Agent Reach source completed."
    return "External research failed: no external evidence was added."


__all__ = [
    "AgentReachAdapter",
    "AgentReachResearchService",
    "NormalizedPropSource",
    "ResearchFinding",
    "ResearchRequest",
    "ResearchResult",
    "build_historical_market_evidence",
    "build_research_request",
    "get_agent_reach_research_service",
    "normalize_prop_source",
    "research_candidate",
    "research_status_summary",
]