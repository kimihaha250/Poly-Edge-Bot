from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "positions.json"


class PositionStore:
    """
    Thread-safe* persistent store for open positions.

    Backed by a JSON file written atomically (write-then-rename).
    On startup the file is loaded automatically so positions survive
    process restarts.

    *Single-writer only — sufficient for a single-threaded bot.
    """

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._positions: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._positions = data
                if data:
                    logger.info(
                        "Restored %d open position(s) from %s: %s",
                        len(data),
                        self._path,
                        list(data.keys()),
                    )
        except Exception as exc:
            logger.warning("Could not load %s (%s) — starting with empty positions", self._path, exc)

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._positions, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            logger.error("Failed to persist positions to %s: %s", self._path, exc)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, slug: str, data: dict[str, Any]) -> None:
        self._positions[slug] = data
        self._save()
        logger.debug("Position added: %s", slug)

    def remove(self, slug: str) -> dict[str, Any] | None:
        pos = self._positions.pop(slug, None)
        if pos is not None:
            self._save()
            logger.debug("Position removed: %s", slug)
        return pos

    def update(self, slug: str, patch: dict[str, Any]) -> None:
        if slug in self._positions:
            self._positions[slug].update(patch)
            self._save()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, slug: str) -> dict[str, Any] | None:
        return self._positions.get(slug)

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._positions)

    def slugs(self) -> list[str]:
        return list(self._positions.keys())

    def total_exposure_usdc(self) -> float:
        return sum(float(p.get("size_usdc", 0)) for p in self._positions.values())

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __contains__(self, slug: str) -> bool:
        return slug in self._positions

    def __len__(self) -> int:
        return len(self._positions)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._positions))
