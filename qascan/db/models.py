"""SQLAlchemy 2.x models.

Single-user for now; ``user_id`` columns arrive in Phase 5. ``steps`` is JSONB so
generated/written test cases round-trip without a side table. ``findings.finding_key``
is the stable Phase-1 hash used for run-over-run diffing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    base_url: Mapped[str] = mapped_column(String(2048))
    label: Mapped[str | None] = mapped_column(String(255), default=None)
    auth_kind: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    suites: Mapped[list[Suite]] = relationship(back_populates="target")


class Suite(Base):
    __tablename__ = "suites"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # "scan" | "functional"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    target: Mapped[Target] = relationship(back_populates="suites")
    cases: Mapped[list[TestCase]] = relationship(
        back_populates="suite", cascade="all, delete-orphan"
    )
    runs: Mapped[list[Run]] = relationship(
        back_populates="suite", cascade="all, delete-orphan"
    )
    selector_cache: Mapped[list[SelectorCache]] = relationship(
        back_populates="suite", cascade="all, delete-orphan"
    )


class TestCase(Base):
    __tablename__ = "test_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("suites.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), default="written")  # written|generated
    steps: Mapped[list] = mapped_column(JSONB, default=list)

    suite: Mapped[Suite] = relationship(back_populates="cases")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("suites.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[str] = mapped_column(String(32))
    pages_scanned: Mapped[int | None] = mapped_column(Integer, default=None)
    stopped_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    llm_cost_estimate: Mapped[float | None] = mapped_column(Float, default=None)
    duration: Mapped[float | None] = mapped_column(Float, default=None)

    suite: Mapped[Suite] = relationship(back_populates="runs")
    findings: Mapped[list[Finding]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    step_results: Mapped[list[StepResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class StepResult(Base):
    __tablename__ = "step_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    case_id: Mapped[int | None] = mapped_column(
        ForeignKey("test_cases.id", ondelete="SET NULL"), default=None
    )
    step_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    evidence_path: Mapped[str | None] = mapped_column(Text, default=None)

    run: Mapped[Run] = relationship(back_populates="step_results")


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    check: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")
    page_url: Mapped[str] = mapped_column(Text, default="")
    evidence_path: Mapped[str | None] = mapped_column(Text, default=None)
    finding_key: Mapped[str] = mapped_column(String(64), index=True)  # stable Phase-1 hash

    run: Mapped[Run] = relationship(back_populates="findings")


class SelectorCache(Base):
    __tablename__ = "selector_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("suites.id", ondelete="CASCADE"))
    step_key: Mapped[str] = mapped_column(String(255), index=True)
    selector: Mapped[str] = mapped_column(Text)
    healed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)

    suite: Mapped[Suite] = relationship(back_populates="selector_cache")
