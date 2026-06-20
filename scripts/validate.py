"""Real-world validation harness (Phase 5, Task 1).

Scans a spread of public sites and prints finding-type counts per site, so false
positives can be triaged by hand. See docs/validation-log.md for the recorded pass.

    python scripts/validate.py
"""

from __future__ import annotations

import asyncio
import collections

from qascan.config import RunLimits
from qascan.crawler import crawl

SITES = [
    "https://example.com",
    "https://www.iana.org",
    "https://books.toscrape.com",
    "https://quotes.toscrape.com",
    "https://quotes.toscrape.com/js/",
    "https://www.saucedemo.com",
    "https://news.ycombinator.com",
    "https://www.wikipedia.org",
]


async def main() -> None:
    limits = RunLimits(max_pages=5, max_depth=1, time_budget_seconds=60)
    agg: collections.Counter = collections.Counter()
    for url in SITES:
        try:
            r = await crawl(url, limits)
            types = collections.Counter(f.type for f in r.findings)
            agg.update(types)
            print(f"{url:42} pages={r.pages_scanned} stop={r.stopped_reason} {dict(types)}")
        except Exception as exc:  # noqa: BLE001
            print(f"{url:42} ERROR {type(exc).__name__}: {exc}")
    print("\nAGGREGATE:", dict(agg))


if __name__ == "__main__":
    asyncio.run(main())
