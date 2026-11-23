"""Shared fixtures for the logscope test suite (offline, deterministic)."""

from __future__ import annotations

import datetime as _dt
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from logscope.engine import AnalysisConfig, analyze, analyze_lines  # noqa: E402

APP_LOG = ROOT / "examples" / "app.log"
NGINX_LOG = ROOT / "examples" / "nginx.log"
JSONL_LOG = ROOT / "examples" / "app.jsonl"


@pytest.fixture(scope="session")
def app_analysis():
    """The demo log analysis, computed once for the whole session."""
    return analyze(str(APP_LOG), AnalysisConfig())


@pytest.fixture()
def simple_config():
    return AnalysisConfig()


@pytest.fixture()
def incident_log() -> list[str]:
    """A tiny hand-written log with a genuine, obvious incident."""
    start = _dt.datetime(2026, 5, 12, 10, 0, 0)
    lines: list[str] = []
    for minute in range(12):
        for second in (0, 20, 40):
            stamp = start + _dt.timedelta(minutes=minute, seconds=second)
            lines.append(
                "%s INFO     api      request handled method=GET path=/v1/cart "
                "status=200 duration=%dms" % (stamp.strftime("%Y-%m-%d %H:%M:%S"), 30 + second)
            )
    spike = start + _dt.timedelta(minutes=6)
    for index in range(60):
        stamp = spike + _dt.timedelta(seconds=index)
        lines.append(
            "%s ERROR    api      checkout failed user=u%d reason=upstream_timeout "
            "order=%08x" % (stamp.strftime("%Y-%m-%d %H:%M:%S"), index, index)
        )
    return lines


def analyse_lines(lines, **kwargs):
    """Analyse in-memory lines; a trailing newline is added when missing."""
    body = [line if line.endswith("\n") else line + "\n" for line in lines]
    return analyze_lines(body, AnalysisConfig(**kwargs))
