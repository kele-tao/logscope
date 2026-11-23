"""Engine plumbing, renderers and end-to-end behaviour on the bundled demo log."""

from __future__ import annotations

import datetime as _dt
import json
import pathlib

import pytest
from conftest import APP_LOG, JSONL_LOG, NGINX_LOG, analyse_lines

from logscope.engine import AnalysisConfig, analyze, fill_gaps, parse_time
from logscope.formats import Event
from logscope.render import (
    Palette,
    html_report,
    palette_for,
    render_analyze,
    render_incidents,
    render_json,
    render_templates,
    render_timeline,
    sparkline,
    svg_bars,
    svg_sparkline,
)


def test_analyze_demo_log_finds_the_incident(app_analysis):
    assert app_analysis.events == 20000
    assert app_analysis.incidents, "the demo log contains a planted incident"
    top = app_analysis.incidents[0]
    assert top.severity == "CRITICAL"
    assert "checkout failed" in top.title or "circuit breaker" in top.title
    assert top.root_causes[0].evidence


def test_analyze_nginx_log_detects_5xx_incident():
    analysis = analyze(str(NGINX_LOG), AnalysisConfig())
    assert analysis.fmt == "cli"
    assert analysis.events == 4000
    assert analysis.errors > 400
    assert analysis.incidents
    assert "status=5" in analysis.incidents[0].title


def test_analyze_jsonl_log():
    analysis = analyze(str(JSONL_LOG), AnalysisConfig())
    assert analysis.fmt == "jsonl"
    assert analysis.events == 800


def test_analysis_to_dict_is_json_serialisable(app_analysis):
    payload = json.dumps(app_analysis.to_dict(5))
    decoded = json.loads(payload)
    assert decoded["events"] == app_analysis.events
    assert decoded["templates"]
    assert decoded["timeline"]


def test_analysis_properties(app_analysis):
    assert 0 < app_analysis.error_rate < 1
    assert app_analysis.span_seconds > 0
    assert app_analysis.peak_per_bucket > 0
    assert app_analysis.template_count == len(app_analysis.clusters)


def test_histogram_gaps_are_filled(app_analysis):
    histogram = app_analysis.histogram()
    keys = sorted(histogram)
    step = _dt.timedelta(seconds=app_analysis.bucket_seconds)
    for left, right in zip(keys, keys[1:], strict=False):
        assert right - left == step


def test_fill_gaps_zero_fills():
    base = _dt.datetime(2026, 5, 12, 10, 0)
    series = {base: 2, base + _dt.timedelta(minutes=3): 5}
    filled = fill_gaps(series, 60)
    assert len(filled) == 4
    assert filled[base + _dt.timedelta(minutes=1)] == 0


def test_config_validation_errors():
    with pytest.raises(ValueError):
        AnalysisConfig(fmt="klingon").validate()
    with pytest.raises(ValueError):
        AnalysisConfig(bucket=-5).validate()
    with pytest.raises(ValueError):
        AnalysisConfig(top=0).validate()
    with pytest.raises(ValueError):
        AnalysisConfig(since=_dt.datetime(2026, 5, 13),
                       until=_dt.datetime(2026, 5, 12)).validate()
    with pytest.raises(ValueError):
        AnalysisConfig(grep="[bad").make_regex()


def test_parse_time_helper():
    assert parse_time("2026-05-12T09:30:00") == _dt.datetime(2026, 5, 12, 9, 30)
    assert parse_time("2026-05-12 09:30") == _dt.datetime(2026, 5, 12, 9, 30)
    now = _dt.datetime(2026, 5, 12, 12, 0)
    assert parse_time("15m", now) == _dt.datetime(2026, 5, 12, 11, 45)
    with pytest.raises(ValueError):
        parse_time("yesterday")


def test_empty_input_is_safe():
    analysis = analyse_lines([])
    assert analysis.events == 0
    assert analysis.incidents == []
    assert analysis.histogram() == {}
    assert render_analyze(analysis) .count("TEMPLATES") == 1


def test_unicode_input_is_safe():
    lines = [
        "2026-05-12 10:%02d:00 ERROR  api  用户登录失败 🔥 retry" % minute
        for minute in range(10)
    ]
    analysis = analyse_lines(lines)
    assert analysis.events == 10
    text = render_analyze(analysis)
    assert "用户登录失败" in text
    assert html_report(analysis)


def test_sparkline_handles_empty_and_single():
    assert sparkline([]) == ""
    marks = sparkline([1.0])
    assert len(marks) == 1
    assert len(sparkline(list(range(200)), 40)) == 40


