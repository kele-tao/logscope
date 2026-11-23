"""Ingestion: every supported dialect, error handling, unicode, big input."""

from __future__ import annotations

import datetime as _dt
import io

import pytest

from logscope.formats import (
    FORMATS,
    Event,
    ParseStats,
    Reader,
    detect_format,
    normalise_level,
    parse_cli,
    parse_jsonl,
    parse_line,
    parse_python,
    parse_syslog,
    parse_text,
    parse_timestamp,
)


def test_parse_python_default_line():
    event = parse_python("2026-05-12 10:00:00,123 ERROR    db    connection refused", 7)
    assert event is not None
    assert event.ts == _dt.datetime(2026, 5, 12, 10, 0, 0, 123000)
    assert event.level == "ERROR"
    assert event.message == "connection refused"
    assert event.source == "db"
    assert event.line == 7


def test_parse_python_without_level_keeps_message():
    event = parse_python("2026-05-12 10:00:00.500 worker started", 1)
    assert event is not None
    assert event.level == "INFO"
    assert "worker" in event.message and "started" in event.message


def test_parse_jsonl_timestamp_and_level():
    line = '{"timestamp":"2026-05-12T10:00:00Z","level":"warn","message":"disk 91%","service":"node"}'
    event = parse_jsonl(line, 2)
    assert event is not None
    assert event.level == "WARNING"
    assert event.message == "disk 91%"
    assert event.source == "node"


def test_parse_jsonl_epoch_millis():
    event = parse_jsonl('{"ts":1778666400000,"msg":"boot","severity":"info"}', 1)
    assert event is not None
    assert event.ts is not None
    assert event.ts.year == 2026


def test_parse_jsonl_rejects_non_json():
    assert parse_jsonl("not json at all", 1) is None
    assert parse_jsonl("[1,2,3]", 1) is None
    assert parse_jsonl("   ", 1) is None


def test_parse_syslog_rfc5424():
    line = "<11>1 2026-05-12T10:00:00.123Z web-01 api 42 ID47 - upstream timeout"
    event = parse_syslog(line, 1)
    assert event is not None
    assert event.level == "ERROR"
    assert event.message == "upstream timeout"
    assert event.source == "api"


def test_parse_syslog_rfc3164():
    line = "May 12 10:00:00 web-01 sshd[123]: Failed password for root"
    event = parse_syslog(line, 1)
    assert event is not None
    assert event.ts == _dt.datetime(2026, 5, 12, 10, 0, 0)
    assert event.source == "sshd"
    assert "Failed password" in event.message


def test_parse_cli_combined_log():
    line = (
        '10.0.0.1 - - [12/May/2026:10:00:00 +0000] "GET /v1/cart HTTP/1.1" '
        '503 1234 "-" "curl/8.5.0"'
    )
    event = parse_cli(line, 1)
    assert event is not None
    assert event.level == "ERROR"
    assert event.ts == _dt.datetime(2026, 5, 12, 10, 0, 0)
    assert "status=503" in event.message
    assert event.extra["user_agent"] == "curl/8.5.0"


def test_parse_cli_rejects_html_line():
    assert parse_cli("<html><body>oops</body></html>", 1) is None


def test_parse_text_falls_back_to_now_less_timestamp():
    event = parse_text("something exploded in the worker", 3)
    assert event is not None
    assert event.ts is None
    assert event.level == "INFO"


def test_parse_text_detects_level_token():
    event = parse_text("2026-05-12 10:00:00 WARN queue depth 900", 1)
    assert event is not None
    assert event.level == "WARNING"
    assert event.ts is not None


def test_parse_timestamp_variants():
    assert parse_timestamp("2026-05-12 10:00:00") == _dt.datetime(2026, 5, 12, 10, 0, 0)
    assert parse_timestamp("2026-05-12T10:00:00+02:00") == _dt.datetime(2026, 5, 12, 8, 0, 0)
    epoch = int(_dt.datetime(2026, 5, 12, 10, 0, tzinfo=_dt.timezone.utc).timestamp())
    assert parse_timestamp(str(epoch)) == _dt.datetime(2026, 5, 12, 10, 0, 0)
    assert parse_timestamp(str(epoch * 1000)) == _dt.datetime(2026, 5, 12, 10, 0, 0)
    assert parse_timestamp("nothing here") is None


def test_normalise_level_aliases():
    assert normalise_level("WARN") == "WARNING"
    assert normalise_level("fatal") == "CRITICAL"
    assert normalise_level("nonsense") == "INFO"
    assert normalise_level(None) == "INFO"


def test_parse_line_unknown_format_raises():
    with pytest.raises(ValueError, match="unknown log format"):
        parse_line("x", "klingon", 1)


def test_detect_format_identifies_each_dialect():
    jsonl = ['{"level":"info","message":"a"}'] * 4
    syslog = ["May 12 10:00:00 host app[1]: hello"] * 4
    cli = ['1.2.3.4 - - [12/May/2026:10:00:00 +0000] "GET / HTTP/1.1" 200 12 "-" "x"'] * 4
    py = ["2026-05-12 10:00:00 INFO app started"] * 4
    assert detect_format(jsonl) == "jsonl"
    assert detect_format(syslog) == "syslog"
    assert detect_format(cli) == "cli"
    assert detect_format(py) == "python"
    assert detect_format(["just text", "more text"]) == "text"


def test_detect_format_empty_is_text():
    assert detect_format([]) == "text"


def test_reader_streams_and_counts():
    body = "2026-05-12 10:00:00 INFO a one\n\n2026-05-12 10:00:01 INFO a two\n"
    reader = Reader(io.StringIO(body), fmt="python")
    events = list(reader)
    assert [e.message for e in events] == ["a one", "a two"]
    assert reader.stats.lines == 3
    assert reader.stats.parsed == 2
    assert reader.stats.timestamped == 2


def test_reader_auto_detects():
    body = "\n".join('{"level":"info","message":"m%d"}' % i for i in range(5))
    reader = Reader(io.StringIO(body))
    events = list(reader)
    assert len(events) == 5
    assert reader.format == "jsonl"


def test_reader_uses_first_timestamp_for_undated_lines():
    body = "2026-05-12 10:00:00 INFO a first\ncontinuation line without a date\n"
    events = list(Reader(io.StringIO(body), fmt="python"))
    assert events[0].ts == _dt.datetime(2026, 5, 12, 10, 0, 0)


def test_unicode_and_emoji_survive():
    line = "2026-05-12 10:00:00 ERROR api 用户 连接失败 🔥 retry"
    event = parse_python(line, 1)
    assert event is not None
    assert "连接失败" in event.message
    assert "🔥" in event.message


def test_large_input_is_streamed(incident_log):
    events = list(Reader(io.StringIO("\n".join(incident_log)), fmt="python"))
    assert len(events) == len(incident_log)


def test_formats_constant_is_stable():
    assert FORMATS == ("jsonl", "syslog", "cli", "python", "text")


def test_parse_stats_record():
    stats = ParseStats()
    event = Event(ts=None, message="m")
    stats.record("text", None)
    stats.record("text", event)
    stats.record("text", Event(ts=_dt.datetime(2026, 1, 1), message="m"))
    assert stats.lines == 3
    assert stats.parsed == 2
    assert stats.timestamped == 1
    assert stats.by_format["text"] == 2
