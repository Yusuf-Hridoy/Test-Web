"""qascan UI design system — tokens, injected CSS, and reusable HTML helpers.

One styling module for the whole app: every screen imports from here, no screen
writes raw CSS. Tokens are CSS custom properties so they change in one place.

ACCENT DECISION: indigo (--accent #6366F1) is the primary-action / active-nav color.
Red is reserved exclusively for the *fail* severity, so brand and status never
collide. To rebrand to red, change --accent here only (keep --fail separate).

Security: all dynamic strings (URLs, selectors, finding text from scanned sites)
are HTML-escaped before being placed in injected markup.
"""

from __future__ import annotations

import html

import streamlit as st

# --------------------------------------------------------------------------- #
# Global CSS (tokens + font load + Streamlit component styling)
# --------------------------------------------------------------------------- #
_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --bg:#0B0D12; --surface:#14171D; --surface-2:#1B1F27;
  --border:#262B36; --border-strong:#333A47;
  --text:#E7E9EE; --text-muted:#9AA1AE; --text-faint:#828A98;
  --accent:#6366F1; --accent-hover:#7C7FF2;
  --pass:#3FB950; --fail:#E5484D; --review:#D9A406; --info:#3B82F6; --neutral:#828A98;
  --pass-bg:rgba(63,185,80,0.14); --fail-bg:rgba(229,72,77,0.14);
  --review-bg:rgba(217,164,6,0.16); --info-bg:rgba(59,130,246,0.14);
  --neutral-bg:rgba(130,138,152,0.14); --accent-bg:rgba(99,102,241,0.14);
  --r-card:10px; --r-pill:6px;
  --font-body:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --font-display:'Space Grotesk',var(--font-body);
  --font-mono:'JetBrains Mono','SFMono-Regular',Menlo,monospace;
}

.stApp { background:var(--bg); color:var(--text); font-family:var(--font-body); }
.block-container { max-width:1040px; padding-top:2.2rem; }
h1,h2,h3,h4 { font-family:var(--font-display); letter-spacing:-0.02em; color:var(--text); }
[data-testid="stMarkdownContainer"] p { color:var(--text); }
code, .qs-mono { font-family:var(--font-mono); }

/* Sidebar */
[data-testid="stSidebar"] { background:var(--surface); border-right:1px solid var(--border); }
[data-testid="stSidebar"] .block-container { padding-top:1.4rem; }
[data-testid="stSidebarNav"] a { border-radius:8px; }

/* Buttons */
[data-testid="stBaseButton-primary"] {
  background:var(--accent); border:1px solid var(--accent); color:#fff; font-weight:600;
  border-radius:8px; transition:background .14s ease, border-color .14s ease;
}
[data-testid="stBaseButton-primary"]:hover { background:var(--accent-hover); border-color:var(--accent-hover); }
[data-testid="stBaseButton-secondary"] {
  background:var(--surface-2); border:1px solid var(--border); color:var(--text);
  border-radius:8px; transition:border-color .14s ease, background .14s ease;
}
[data-testid="stBaseButton-secondary"]:hover { border-color:var(--border-strong); background:var(--surface-2); }