def test_sparkline_peak_is_full_block():
    marks = sparkline([0.0, 0.0, 8.0], 3)
    assert marks[-1] == "█"


def test_svg_sparkline_is_offline(app_analysis):
    svg = svg_sparkline(list(app_analysis.histogram().values()))
    assert svg.startswith("<svg")
    assert "<polyline" in svg
    assert "<script" not in svg
    # The only URL allowed is the SVG XML namespace, which browsers never fetch.
    assert svg.count("http") == 1
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg


def test_svg_sparkline_empty_is_valid():
    assert svg_sparkline([]).startswith("<svg")


def test_svg_bars_renders_one_rect_per_bucket():
    svg = svg_bars([1.0, 2.0, 3.0], width=300, height=60)
    assert svg.startswith("<svg")
    assert svg.count("<rect") == 3
    assert "<script" not in svg


def test_svg_bars_stacks_errors_on_top():
    total = [10.0, 10.0]
    errors = [0.0, 4.0]
    svg = svg_bars(total, error_values=errors)
    # Bucket one is all traffic; bucket two is split into traffic + error share.
    assert svg.count("<rect") == 3
    assert svg.count('fill="#ff6b6b"') == 1
    all_errors = svg_bars([10.0], error_values=[10.0])
    assert all_errors.count('fill="#ff6b6b"') == 1
    assert all_errors.count('opacity="0.75"') == 0


def test_svg_bars_empty_is_valid():
    assert svg_bars([]).startswith("<svg")


def test_palette_modes():
    plain = Palette(False)
    coloured = Palette(True)
    assert plain.red("x") == "x"
    assert "\x1b[" in coloured.red("x")
    assert palette_for(None, "never").enabled is False
    assert palette_for(None, "always").enabled is True
    assert palette_for(None, "auto", {"FORCE_COLOR": "1"}).enabled is True
    assert palette_for(None, "auto", {"NO_COLOR": "1"}).enabled is False
    assert palette_for(None, "auto", {}).enabled is False


def test_renderers_produce_output(app_analysis):
    plain = Palette(False)
    for renderer in (render_analyze, render_incidents, render_templates, render_timeline):
        text = renderer(app_analysis, 5, plain)
        assert isinstance(text, str)
        assert text.strip()


def test_render_json_round_trips(app_analysis):
    payload = json.loads(render_json(app_analysis, 3))
    assert payload["tool"] == "logscope"
    assert len(payload["templates"]) <= 3


def test_html_report_is_self_contained(app_analysis, tmp_path):
    document = html_report(app_analysis, 5)
    assert document.startswith("<!doctype html>")
    assert document.rstrip().endswith("</html>")
    assert "<script" not in document
    assert "<link" not in document
    assert "src=" not in document
    assert "href=" not in document
    # Every URL in the file is the SVG namespace, never a fetched asset.
    assert document.count("http") == document.count('xmlns="http://www.w3.org/2000/svg"')
    assert document.count("<svg") >= 2
    target = tmp_path / "r.html"
    target.write_text(document, encoding="utf-8")
    assert target.stat().st_size > 8000


def test_html_report_escapes_untrusted_text():
    lines = [
        "2026-05-12 10:00:00 ERROR api <script>alert(1)</script> & more" for _ in range(3)
    ]
    analysis = analyse_lines(lines)
    document = html_report(analysis)
    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;" in document


def test_timeline_rows_structure(app_analysis):
    rows = app_analysis.timeline_rows()
    assert rows
    row = rows[0]
    assert set(row) == {
        "bucket", "events", "errors", "new_templates", "top_template", "top_count"
    }
    assert sum(row["events"] for row in rows) == app_analysis.events


def test_error_histogram_subset_of_histogram(app_analysis):
    errors = app_analysis.error_histogram()
    histogram = app_analysis.histogram()
    for bucket, count in errors.items():
        assert bucket in histogram
        assert 0 <= count <= histogram[bucket]


def test_analyze_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        analyze(str(pathlib.Path("does-not-exist.log")))


def test_deterministic_output(app_analysis):
    first = render_json(app_analysis, 5)
    second = render_json(analyze(str(APP_LOG), AnalysisConfig()), 5)
    def strip_timings(text: str) -> list[str]:
        return [line for line in text.splitlines() if "duration" not in line]

    assert strip_timings(first) == strip_timings(second)


def test_event_as_dict():
    event = Event(ts=_dt.datetime(2026, 5, 12, 10, 0), message="m", level="ERROR")
    payload = event.as_dict()
    assert payload["level"] == "ERROR"
    assert payload["ts"].startswith("2026-05-12")
