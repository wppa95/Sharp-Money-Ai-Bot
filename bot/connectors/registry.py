"""
connectors/registry.py — ConnectorRegistry: manages enabled connectors.

The registry holds the set of active BaseConnector instances. It is
populated at startup from config and provides a single entry-point
to fetch all market snapshots in one call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .base import BaseConnector, ConnectorStatus, MarketSnapshot

logger = logging.getLogger(__name__)


class ConnectorRegistry:
    """
    Manages a collection of BaseConnector instances.

    Usage::

        registry = ConnectorRegistry()
        registry.register(DraftKingsConnector(...))
        registry.register(FanDuelConnector(...))

        snapshots = await registry.fetch_all()   # runs connectors in parallel
    """

    def __init__(self) -> None:
        self._connectors: list[BaseConnector] = []

    def register(self, connector: BaseConnector) -> None:
        """Add a connector. Disabled connectors are registered but skipped."""
        self._connectors.append(connector)
        logger.info(
            "Connector registered: %s (enabled=%s, pickem=%s)",
            connector.name, connector.enabled, connector.is_pickem,
        )

    @property
    def connectors(self) -> list[BaseConnector]:
        return list(self._connectors)

    @property
    def enabled_connectors(self) -> list[BaseConnector]:
        return [c for c in self._connectors if c.enabled]

    @property
    def sportsbook_connectors(self) -> list[BaseConnector]:
        """Return enabled connectors for sportsbook markets (not pick'em)."""
        return [c for c in self.enabled_connectors if not c.is_pickem]

    @property
    def pickem_connectors(self) -> list[BaseConnector]:
        """Return enabled connectors for pick'em platforms."""
        return [c for c in self.enabled_connectors if c.is_pickem]

    async def fetch_all(self) -> list[MarketSnapshot]:
        """
        Fetch from all enabled connectors concurrently.
        Failures in individual connectors are logged and skipped.
        Returns the combined list of all snapshots.
        """
        if not self.enabled_connectors:
            return []

        tasks = {c.name: asyncio.create_task(c.fetch()) for c in self.enabled_connectors}
        results: list[MarketSnapshot] = []

        for name, task in tasks.items():
            try:
                snapshots = await task
                results.extend(snapshots)
                logger.debug("Connector %s returned %d snapshots", name, len(snapshots))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Connector %s fetch error: %s", name, exc)

        logger.info(
            "fetch_all: %d connectors → %d total snapshots",
            len(tasks), len(results),
        )
        return results

    async def fetch_sportsbook(self) -> list[MarketSnapshot]:
        """Fetch only sportsbook connectors (excludes pick'em)."""
        connectors = self.sportsbook_connectors
        if not connectors:
            return []
        tasks = {c.name: asyncio.create_task(c.fetch()) for c in connectors}
        results: list[MarketSnapshot] = []
        for name, task in tasks.items():
            try:
                results.extend(await task)
            except Exception as exc:
                logger.warning("Connector %s fetch error: %s", name, exc)
        return results

    async def fetch_pickem(self) -> list[MarketSnapshot]:
        """Fetch only pick'em connectors (Underdog, PrizePicks)."""
        connectors = self.pickem_connectors
        if not connectors:
            return []
        tasks = {c.name: asyncio.create_task(c.fetch()) for c in connectors}
        results: list[MarketSnapshot] = []
        for name, task in tasks.items():
            try:
                results.extend(await task)
            except Exception as exc:
                logger.warning("Connector %s fetch error: %s", name, exc)
        return results

    async def health_check_all(self) -> dict[str, ConnectorStatus]:
        """Run health checks on all registered connectors."""
        results: dict[str, ConnectorStatus] = {}
        for connector in self._connectors:
            if not connector.enabled:
                results[connector.name] = ConnectorStatus.DISABLED
                continue
            try:
                results[connector.name] = await connector.health_check()
            except Exception as exc:
                logger.warning("Health check failed for %s: %s", connector.name, exc)
                results[connector.name] = ConnectorStatus.ERROR
        return results
