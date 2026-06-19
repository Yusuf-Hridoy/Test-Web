"""Bounded page snapshot for grounding the LLM.

A compact, deterministic list of interactive / labelled elements (role, name, id,
text). Version-independent — it does not rely on Playwright's accessibility API,
which is not present in all builds.
"""

from __future__ import annotations

import json

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

MAX_SNAPSHOT_CHARS = 12_000

_SNAPSHOT_JS = """
() => {
  const out = [];
  const sel = 'a,button,input,select,textarea,label,[role],[aria-label],h1,h2,h3';
  for (const el of document.querySelectorAll(sel)) {
    const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
                  el.getAttribute('placeholder') || '').trim().slice(0, 80);
    out.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '',
      id: el.id || '',
      name: el.getAttribute('name') || '',
      classes: (typeof el.className === 'string') ? el.className : '',
      text,
    });
    if (out.length >= 200) break;
  }
  return { url: location.href, title: document.title, elements: out };
}
"""


async def page_snapshot(page: Page) -> str:
    """Return a bounded JSON snapshot of the page's salient elements."""
    try:
        data = await page.evaluate(_SNAPSHOT_JS)
    except PlaywrightError:
        data = None
    return (json.dumps(data, ensure_ascii=False) if data else "")[:MAX_SNAPSHOT_CHARS]