/* Inputs */
[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea,
[data-testid="stNumberInput"] input {
  background:var(--surface); color:var(--text); border:1px solid var(--border);
  border-radius:8px; font-family:var(--font-body);
}
[data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus,
[data-testid="stNumberInput"] input:focus {
  border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-bg);
}
::placeholder { color:var(--text-faint); }

/* Containers / expanders / metrics */
[data-testid="stExpander"] { border:1px solid var(--border); border-radius:var(--r-card); background:var(--surface); }
[data-testid="stExpander"] summary:hover { color:var(--accent-hover); }
[data-testid="stMetric"] {
  background:var(--surface); border:1px solid var(--border); border-radius:var(--r-card);
  padding:14px 16px;
}
[data-testid="stMetricValue"] { font-family:var(--font-display); }
div[data-testid="stVerticalBlockBorderWrapper"] {
  border-radius:var(--r-card);
}

/* Keyboard accessibility — never remove focus outlines */
a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible,
[role="button"]:focus-visible, summary:focus-visible {
  outline:2px solid var(--accent-hover); outline-offset:2px;
}

/* qascan component classes */
.qs-badge {
  display:inline-flex; align-items:center; gap:6px; padding:2px 10px; border-radius:var(--r-pill);
  font-size:12px; font-weight:600; font-family:var(--font-body); line-height:1.7;
  border:1px solid transparent; white-space:nowrap;
}
.qs-badge .dot { width:7px; height:7px; border-radius:50%; }
.qs-mono-chip {
  font-family:var(--font-mono); font-size:12.5px; color:var(--text-muted);
  background:var(--surface-2); border:1px solid var(--border); border-radius:var(--r-pill);
  padding:1px 7px; word-break:break-all;
}
.qs-card {
  background:var(--surface); border:1px solid var(--border); border-radius:var(--r-card);
  padding:16px 18px; transition:border-color .14s ease;
}
.qs-card:hover { border-color:var(--border-strong); }
.qs-section-title { font-family:var(--font-display); font-size:26px; font-weight:600; margin:0; }
.qs-section-sub { color:var(--text-muted); font-size:14px; margin:4px 0 18px; }
.qs-step { display:flex; align-items:center; gap:10px; padding:7px 0; border-top:1px solid var(--border); }
.qs-step:first-child { border-top:none; }
.qs-step .num { color:var(--text-faint); font-family:var(--font-mono); font-size:12px; width:20px; }
.qs-action {
  font-family:var(--font-mono); font-size:11px; font-weight:500; letter-spacing:.04em;
  padding:2px 7px; border-radius:5px; text-transform:uppercase;
  background:var(--accent-bg); color:var(--accent-hover); border:1px solid var(--border);
}
.qs-finding-title { font-family:var(--font-display); font-weight:600; font-size:15px; color:var(--text); }
.qs-finding-detail { color:var(--text-muted); font-size:13.5px; margin:4px 0 8px; }
.qs-hint { color:var(--text-faint); font-size:13px; }

@media (prefers-reduced-motion: reduce) {
  * { transition:none !important; animation:none !important; }
}
</style>
"""

_SEVERITY = {
    "pass": ("Pass", "var(--pass)", "var(--pass-bg)"),
    "healthy": ("Healthy", "var(--pass)", "var(--pass-bg)"),
    "fail": ("Fail", "var(--fail)", "var(--fail-bg)"),
    "critical": ("Critical", "var(--fail)", "var(--fail-bg)"),
    "review": ("Needs review", "var(--review)", "var(--review-bg)"),
    "needs_review": ("Needs review", "var(--review)", "var(--review-bg)"),
    "warning": ("Warning", "var(--review)", "var(--review-bg)"),
    "info": ("Info", "var(--info)", "var(--info-bg)"),
    "minor": ("Minor", "var(--info)", "var(--info-bg)"),
    "neutral": ("", "var(--neutral)", "var(--neutral-bg)"),
    "accent": ("", "var(--accent-hover)", "var(--accent-bg)"),
}

_ACTION_LABEL = {"goto": "GO", "click": "CLICK", "fill": "FILL", "select": "SEL",
                 "wait": "WAIT", "expect": "EXPECT", "verify_nl": "VERIFY"}


def inject() -> None:
    """Inject global CSS once per session run (idempotent)."""
    st.markdown(_CSS, unsafe_allow_html=True)


# Streamlit's sidebar <section> carries aria-expanded, which axe (correctly) flags
# as aria-allowed-attr — a framework quirk we can't reach from st.markdown (it
# strips <script>). A zero-height, same-origin components iframe can reach the
# parent DOM and strip the spurious attribute, keeping our own UI axe-clean.
_A11Y_JS = """
<script>
const fix = () => {
  try {
    const doc = window.parent.document;
    doc.querySelectorAll('[data-testid="stSidebar"][aria-expanded]')
       .forEach(el => el.removeAttribute('aria-expanded'));
  } catch (e) {}
};
fix();
new MutationObserver(fix).observe(window.parent.document.documentElement,
  {attributes: true, subtree: true, attributeFilter: ['aria-expanded']});
</script>
"""


def a11y_fixups() -> None:
    """Strip a framework-level invalid ARIA attribute so the app passes its own
    accessibility check (UI-layer only; no engine/markup of ours depends on it)."""
    import streamlit.components.v1 as components

    components.html(_A11Y_JS, height=0)


def badge(label: str, kind: str = "neutral") -> str:
    """A pill that pairs a colored dot WITH a label. Text is high-contrast light
    (AA on the tinted background); the dot + tint + border carry the color meaning,
    so it never relies on text color alone."""
    _, color, bg = _SEVERITY.get(kind, _SEVERITY["neutral"])
    text = html.escape(label)
    return (f'<span class="qs-badge" style="background:{bg};color:var(--text);'
            f'border-color:{color}59"><span class="dot" style="background:{color}"></span>'
            f"{text}</span>")


def mono(text: str) -> str:
    """Inline monospace chip for URLs / selectors / ids."""
    return f'<span class="qs-mono-chip">{html.escape(str(text))}</span>'


def section_header(title: str, subtitle: str = "") -> None:
    sub = f'<div class="qs-section-sub">{html.escape(subtitle)}</div>' if subtitle else ""
    st.markdown(f'<div class="qs-section-title">{html.escape(title)}</div>{sub}',
                unsafe_allow_html=True)


def metric_card(label: str, value: str, kind: str | None = None) -> str:
    """Status-aware metric: the value color carries meaning when kind is given."""
    color = _SEVERITY.get(kind, (None, "var(--text)", None))[1] if kind else "var(--text)"
    return (f'<div class="qs-card" style="text-align:left">'
            f'<div style="color:var(--text-faint);font-size:12px;text-transform:uppercase;'
            f'letter-spacing:.05em">{html.escape(label)}</div>'
            f'<div style="font-family:var(--font-display);font-size:26px;font-weight:600;'
            f'color:{color};margin-top:2px">{html.escape(str(value))}</div></div>')


def step_row(step: dict, index: int) -> str:
    """Readable rendering of one functional step — never raw JSON."""
    action = str(step.get("action", "")).lower()
    label = _ACTION_LABEL.get(action, action.upper() or "STEP")
    target = step.get("selector") or step.get("target") or ""
    value = step.get("value")
    assert_ = step.get("assert") or step.get("assert_")
    bits = [f'<span class="qs-action">{html.escape(label)}</span>']
    if target:
        bits.append(mono(target))
    if assert_:
        bits.append(f'<span class="qs-hint">expect {html.escape(str(assert_))}</span>')
    if value:
        bits.append(f'<span class="qs-hint">= {html.escape(str(value))}</span>')
    if action == "verify_nl":
        bits.append(badge("needs review", "review"))
    inner = " ".join(bits)
    return (f'<div class="qs-step"><span class="num">{index}</span>'
            f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">{inner}</div></div>')


def finding_card(severity: str, title: str, detail: str, page_url: str,
                 tags: list[str] | None = None) -> str:
    """Severity-badged finding card (HTML-escaped)."""
    tag_html = " ".join(tags or [])
    return (f'<div class="qs-card" style="margin-bottom:10px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
            f'{badge(severity, severity)}{tag_html}</div>'
            f'<div class="qs-finding-title">{html.escape(title)}</div>'
            f'<div class="qs-finding-detail">{html.escape(detail)}</div>'
            f'{mono(page_url)}</div>')


def heal_diff(old: str, new: str) -> str:
    """Old vs new selector, diff-styled (old=fail tint, new=pass tint)."""
    return (
        f'<div style="display:flex;flex-direction:column;gap:6px;margin:6px 0">'
        f'<div><span class="qs-hint">was</span> <span class="qs-mono-chip" '
        f'style="color:var(--fail);background:var(--fail-bg);text-decoration:line-through">'
        f'{html.escape(old) or "—"}</span></div>'
        f'<div><span class="qs-hint">now</span> <span class="qs-mono-chip" '
        f'style="color:var(--pass);background:var(--pass-bg)">{html.escape(new)}</span></div></div>')
