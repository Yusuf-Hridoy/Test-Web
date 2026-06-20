"""Alerting: Slack / email / generic webhook.

Notify only on things that need action: a NEW critical finding, a regression (a
finding newly appearing vs the previous run), or a functional case failing. An
*unchanged* known issue never alerts — alert fatigue kills the product.
"""

from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import models


@dataclass
class AlertContext:
    suite_name: str
    run_id: int
    kind: str  # "scan" | "functional"
    status: str
    new_findings: list[dict] = field(default_factory=list)
    new_critical: list[dict] = field(default_factory=list)
    failed_cases: list[str] = field(default_factory=list)
    dashboard_url: str = "http://localhost:8501"


def dashboard_base() -> str:
    return os.getenv("QASCAN_DASHBOARD_URL", "http://localhost:8501").rstrip("/")


def build_context(suite_name: str, run: models.Run, diff: dict, *,
                  kind: str, failed_cases: list[str]) -> AlertContext:
    new = diff.get("new", [])
    return AlertContext(
        suite_name=suite_name,
        run_id=run.id,
        kind=kind,
        status=run.status,
        new_findings=new,
        new_critical=[f for f in new if f.get("severity") == "critical"],
        failed_cases=failed_cases,
        dashboard_url=dashboard_base(),
    )


def should_notify(config: models.NotifyConfig, ctx: AlertContext) -> bool:
    """Pure decision: does this run match this channel's configured events?"""
    if config.on_critical and ctx.new_critical:
        return True
    if config.on_regression and ctx.new_findings:
        return True
    if config.on_failure and ctx.failed_cases:
        return True
    return False


def build_message(ctx: AlertContext) -> str:
    lines = [f"🔴 qascan: {ctx.suite_name} — run #{ctx.run_id} ({ctx.status})"]
    if ctx.new_critical:
        lines.append(f"{len(ctx.new_critical)} NEW critical finding(s):")
        lines += [f"  • {f['title']} — {f['page_url']}" for f in ctx.new_critical[:5]]
    elif ctx.new_findings:
        lines.append(f"{len(ctx.new_findings)} new finding(s) since last run:")
        lines += [f"  • [{f['severity']}] {f['title']}" for f in ctx.new_findings[:5]]
    if ctx.failed_cases:
        lines.append(f"{len(ctx.failed_cases)} failing case(s): "
                     + ", ".join(ctx.failed_cases[:5]))
    lines.append(f"Details: {ctx.dashboard_url}  (run #{ctx.run_id})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #
def _send_slack(target: str, message: str) -> None:
    httpx.post(target, json={"text": message}, timeout=10.0).raise_for_status()


def _send_webhook(target: str, ctx: AlertContext, message: str) -> None:
    payload = {
        "suite": ctx.suite_name, "run_id": ctx.run_id, "status": ctx.status,
        "new_findings": ctx.new_findings, "failed_cases": ctx.failed_cases,
        "message": message,
    }
    httpx.post(target, json=payload, timeout=10.0).raise_for_status()


def _send_email(target: str, message: str) -> None:
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST not set; cannot send email alert.")
    msg = EmailMessage()
    msg["Subject"] = "qascan alert"
    msg["From"] = os.getenv("SMTP_FROM", "qascan@localhost")
    msg["To"] = target
    msg.set_content(message)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
        if os.getenv("SMTP_USER"):
            s.starttls()
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        s.send_message(msg)


def dispatch(config: models.NotifyConfig, ctx: AlertContext, message: str) -> None:
    """Send one alert via its channel. Raises on transport failure."""
    if config.channel == "slack":
        _send_slack(config.target, message)
    elif config.channel == "webhook":
        _send_webhook(config.target, ctx, message)
    elif config.channel == "email":
        _send_email(config.target, message)
    else:
        raise ValueError(f"Unknown notify channel: {config.channel!r}")


def notify_for_run(session: Session, suite_id: int, ctx: AlertContext, *,
                   sender=dispatch) -> list[dict]:
    """Evaluate every enabled NotifyConfig for the suite; dispatch matches.

    Returns a list of {channel, target} actually notified. ``sender`` is injectable
    for testing. A channel that errors is recorded but never aborts the others.
    """
    configs = session.scalars(
        select(models.NotifyConfig).where(
            models.NotifyConfig.suite_id == suite_id,
            models.NotifyConfig.enabled.is_(True),
        )
    ).all()
    message = build_message(ctx)
    sent: list[dict] = []
    for config in configs:
        if not should_notify(config, ctx):
            continue
        try:
            sender(config, ctx, message)
            sent.append({"channel": config.channel, "target": config.target})
        except Exception as exc:  # noqa: BLE001 — one bad channel must not block others
            sent.append({"channel": config.channel, "error": str(exc)})
    return sent


def to_json(obj) -> str:
    return json.dumps(obj, default=str)
