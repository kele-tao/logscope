"""CLI behaviour: every command, every documented flag, exit codes."""

from __future__ import annotations

import json

import pytest

from logscope.cli import main
from logscope.engine import parse_time_spec


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    captured = capsys.readouterr()
    assert "analyze" in captured.out
    assert "incidents" in captured.out
    assert "timeline" in captured.out


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0


def test_subcommand_help(capsys):
    for command in ("analyze", "incidents", "timeline", "templates", "report", "formats"):
        with pytest.raises(SystemExit) as info:
            main([command, "--help"])
        assert info.value.code == 0


def test_analyze_text_output(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, out, err = run(capsys, "analyze", str(path), "--color", "never")
    assert code == 0
    assert "TEMPLATES BY VOLUME" in out
    assert "errors" in out
    assert err == ""


def test_analyze_json_is_valid(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, out, _ = run(capsys, "analyze", str(path), "--json", "--quiet")
    payload = json.loads(out)
    assert code == 0
    assert payload["tool"] == "logscope"
    assert payload["events"] == len(incident_log)
    assert payload["templates"]


def test_incidents_finds_the_incident(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, out, _ = run(capsys, "incidents", str(path), "--color", "never")
    assert "SEV-1" in out
    assert "checkout failed" in out
    assert "cause 1" in out
    assert code == 0


def test_incidents_json_shape(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, "incidents", str(path), "--json", "--quiet")
    payload = json.loads(out)
    assert payload["incidents"]
    assert payload["incidents"][0]["root_causes"]


def test_timeline_json_rows(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, "timeline", str(path), "--json", "--quiet")
    payload = json.loads(out)
    assert payload["rows"]
    assert {"bucket", "events", "errors"} <= set(payload["rows"][0])


def test_templates_command(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, out, _ = run(capsys, "templates", str(path), "--top", "2", "--color", "never")
    assert code == 0
    assert "templates" in out


def test_report_writes_html(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    out_file = tmp_path / "report.html"
    code, out, _ = run(capsys, "report", str(path), "--out", str(out_file))
    assert code == 0
    body = out_file.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert "<svg" in body
    assert "logscope" in body
    assert "https://" not in body.split("<style>")[-1].split("</style>")[-1][:2000]


def test_report_creates_parent_directory(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    out_file = tmp_path / "nested" / "deep" / "report.html"
    code, _, _ = run(capsys, "report", str(path), "--out", str(out_file), "--quiet")
    assert code == 0
    assert out_file.exists()


def test_formats_command(capsys, tmp_path):
    path = tmp_path / "a.log"
    path.write_text('{"level":"info","message":"a"}\n' * 5, encoding="utf-8")
    code, out, _ = run(capsys, "formats", str(path))
    assert code == 0
    assert "jsonl" in out
    assert "detected: jsonl" in out


def test_fail_on_error_sets_exit_one(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, _, _ = run(capsys, "incidents", str(path), "--quiet", "--fail-on", "error")
    assert code == 1
    code, _, _ = run(capsys, "incidents", str(path), "--quiet", "--fail-on", "any")
    assert code == 1


def test_fail_on_quiet_log_exits_zero(capsys, tmp_path):
    path = tmp_path / "quiet.log"
    lines = [
        "2026-05-12 10:%02d:00 INFO  api  steady heartbeat ok" % minute for minute in range(20)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, _, _ = run(capsys, "analyze", str(path), "--quiet", "--fail-on", "warning")
    assert code == 0


def test_missing_file_exits_two(capsys, tmp_path):
    code, _, err = run(capsys, "analyze", str(tmp_path / "absent.log"))
    assert code == 2
    assert "no such log file" in err


def test_directory_instead_of_file_exits_two(capsys, tmp_path):
    code, _, err = run(capsys, "analyze", str(tmp_path))
    assert code == 2
    assert "directory" in err


def test_malformed_time_exits_two(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, _, err = run(capsys, "analyze", str(path), "--since", "not-a-time")
    assert code == 2
    assert "cannot parse time" in err


def test_bad_grep_regex_exits_two(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, _, err = run(capsys, "analyze", str(path), "--grep", "[unclosed")
    assert code == 2
    assert "invalid --grep" in err


def test_unknown_format_exits_two(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, _, err = run(capsys, "analyze", str(path), "--format", "klingon")
    assert code == 2
    assert "unknown --format" in err


def test_multiple_paths_rejected(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, _, err = run(capsys, "analyze", str(path), str(path))
    assert code == 2
    assert "one log at a time" in err


def test_bucket_flag_accepts_units(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, "timeline", str(path), "--bucket", "1m", "--json", "--quiet")
    payload = json.loads(out)
    assert payload["bucket_seconds"] == 60
    _, out, _ = run(capsys, "timeline", str(path), "--bucket", "5m", "--json", "--quiet")
    assert json.loads(out)["bucket_seconds"] == 300


def test_grep_filters_events(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, "analyze", str(path), "--grep", "checkout failed",
                    "--json", "--quiet")
    payload = json.loads(out)
    assert payload["events"] < len(incident_log)
    assert payload["events"] > 0


def test_invert_grep(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    _, kept, _ = run(capsys, "analyze", str(path), "--grep", "checkout failed",
                     "--json", "--quiet")
    _, dropped, _ = run(capsys, "analyze", str(path), "--grep", "checkout failed",
                        "--invert", "--json", "--quiet")
    assert json.loads(kept)["events"] + json.loads(dropped)["events"] == len(incident_log)


def test_min_level_filter(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, "analyze", str(path), "--min-level", "ERROR",
                    "--json", "--quiet")
    payload = json.loads(out)
    assert payload["events"] > 0
    assert payload["warnings"] == 0


def test_color_never_has_no_escapes(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, "analyze", str(path), "--color", "never")
    assert "\x1b[" not in out


def test_color_always_has_escapes(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    _, out, _ = run(capsys, "analyze", str(path), "--color", "always")
    assert "\x1b[" in out


def test_quiet_suppresses_text(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    code, out, _ = run(capsys, "analyze", str(path), "--quiet")
    assert code == 0
    assert out == ""


def test_bucket_validation_error(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        main(["timeline", str(path), "--bucket", "zz"])
    assert info.value.code == 2


def test_parse_time_spec_relative_and_tod():
    value, tod = parse_time_spec("09:12")
    assert tod is True
    assert value.hour == 9 and value.minute == 12
    value, tod = parse_time_spec("2026-05-12T09:12:00")
    assert tod is False
    assert value.year == 2026


def test_report_quiet_writes_file(capsys, incident_log, tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(incident_log) + "\n", encoding="utf-8")
    target = tmp_path / "quiet.html"
    code, out, _ = run(capsys, "report", str(path), "--out", str(target), "--quiet")
    assert code == 0
    assert out == ""
    assert target.exists()
