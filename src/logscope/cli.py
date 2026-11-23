"""logscope command line interface.

Exit codes
----------
0   analysis completed, no finding crossed the --fail-on threshold
1   findings crossed the --fail-on threshold (CI gate failed)
2   usage error: bad flag, unreadable file, unparsable time

Every user error is printed as a single actionable line; a traceback is a bug.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence
from typing import TextIO

from logscope import __version__
from logscope.engine import Analysis, AnalysisConfig, analyze, parse_time_spec
from logscope.formats import FORMATS, LEVEL_ORDER
from logscope.render import (
    html_report,
    palette_for,
    render_analyze,
    render_incidents,
    render_json,
    render_templates,
    render_timeline,
)

__all__ = ["main", "build_parser"]

PROG = "logscope"

EPILOG = """examples:
  logscope analyze app.log
  logscope incidents app.log --bucket 1m --top 5
  logscope timeline app.log --since 09:12 --until 09:20
  logscope report app.log --out report.html
  tail -f app.log | logscope analyze - --grep timeout

exit codes: 0 clean, 1 findings over --fail-on, 2 usage error.
"""


# --------------------------------------------------------------------------
# Argument helpers
# --------------------------------------------------------------------------

def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not an integer" % value) from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1, got %d" % number)
    return number


def _threshold(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a number" % value) from None
    if not 0.0 < number <= 1.0:
        raise argparse.ArgumentTypeError("must be in (0, 1], got %r" % value)
    return number


def _bucket(value: str) -> int:
    """Accept 30s / 5m / 1h / 90 (seconds)."""
    text = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if text and text[-1] in units:
        digits = text[:-1]
        if not digits.isdigit():
            raise argparse.ArgumentTypeError("expected e.g. 30s, 5m or 1h, got %r" % value)
        return int(digits) * units[text[-1]]
    if not text.isdigit():
        raise argparse.ArgumentTypeError("expected e.g. 30s, 5m or 1h, got %r" % value)
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser (also used by the tests)."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Stream logs into ranked incidents: template mining, spike detection, "
                    "root-cause evidence. Offline, zero dependencies.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("paths", nargs="*", metavar="LOG",
                        help="log file(s) to analyse, or - for stdin (default: stdin)")
    common.add_argument("--format", default="auto", metavar="FMT",
                        help="input dialect: auto (default), %s" % ", ".join(FORMATS))
    common.add_argument("--bucket", type=_bucket, default=0, metavar="SIZE",
                        help="time bucket for histograms, e.g. 30s/1m/5m/1h "
                             "(default: chosen from the log span)")
    common.add_argument("--since", default=None, metavar="TIME",
                        help="only events at/after TIME (ISO 8601 or relative 15m/2h/1d)")
    common.add_argument("--until", default=None, metavar="TIME",
                        help="only events at/before TIME")
    common.add_argument("--grep", default=None, metavar="REGEX",
                        help="only events whose raw line matches REGEX (case-insensitive)")
    common.add_argument("--invert", action="store_true",
                        help="drop events matching --grep instead of keeping them")
    common.add_argument("--min-level", default=None, choices=sorted(LEVEL_ORDER),
                        metavar="LEVEL",
                        help="minimum severity to keep: DEBUG, INFO, WARNING, ERROR, CRITICAL")
    common.add_argument("--threshold", type=_threshold, default=0.7, metavar="SIM",
                        help="template similarity threshold in (0, 1]; lower merges "
                             "more lines into one template (default 0.7)")
    common.add_argument("--top", type=_positive, default=10, metavar="N",
                        help="how many results to show (default 10)")
    common.add_argument("--color", default="auto", choices=("auto", "always", "never"),
                        help="ANSI colour: auto (default), always, never")
    common.add_argument("--json", action="store_true", dest="as_json",
                        help="emit machine-readable JSON instead of text")
    common.add_argument("--fail-on", default=None, metavar="KIND",
                        help="exit 1 when an incident/anomaly of this severity or worse "
                             "exists: warning, error, or any (default: never fail)")
    common.add_argument("--quiet", "-q", action="store_true",
                        help="suppress the human summary (useful with --json or --fail-on)")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = False

    analyze_cmd = sub.add_parser(
        "analyze", parents=[common], help="health summary: volume, templates, incident count",
        description="Summarise a log: volume sparkline, templates ranked by volume, findings.",
    )
    analyze_cmd.set_defaults(handler=_cmd_analyze)

    incidents_cmd = sub.add_parser(
        "incidents", parents=[common], help="ranked incidents with causes and evidence",
        description="Group anomalies into incidents and rank root-cause candidates.",
    )
    incidents_cmd.set_defaults(handler=_cmd_incidents)

    timeline_cmd = sub.add_parser(
        "timeline", parents=[common], help="bucket-by-bucket volume and error table",
        description="Per-bucket table: events, errors, new templates, leading template.",
    )
    timeline_cmd.set_defaults(handler=_cmd_timeline)

    templates_cmd = sub.add_parser(
        "templates", parents=[common], help="list mined templates with samples",
        description="List mined templates with their counts and sample lines.",
    )
    templates_cmd.set_defaults(handler=_cmd_templates)

    report_cmd = sub.add_parser(
        "report", parents=[common], help="write a self-contained dark HTML report",
        description="Render a single-file HTML report with inline SVG sparklines.",
    )
    report_cmd.add_argument("--out", default="logscope-report.html", metavar="FILE",
                            help="output file (default logscope-report.html)")
    report_cmd.set_defaults(handler=_cmd_report)

    formats_cmd = sub.add_parser(
        "formats", help="detect the input dialect and explain the decision",
        description="Run auto-detection on a sample and show per-format scores.",
    )
    formats_cmd.add_argument("paths", nargs="*", metavar="LOG",
                             help="log file(s), or - for stdin (default: stdin)")
    formats_cmd.add_argument("--sample", type=_positive, default=200, metavar="N",
                             help="lines to sample (default 200)")
    formats_cmd.set_defaults(handler=_cmd_formats)
    return parser


# --------------------------------------------------------------------------
# Shared plumbing
# --------------------------------------------------------------------------

def _err(message: str) -> None:
    sys.stderr.write("logscope: error: %s\n" % message)


def _config(args: argparse.Namespace) -> AnalysisConfig:
    cfg = AnalysisConfig(
        fmt=args.format,
        bucket=args.bucket,
        threshold=args.threshold,
        grep=args.grep,
        invert=args.invert,
        min_level=args.min_level,
        top=args.top,
    )
    if args.since:
        cfg.since, cfg.since_tod = parse_time_spec(args.since)
    if args.until:
        cfg.until, cfg.until_tod = parse_time_spec(args.until)
    cfg.validate()
    return cfg


def _single_path(args: argparse.Namespace) -> str:
    paths = list(getattr(args, "paths", []) or [])
    if not paths:
        return "-"
    if len(paths) > 1:
        raise ValueError(
            "this command takes one log at a time (got %d); concatenate them first "
            "with 'cat a.log b.log | logscope %s -'" % (len(paths), args.command or "analyze")
        )
    return paths[0]


def _gate(analysis: Analysis, fail_on: str | None) -> int:
    """Translate --fail-on into an exit code."""
    if not fail_on:
        return 0
    level = fail_on.lower()
    if level in ("any", "all"):
        return 1 if analysis.anomalies else 0
    if level in ("error", "errors", "critical", "sev1"):
        return 1 if any(a.severity == "CRITICAL" for a in analysis.anomalies) else 0
    if level in ("warning", "warn", "sev2"):
        return 1 if analysis.anomalies else 0
    raise ValueError("unknown --fail-on value %r (use warning, error or any)" % fail_on)


def _emit(text: str, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    out.write(text.rstrip("\n") + "\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _cmd_analyze(args: argparse.Namespace) -> int:
    analysis = analyze(_single_path(args), _config(args))
    palette = palette_for(sys.stdout, args.color)
    if args.as_json:
        _emit(render_json(analysis, args.top))
    elif not args.quiet:
        _emit(render_analyze(analysis, args.top, palette))
    return _gate(analysis, args.fail_on)


def _cmd_incidents(args: argparse.Namespace) -> int:
    analysis = analyze(_single_path(args), _config(args))
    palette = palette_for(sys.stdout, args.color)
    if args.as_json:
        payload = {
            "origin": analysis.origin,
            "events": analysis.events,
            "bucket_seconds": analysis.bucket_seconds,
            "incidents": [incident.as_dict() for incident in analysis.incidents[: args.top]],
        }
        _emit(json.dumps(payload, indent=2, ensure_ascii=False))
    elif not args.quiet:
        _emit(render_incidents(analysis, args.top, palette))
    return _gate(analysis, args.fail_on)


def _cmd_timeline(args: argparse.Namespace) -> int:
    analysis = analyze(_single_path(args), _config(args))
    palette = palette_for(sys.stdout, args.color)
    if args.as_json:
        payload = {
            "origin": analysis.origin,
            "bucket_seconds": analysis.bucket_seconds,
            "rows": [
                {
                    "bucket": row["bucket"].isoformat(sep=" "),
                    "events": row["events"],
                    "errors": row["errors"],
                    "new_templates": row["new_templates"],
                    "top_template": row["top_template"],
                }
                for row in analysis.timeline_rows()
            ],
        }
        _emit(json.dumps(payload, indent=2, ensure_ascii=False))
    elif not args.quiet:
        _emit(render_timeline(analysis, args.top if args.top != 10 else None, palette))
    return _gate(analysis, args.fail_on)


def _cmd_templates(args: argparse.Namespace) -> int:
    analysis = analyze(_single_path(args), _config(args))
    palette = palette_for(sys.stdout, args.color)
    if args.as_json:
        _emit(render_json(analysis, args.top))
    elif not args.quiet:
        _emit(render_templates(analysis, args.top, palette))
    return _gate(analysis, args.fail_on)


def _cmd_report(args: argparse.Namespace) -> int:
    analysis = analyze(_single_path(args), _config(args))
    document = html_report(analysis, args.top, title="logscope report -- %s" % analysis.origin)
    out = pathlib.Path(args.out)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(document, encoding="utf-8")
    except OSError as exc:
        raise ValueError("cannot write %s: %s" % (out, exc)) from exc
    if not args.quiet:
        size = out.stat().st_size
        _emit("wrote %s (%s bytes, %d incidents, %d anomalies)"
              % (out, format(size, ","), len(analysis.incidents), len(analysis.anomalies)))
    return _gate(analysis, args.fail_on)


def _cmd_formats(args: argparse.Namespace) -> int:
    path = _single_path(args)
    from logscope.formats import _score, detect_format

    stream = sys.stdin if path == "-" else open(path, encoding="utf-8", errors="replace")
    try:
        head = []
        for line in stream:
            head.append(line)
            if len(head) >= args.sample:
                break
    finally:
        if path != "-":
            stream.close()
    sample = [line for line in head if line.strip()]
    if not sample:
        raise ValueError("no non-empty lines found in %s" % (path if path != "-" else "stdin"))
    scores = _score(sample[: args.sample])
    winner = detect_format(sample, args.sample)
    lines = [
        "%s  %s" % (path if path != "-" else "<stdin>", "(%d sampled lines)" % len(sample)),
        "",
    ]
    for fmt in FORMATS:
        mixed = scores[fmt]
        marker = "->" if fmt == winner else "  "
        lines.append("  %s %-8s %6d" % (marker, fmt, mixed))
    lines.append("")
    lines.append("  detected: %s" % winner)
    _emit("\n".join(lines))
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _prefer_utf8() -> None:
    """Make block-character output safe on legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if "utf" in encoding:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    _prefer_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args)
    except FileNotFoundError as exc:
        _err("no such log file: %s" % (exc.filename or "?"))
        return 2
    except IsADirectoryError as exc:
        _err("%s is a directory, not a log file" % (exc.filename or "?"))
        return 2
    except PermissionError as exc:
        _err("permission denied reading %s" % (exc.filename or "?"))
        return 2
    except (ValueError, OSError) as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
