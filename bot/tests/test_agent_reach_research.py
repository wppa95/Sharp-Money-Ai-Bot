"""Contract tests for the bounded Agent Reach analyst research layer."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from engine.agent_reach_research import (
    AgentReachAdapter,
    AgentReachResearchService,
    ResearchFinding,
    ResearchRequest,
    ResearchResult,
    build_historical_market_evidence,
    build_research_request,
    normalize_prop_source,
    research_candidate,
)
from engine.analyst import build_analyst_narrative
from engine.candidate import Candidate, ConfidenceDimensions, candidate_from_ud_decision
from engine.explanation import ExplanationFormat, get_explanation_service


def _candidate():
    decision = SimpleNamespace(
        confidence=82,
        decision_tier="A",
        recommendation="OVER",
        reason="Stored quantitative signal.",
        hit_rates={},
        window_agreement=0,
    )
    score = SimpleNamespace(total=78, n_history=20)
    return candidate_from_ud_decision(
        player_name="Jalen Brunson",
        sport="NBA",
        stat_type="Points",
        line=24.5,
        decision=decision,
        score=score,
        game_time=datetime(2026, 8, 27, 0, 0),
    )


class StubAdapter:
    sources = ("rss", "exa_search")
    timeout_seconds = 1

    def __init__(self, result=None, delay=0):
        self.result = result
        self.delay = delay
        self.calls = 0

    async def research(self, request):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result or ResearchResult(
            status="success",
            request=request,
            attempted_sources=("rss",),
            successful_sources=("rss",),
            findings=(
                ResearchFinding(
                    source="rss",
                    title="Brunson listed for Thursday",
                    summary="The stored source reports expected availability.",
                    url="https://example.test/news",
                    retrieved_at="2026-08-26T20:00:00Z",
                ),
            ),
        )


def test_normalize_prop_source_shares_canonical_identity_across_field_shapes():
    underdog = normalize_prop_source({
        "player_name": "Jalen Brunson",
        "sport": "NBA",
        "stat_type": "Points",
        "line_value": 24.5,
        "game_id": "game-1",
        "external_id": "ud-1",
    }, provider="Underdog")
    sportsbook = normalize_prop_source({
        "player": "Jalen Brunson",
        "sport": "nba",
        "market_type": "Points",
        "line": 24.5,
        "game_id": "game-1",
    }, provider="DraftKings")

    assert underdog.canonical_key == sportsbook.canonical_key
    assert underdog.player_name == sportsbook.player_name
    assert underdog.stat_type.lower() == sportsbook.stat_type.lower()
    assert underdog.line == sportsbook.line


def test_build_request_is_specific_and_does_not_recalculate_candidate():
    candidate = _candidate()
    request = build_research_request(candidate)
    assert request.player_name == "Jalen Brunson"
    assert "NBA" in request.query
    assert "Points" in request.query
    assert "line 24.5" in request.query
    assert "injury" in request.query


def test_historical_projection_uses_only_stored_trace_and_marks_missing_data():
    candidate = replace(
        _candidate(),
        decision_trace={
            "prop_intelligence": {
                "historical": {
                    "n": 20,
                    "sample_strength": 72,
                    "hit_rate": 0.65,
                    "avg_vs_line": 1.2,
                }
            }
        },
    )
    evidence = build_historical_market_evidence(candidate)
    assert evidence["available"] is True
    assert evidence["items"][0]["source"] == "stored_prop_intelligence"
    assert evidence["items"][0]["n"] == 20
    assert build_historical_market_evidence(_candidate())["available"] is False


@pytest.mark.asyncio
async def test_research_success_is_stored_separately_and_quantitative_fields_are_invariant():
    candidate = _candidate()
    before = (candidate.confidence, candidate.decision, candidate.tier, candidate.line)
    enriched = await research_candidate(
        candidate,
        service=AgentReachResearchService(
            adapter=StubAdapter(),
            cache_ttl_seconds=60,
            cooldown_seconds=60,
        ),
    )

    assert "external_research" not in candidate.decision_trace
    research = enriched.decision_trace["external_research"]
    assert research["status"] == "success"
    assert research["successful_sources"] == ["rss"]
    assert research["findings"][0]["title"] == "Brunson listed for Thursday"
    assert (enriched.confidence, enriched.decision, enriched.tier, enriched.line) == before


@pytest.mark.asyncio
async def test_partial_failure_is_explicit_and_never_fabricates_a_finding():
    request = ResearchRequest("Jalen Brunson", "NBA", "Points", 24.5, "OVER")
    result = ResearchResult(
        status="partial",
        request=request,
        attempted_sources=("rss", "exa_search"),
        successful_sources=("rss",),
        failures={"exa_search": "Agent Reach reports no active backend"},
    )
    candidate = _candidate()
    enriched = await research_candidate(
        candidate,
        service=AgentReachResearchService(adapter=StubAdapter(result=result)),
    )

    trace = enriched.decision_trace["external_research"]
    assert trace["status"] == "partial"
    assert trace["failures"]["exa_search"]
    assert trace["findings"] == []
    narrative = build_analyst_narrative(enriched)
    assert "external" in narrative.risk_because.lower() or "external" in narrative.recommended_because.lower()


@pytest.mark.asyncio
async def test_total_failure_and_disabled_service_are_not_silent():
    candidate = _candidate()
    failed = ResearchResult(
        status="unavailable",
        request=build_research_request(candidate),
        attempted_sources=("rss",),
        failures={"rss": "feed unavailable"},
    )
    enriched = await research_candidate(
        candidate,
        service=AgentReachResearchService(adapter=StubAdapter(result=failed)),
    )
    assert enriched.decision_trace["external_research"]["status"] == "unavailable"
    assert "feed unavailable" in enriched.decision_trace["external_research"]["failures"]["rss"]

    disabled = await research_candidate(
        candidate,
        service=AgentReachResearchService(adapter=StubAdapter(), enabled=False),
    )
    assert disabled.decision_trace["external_research"]["status"] == "skipped"
    assert disabled.decision_trace["external_research"]["skipped_reason"]


@pytest.mark.asyncio
async def test_service_deduplicates_inflight_requests_and_uses_cache():
    adapter = StubAdapter(delay=0.01)
    service = AgentReachResearchService(
        adapter=adapter,
        cache_ttl_seconds=60,
        cooldown_seconds=60,
        max_concurrency=1,
    )
    request = build_research_request(_candidate())
    first, second = await asyncio.gather(service.research(request), service.research(request))
    third = await service.research(request)

    assert adapter.calls == 1
    assert first.findings == second.findings == third.findings
    assert third.cached is True


@pytest.mark.asyncio
async def test_adapter_marks_unconfigured_source_without_substitution(monkeypatch):
    adapter = AgentReachAdapter(sources=("web",), web_urls=())
    adapter._doctor_cache = (float("inf"), {"web": {"active_backend": "Jina Reader"}})
    result = await adapter.research(build_research_request(_candidate()))

    assert result.status == "unavailable"
    assert result.findings == ()
    assert "web" in result.failures
    assert "does not provide search" in result.failures["web"]


def test_analyst_and_explanation_distinguish_external_evidence():
    candidate = _candidate().with_external_research(
        ResearchResult(
            status="success",
            request=build_research_request(_candidate()),
            attempted_sources=("rss",),
            successful_sources=("rss",),
            findings=(
                ResearchFinding(
                    source="rss",
                    title="Availability update",
                    summary="Player expected to play.",
                ),
            ),
        )
    )
    narrative = build_analyst_narrative(candidate)
    assert "External evidence" in narrative.recommended_because
    rendered = get_explanation_service().render(candidate, ExplanationFormat.TELEGRAM)
    assert "External research" in rendered
    assert "Availability update" in rendered