"""Health server port selection stays safe in shared Replit workflows."""

import pytest

from main import _health_server_port


def test_local_default_avoids_api_server_port(monkeypatch):
    monkeypatch.delenv("BOT_HEALTH_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    assert _health_server_port() == (8099, "local default")


def test_workflow_override_wins_over_platform_port(monkeypatch):
    monkeypatch.setenv("BOT_HEALTH_PORT", "8081")
    monkeypatch.setenv("PORT", "8080")

    assert _health_server_port() == (8081, "BOT_HEALTH_PORT")


def test_deployment_port_is_preserved_when_no_bot_override(monkeypatch):
    monkeypatch.delenv("BOT_HEALTH_PORT", raising=False)
    monkeypatch.setenv("PORT", "9000")

    assert _health_server_port() == (9000, "PORT")


def test_invalid_health_port_is_rejected(monkeypatch):
    monkeypatch.setenv("BOT_HEALTH_PORT", "not-a-port")

    with pytest.raises(ValueError):
        _health_server_port()


def test_non_positive_health_port_is_rejected(monkeypatch):
    monkeypatch.setenv("BOT_HEALTH_PORT", "0")

    with pytest.raises(ValueError):
        _health_server_port()