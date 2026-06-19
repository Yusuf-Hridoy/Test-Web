"""Settings and run limits.

Env is read once (via ``python-dotenv``) into a pydantic ``Settings`` model.
``RunLimits`` is the plain, immutable bundle handed to the crawler so the crawl
path never reaches back into global config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# CLAUDE.md defaults.
DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_DEPTH = 3
DEFAULT_TIME_BUDGET_SECONDS = 300


class Settings(BaseModel):
    """Process-wide configuration sourced from the environment."""

    max_pages: int = Field(default=DEFAULT_MAX_PAGES, ge=1)
    max_depth: int = Field(default=DEFAULT_MAX_DEPTH, ge=0)
    time_budget_seconds: float = Field(default=DEFAULT_TIME_BUDGET_SECONDS, gt=0)

    @classmethod
    def from_env(cls) -> Settings:
        """Load ``.env`` (if present) and build settings from ``QASCAN_*`` vars."""
        load_dotenv()
        return cls(
            max_pages=int(os.getenv("QASCAN_MAX_PAGES", DEFAULT_MAX_PAGES)),
            max_depth=int(os.getenv("QASCAN_MAX_DEPTH", DEFAULT_MAX_DEPTH)),
            time_budget_seconds=float(
                os.getenv("QASCAN_TIME_BUDGET_SECONDS", DEFAULT_TIME_BUDGET_SECONDS)
            ),
        )

    def to_limits(self) -> RunLimits:
        """Project these settings into the immutable crawler limits."""
        return RunLimits(
            max_pages=self.max_pages,
            max_depth=self.max_depth,
            time_budget_seconds=self.time_budget_seconds,
        )


@dataclass(frozen=True)
class RunLimits:
    """Hard bounds for a single scan. Nothing in the crawl path is unbounded."""

    max_pages: int = DEFAULT_MAX_PAGES
    max_depth: int = DEFAULT_MAX_DEPTH
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS
    # Per-navigation and per-request ceilings (milliseconds / seconds).
    nav_timeout_ms: int = 15_000
    link_timeout_seconds: float = 10.0
    link_concurrency: int = 10
    polite_delay_seconds: float = 0.2
