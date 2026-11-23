"""Renderers: ANSI terminal text and a self-contained dark HTML report.

Text output is deterministic and degrades to plain ASCII when stdout is not a TTY
or NO_COLOR is set.

The HTML report embeds inline SVG sparklines -- no CDN, no JavaScript, no fonts
to fetch. Open it on a plane.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
from dataclasses import dataclass

from logscope.engine import Analysis
from logscope.incidents import Incident
from logscope.miner import Cluster

__all__ = [
    "Palette",
    "palette_for",
    "render_analyze",
    "render_incidents",
    "render_timeline",
    "render_templates",
    "render_json",
    "sparkline",
    "html_report",
    "svg_sparkline",
    "svg_bars",
]

SPARK_CHARS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


@dataclass(slots=True)
class Palette:
    """ANSI colour helper that can be switched off globally."""

    enabled: bool = True

    def wrap(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return "\x1b[" + ";".join(codes) + "m" + text + "\x1b[0m"

    def bold(self, text: str) -> str:
        return self.wrap(text, "1")

    def dim(self, text: str) -> str:
        return self.wrap(text, "90")

    def red(self, text: str) -> str:
        return self.wrap(text, "1", "91")

    def yellow(self, text: str) -> str:
        return self.wrap(text, "93")

    def green(self, text: str) -> str:
        return self.wrap(text, "92")

    def cyan(self, text: str) -> str:
        return self.wrap(text, "96")

    def blue(self, text: str) -> str:
        return self.wrap(text, "94")

    def severity(self, label: str) -> str:
        if label == "SEV-1":
            return self.red(label)
        if label == "SEV-2":
            return self.yellow(label)
        return self.cyan(label)


def palette_for(stream, color: str = "auto", environ=None) -> Palette:
    """Resolve --color auto/always/never into a Palette.

    auto honours the conventions every CLI user already expects: NO_COLOR wins,
    FORCE_COLOR forces colour on for pipes and recorders, otherwise a TTY decides.
    """
    import os

    env = os.environ if environ is None else environ
    if color == "never":
        return Palette(False)
    if color == "always":
        return Palette(True)
    if env.get("NO_COLOR"):
        return Palette(False)
    if env.get("FORCE_COLOR") and env.get("FORCE_COLOR") != "0":
        return Palette(True)
    return Palette(bool(getattr(stream, "isatty", lambda: False)()))


def _num(value: float) -> str:
    return format(int(value), ",")


def _pct(part: int, whole: int) -> str:
    if not whole:
        return "0.0%"
    return "%.1f%%" % (100.0 * part / whole)


def _short(text: str, width: int) -> str:
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[: width - 1] + "\u2026"


def _bucket_label(seconds: int) -> str:
    if seconds % 3600 == 0:
        return "%dh" % (seconds // 3600)
    if seconds % 60 == 0:
        return "%dm" % (seconds // 60)
    return "%ds" % seconds


def _human_duration(seconds: float) -> str:
    if seconds < 1:
        return "<1s"
    if seconds < 90:
        return "%ds" % round(seconds)
    if seconds < 5400:
        return "%dm" % round(seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)


def sparkline(values: list[float], width: int = 48) -> str:
    """Block-character sparkline, downsampled to width buckets."""
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / float(width)
        values = [
            sum(values[int(i * step): max(int(i * step) + 1, int((i + 1) * step))])
            for i in range(width)
        ]
    peak = max(values) or 1.0
    out = []
    for value in values:
        index = int(round((value / peak) * (len(SPARK_CHARS) - 1)))
        out.append(SPARK_CHARS[max(0, min(len(SPARK_CHARS) - 1, index))])
    return "".join(out)


def _header(analysis: Analysis, palette: Palette, title: str) -> list[str]:
    span = _human_duration(analysis.span_seconds)
    left = "  %s  %s" % (palette.bold(title), palette.dim(_short(analysis.origin, 70)))
    right = palette.dim(
        "format %s | %s events | %s templates | %s span | bucket %s"
        % (
            analysis.fmt,
            _num(analysis.events),
            _num(analysis.template_count),
            span,
            _bucket_label(analysis.bucket_seconds),
        )
    )
    errors = palette.red(str(analysis.errors)) if analysis.errors else "0"
    stats = "  %s errors (%s) | %s warnings | %s anomalies in %s incidents" % (
        errors,
        _pct(analysis.errors, analysis.events),
        analysis.warnings,
        palette.bold(str(len(analysis.anomalies))),
        palette.bold(str(len(analysis.incidents))),
    )
    return [left, right, stats, ""]


def render_analyze(analysis: Analysis, top: int = 10, palette: Palette | None = None) -> str:
    """The default summary: health line, ranked templates, incident count."""
    p = palette or Palette(False)
    lines = _header(analysis, p, "analyze")
    hist = analysis.histogram()
    if hist:
        values = list(hist.values())
        lines.append(
            "  volume %s  peak %s/bucket"
            % (p.cyan(sparkline(values, 56)), _num(max(values) if values else 0))
        )
        lines.append("")

    lines.append("  %s" % p.bold("TEMPLATES BY VOLUME"))
    lines.append(
        "  %s  %s  %s  %s  %s"
        % (p.dim("count".rjust(8)), p.dim("err".rjust(6)),
           p.dim("level".ljust(8)), p.dim("first seen".ljust(10)), p.dim("template"))
    )
    for cluster in sorted(analysis.clusters, key=lambda c: (-c.count, c.cluster_id))[:top]:
        level = cluster.severity
        painted = p.red(level.ljust(8)) if level in ("ERROR", "CRITICAL") else (
            p.yellow(level.ljust(8)) if level == "WARNING" else p.dim(level.ljust(8)))
        first = cluster.first_seen.strftime("%H:%M:%S") if cluster.first_seen else "-"
        lines.append(
            "  %s  %s  %s  %s  %s"
            % (_num(cluster.count).rjust(8), str(cluster.error_count).rjust(6), painted,
               first.ljust(10), _short(cluster.template, 78))
        )
    if analysis.template_count > top:
        lines.append(p.dim("  ... %d more templates (raise --top to see them)"
                           % (analysis.template_count - top)))

    lines.append("")
    if analysis.incidents:
        lines.append(
            "  %s %d incident(s), %d anomal%s -- run 'logscope incidents' for the timeline"
            % (p.bold("FINDINGS"), len(analysis.incidents), len(analysis.anomalies),
               "y" if len(analysis.anomalies) == 1 else "ies")
        )
        for incident in analysis.incidents[:3]:
            lines.append("  %s  %s" % (p.severity(incident.severity_label),
                                       _short(incident.title, 88)))
    else:
        lines.append("  %s no incidents detected in %s events"
                     % (p.green("OK"), _num(analysis.events)))
    return "\n".join(lines)


def render_incidents(analysis: Analysis, top: int = 5, palette: Palette | None = None) -> str:
    """The headline view: ranked incidents with causes, evidence and timeline."""
    p = palette or Palette(False)
    lines = _header(analysis, p, "incidents")
    if not analysis.incidents:
        lines.append("  %s no incidents detected across %s events and %s templates."
                     % (p.green("OK"), _num(analysis.events), _num(analysis.template_count)))
        hints = ["  Try --grep, --since/--until, or a lower --threshold to widen the net."]
        buckets = len(analysis.histogram())
        if 0 < buckets <= 6:
            hints.append(
                "  Only %d time bucket(s) in this window; spike detection needs at least 6 "
                "to learn a baseline. Widen --since/--until." % buckets
            )
        lines.extend(p.dim(hint) for hint in hints)
        return "\n".join(lines)

    shown = analysis.incidents[: max(1, top)]
    for incident in shown:
        lines.extend(_render_incident(incident, analysis, p))
    if len(analysis.incidents) > len(shown):
        lines.append(p.dim("  %d more incident(s) not shown (raise --top)"
                           % (len(analysis.incidents) - len(shown))))
    return "\n".join(lines)


def _render_incident(incident: Incident, analysis: Analysis, p: Palette) -> list[str]:
    out: list[str] = []
    out.append("  %s  %s" % (p.severity(incident.severity_label),
                             p.bold(_short(incident.title, 92))))
    window = "?"
    if incident.start and incident.end:
        window = "%s -> %s (%s)" % (
            incident.start.strftime("%H:%M:%S"),
            incident.end.strftime("%H:%M:%S"),
            _human_duration(incident.duration_seconds),
        )
    state = p.red("STILL OPEN") if incident.still_open else p.green("recovered")
    peak = incident.peak.strftime("%H:%M:%S") if incident.peak else "-"
    out.append(
        "       %s  %s  peak %s  %d events / %d errors  confidence %s"
        % (window, state, peak, incident.peak_events, incident.errors,
           "%.0f" % incident.confidence)
    )
    spark = _incident_spark(incident, analysis)
    if spark:
        out.append("       %s %s" % (p.cyan(spark), p.dim("errors across the incident")))
    out.append("")
    for index, cause in enumerate(incident.root_causes[:3], 1):
        marker = p.green("->") if index == 1 else p.dim("  ")
        out.append(
            "       %s %s %s  %s"
            % (marker, p.bold("cause %d" % index), _short(cause.template, 72),
               p.dim("score %.1f" % cause.score))
        )
        for reason in cause.reasons[:3]:
            out.append("          %s %s" % (p.dim("-"), p.dim(reason)))
        for line in cause.evidence[:2]:
            out.append("          %s %s" % (p.blue(">"), _short(line, 128)))
    extra = len(incident.root_causes) - 3
    if extra > 0:
        out.append(p.dim("          ... %d more cause candidate(s)" % extra))
    kinds = sorted({a.kind for a in incident.anomalies})
    out.append("       %s %s" % (p.dim("signals:"), p.dim(", ".join(kinds) if kinds else "none")))
    out.append("")
    return out


def _incident_spark(incident: Incident, analysis: Analysis) -> str:
    """Error volume across the incident window -- the shape of the outage.

    Total volume is useless here: a steady 700 lines/min log looks flat during a
    five-alarm fire. The sparkline therefore plots ERROR/CRITICAL counts only.
    """
    errors = analysis.error_histogram()
    if not errors:
        return sparkline([])
    keys = sorted(errors)
    start = incident.start or keys[0]
    end = incident.end or keys[-1]
    window = [count for key, count in errors.items() if start <= key <= end]
    if not window or max(window) == 0:
        return sparkline([])
    return sparkline([float(v) for v in window], 44)


def render_timeline(analysis: Analysis, limit: int | None = None,
                    palette: Palette | None = None) -> str:
    """Bucket-by-bucket table with anomaly buckets flagged."""
    p = palette or Palette(False)
    rows = analysis.timeline_rows()
    if not rows:
        return p.dim("  no events to plot")
    if limit:
        rows = rows[-limit:]
    lines = [
        "  %s  %s  %s  %s  %s"
        % (p.dim("bucket".ljust(11)), p.dim("events".rjust(8)),
           p.dim("errors".rjust(7)), p.dim("new".rjust(4)), p.dim("leading template"))
    ]
    peak = max((row["events"] for row in rows), default=0)
    anomaly_buckets = {a.bucket for a in analysis.anomalies if a.bucket}
    for row in rows:
        bucket = row["bucket"]
        assert isinstance(bucket, _dt.datetime)
        marker = p.red("!") if bucket in anomaly_buckets else " "
        bar_len = int(round(28.0 * float(row["events"]) / peak)) if peak else 0
        bar = p.cyan("\u2588" * bar_len)
        lines.append(
            " %s%s  %s  %s  %s  %s %s"
            % (marker, bucket.strftime("%m-%d %H:%M").ljust(11),
               _num(float(row["events"])).rjust(8),
               _num(float(row["errors"])).rjust(7),
               str(row["new_templates"]).rjust(4),
               bar.ljust(28),
               _short(str(row["top_template"]), 46))
        )
    reopened = sum(1 for incident in analysis.incidents if incident.still_open)
    lines.append("")
    lines.append(p.dim("  %d buckets | %d anomalies | %d still open at end of log"
                       % (len(rows), len(analysis.anomalies), reopened)))
    return "\n".join(lines)


def render_templates(analysis: Analysis, top: int = 20, palette: Palette | None = None) -> str:
    """Full template list with samples -- the 'what is in my log' view."""
    p = palette or Palette(False)
    lines = _header(analysis, p, "templates")
    ranked = sorted(analysis.clusters, key=lambda c: (-c.count, c.cluster_id))[:top]
    for index, cluster in enumerate(ranked, 1):
        level = cluster.severity
        painted = p.red(level) if level in ("ERROR", "CRITICAL") else (
            p.yellow(level) if level == "WARNING" else p.dim(level))
        lines.append(
            "  %s %s  %s  %s"
            % (p.dim("%2d." % index), p.bold(_short(cluster.template, 84)),
               _num(cluster.count).rjust(7), painted)
        )
        for sample in cluster.samples[:2]:
            lines.append("       %s %s" % (p.dim("-"), p.dim(_short(sample, 110))))
    lines.append("")
    lines.append(p.dim("  %d templates | %d events"
                       % (analysis.template_count, analysis.events)))
    return "\n".join(lines)


def render_json(analysis: Analysis, top: int = 10) -> str:
    """Stable JSON for piping into jq."""
    return json.dumps(analysis.to_dict(top), indent=2, ensure_ascii=False, sort_keys=False)

# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------

_CSS = """
:root{--bg:#0b0f17;--panel:#121826;--line:#1f2937;--fg:#d7dee9;--muted:#8794a8;
      --red:#ff6b6b;--amber:#fbbf24;--green:#4ade80;--cyan:#22d3ee;--blue:#60a5fa}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,"Cascadia Mono",monospace}
.wrap{max-width:1180px;margin:0 auto;padding:36px 28px 64px}
h1{font-size:30px;margin:0 0 6px;letter-spacing:-.4px}
h2{font-size:19px;margin:38px 0 14px;letter-spacing:-.2px}
h2 .n{color:var(--muted);font-weight:500;font-size:14px;margin-left:8px}
.sub{color:var(--muted);font-size:14px;margin:0 0 22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:22px 0 8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.card .v{font-size:24px;font-weight:650;margin-top:6px}
.card.bad .v{color:var(--red)}.card.warn .v{color:var(--amber)}.card.good .v{color:var(--green)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:14px 0}
.panel h3{margin:0 0 6px;font-size:17px}
.row{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:13px}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
       font-weight:700;letter-spacing:.04em}
.badge.sev1{background:rgba(255,107,107,.14);color:var(--red);border:1px solid rgba(255,107,107,.4)}
.badge.sev2{background:rgba(251,191,36,.13);color:var(--amber);border:1px solid rgba(251,191,36,.4)}
.badge.sev3{background:rgba(34,211,238,.12);color:var(--cyan);border:1px solid rgba(34,211,238,.35)}
.badge.bad{background:rgba(255,107,107,.12);color:var(--red)}
.badge.ok{background:rgba(74,222,128,.12);color:var(--green)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;
   letter-spacing:.06em;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid rgba(31,41,55,.6);vertical-align:top}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.tmpl{color:#c7d2e0}
.ev{color:var(--muted);font-size:12px;margin:3px 0 0;white-space:pre-wrap;word-break:break-word}
.ev b{color:var(--blue)}
.cause{margin:14px 0 0;padding:12px 14px;border-left:3px solid var(--line);
       background:rgba(255,255,255,.02);border-radius:0 8px 8px 0}
.cause.first{border-left-color:var(--red)}
.cause .why{color:var(--muted);font-size:13px;margin:4px 0}
.bar{height:6px;border-radius:3px;background:rgba(96,165,250,.35)}
.foot{color:var(--muted);font-size:12.5px;margin-top:44px;border-top:1px solid var(--line);padding-top:14px}
"""


def svg_sparkline(values: list[float], width: int = 640, height: int = 64,
                  color: str = "#60a5fa") -> str:
    """Inline SVG area sparkline -- no JS, no external assets.

    The y axis gets 10% headroom so a flat series does not draw its line along
    the top edge and masquerade as a filled box.
    """
    if not values:
        return '<svg width="%d" height="%d"></svg>' % (width, height)
    peak = (max(values) or 1.0) * 1.1
    count = len(values)
    if count == 1:
        values = values * 2
        count = 2
    pad = 3
    usable = height - pad * 2
    step = (width - 2) / float(count - 1)
    points = []
    for index, value in enumerate(values):
        x = 1 + index * step
        y = height - pad - (value / peak) * usable
        points.append("%.1f,%.1f" % (x, y))
    line = " ".join(points)
    area = "1,%d %s %d,%d" % (height - pad, line, width - 1, height - pad)
    return (
        '<svg viewBox="0 0 %d %d" width="100%%" height="%d" preserveAspectRatio="none" '
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="event volume sparkline">'
        '<polygon points="%s" fill="%s" opacity="0.22"/>'
        '<polyline points="%s" fill="none" stroke="%s" stroke-width="1.6" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        "</svg>" % (width, height, height, area, color, line, color)
    )


def svg_bars(values: list[float], width: int = 1100, height: int = 96,
             bar_color: str = "#60a5fa", error_values: list[float] | None = None,
             error_color: str = "#ff6b6b") -> str:
    """Inline SVG bar chart -- reads correctly for flat and spiky series alike.

    When error_values is supplied, the error share of each bucket is drawn as a
    second bar stacked on top in a contrasting colour, so an incident reads as
    "traffic steady, errors climbing" instead of one flat line.
    """
    if not values:
        return '<svg width="%d" height="%d"></svg>' % (width, height)
    count = len(values)
    peak = max(values) or 1.0
    slot = width / float(count)
    gap = min(2.0, slot * 0.25)
    bars = []
    for index, value in enumerate(values):
        total_h = max(1.0, (value / peak) * (height - 4))
        x = index * slot + gap / 2
        bar_w = max(1.0, slot - gap)
        base_y = height - 2
        errors = float(error_values[index]) if error_values and index < len(error_values) else 0.0
        error_h = total_h * min(1.0, errors / value) if value else 0.0
        normal_h = total_h - error_h
        if normal_h > 0:
            bars.append(
                '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s" opacity="0.75"/>'
                % (x, base_y - total_h, bar_w, normal_h, bar_color)
            )
        if error_h > 0:
            bars.append(
                '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s"/>'
                % (x, base_y - error_h, bar_w, error_h, error_color)
            )
    return (
        '<svg viewBox="0 0 %d %d" width="100%%" height="%d" preserveAspectRatio="none" '
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="events per bucket">'
        "%s</svg>" % (width, height, height, "".join(bars))
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _cluster_svg(cluster: Cluster, analysis: Analysis, width: int = 220) -> str:
    buckets = sorted(
        (key, count) for key, count in cluster.buckets(analysis.bucket_seconds).items() if key
    )
    if len(buckets) < 2:
        return ""
    color = "#ff6b6b" if cluster.severity in ("ERROR", "CRITICAL") else "#60a5fa"
    return svg_sparkline([float(v) for _, v in buckets], width=width, height=34, color=color)


def _render_incident_html(incident: Incident) -> str:
    sev = "sev1" if incident.severity_label == "SEV-1" else (
        "sev2" if incident.severity_label == "SEV-2" else "sev3")
    state = ('<span class="badge bad">STILL OPEN</span>' if incident.still_open
             else '<span class="badge ok">recovered</span>')
    window = "unknown"
    if incident.start and incident.end:
        window = "%s &rarr; %s (%s)" % (
            incident.start.strftime("%Y-%m-%d %H:%M:%S"),
            incident.end.strftime("%H:%M:%S"),
            _human_duration(incident.duration_seconds),
        )
    causes = []
    for index, cause in enumerate(incident.root_causes[:4], 1):
        reasons = "".join('<div class="why">&#183; %s</div>' % _esc(r) for r in cause.reasons[:3])
        evidence = "".join(
            '<p class="ev mono"><b>&gt;</b> %s</p>' % _esc(line) for line in cause.evidence[:2]
        )
        causes.append(
            '<div class="cause%s">'
            '<div><b>cause %d</b> &nbsp;<span class="tmpl mono">%s</span> '
            '&nbsp;<span class="badge %s">score %.1f</span></div>%s%s</div>'
            % (" first" if index == 1 else "", index, _esc(_short(cause.template, 110)),
               "sev1" if cause.severity == "CRITICAL" else "sev2", cause.score,
               reasons, evidence)
        )
    kinds = sorted({anomaly.kind for anomaly in incident.anomalies})
    return (
        '<div class="panel">'
        '<h3><span class="badge %s">%s</span> &nbsp;%s &nbsp;%s</h3>'
        '<div class="row"><span>%s</span><span>peak %s &#183; %d events</span>'
        '<span>%d errors</span><span>confidence %.0f</span><span>signals: %s</span></div>%s'
        "</div>"
        % (
            sev, incident.severity_label, _esc(incident.title), state, window,
            incident.peak.strftime("%H:%M:%S") if incident.peak else "-",
            incident.peak_events, incident.errors, incident.confidence,
            _esc(", ".join(kinds)), "".join(causes),
        )
    )


def html_report(analysis: Analysis, top: int = 10, title: str = "logscope report") -> str:
    """Build a single-file dark HTML report; returns the document as a string."""
    hist = list(analysis.histogram().values())
    cards = [
        ("events", _num(analysis.events), ""),
        ("templates", _num(analysis.template_count), ""),
        ("errors", _num(analysis.errors), "bad" if analysis.errors else "good"),
        ("error rate", _pct(analysis.errors, analysis.events),
         "bad" if analysis.error_rate >= 0.05 else ""),
        ("anomalies", _num(len(analysis.anomalies)), ""),
        ("incidents", _num(len(analysis.incidents)),
         "warn" if analysis.incidents else "good"),
        ("span", _human_duration(analysis.span_seconds), ""),
        ("parsed", _pct(analysis.stats.parsed, analysis.stats.lines), ""),
    ]
    card_html = "".join(
        '<div class="card %s"><div class="k">%s</div><div class="v">%s</div></div>'
        % (cls, _esc(key), _esc(value))
        for key, value, cls in cards
    )

    template_rows = []
    for cluster in sorted(analysis.clusters, key=lambda c: (-c.count, c.cluster_id))[:top]:
        level_badge = "sev1" if cluster.severity in ("ERROR", "CRITICAL") else (
            "sev2" if cluster.severity == "WARNING" else "sev3")
        samples = "".join('<p class="ev mono">&#183; %s</p>' % _esc(s)
                          for s in cluster.samples[:1])
        template_rows.append(
            '<tr><td class="num">%s</td><td class="num">%s</td>'
            '<td><span class="badge %s">%s</span></td>'
            '<td class="tmpl mono">%s%s</td><td>%s</td></tr>'
            % (
                _num(cluster.count), _num(cluster.error_count), level_badge,
                _esc(cluster.severity), _esc(_short(cluster.template, 96)), samples,
                _cluster_svg(cluster, analysis),
            )
        )

    incidents_html = "".join(
        _render_incident_html(incident) for incident in analysis.incidents[:top]
    ) or '<div class="panel"><b>No incidents detected.</b> Every template stayed inside its baseline.</div>'

    rows = analysis.timeline_rows()[-40:]
    peak = max((row["events"] for row in rows), default=1) or 1
    timeline_rows = "".join(
        '<tr><td class="mono">%s</td><td class="num">%s</td><td class="num">%s</td>'
        '<td><div class="bar" style="width:%.1f%%"></div></td>'
        '<td class="tmpl mono">%s</td></tr>'
        % (
            row["bucket"].strftime("%m-%d %H:%M"), _num(float(row["events"])),
            _num(float(row["errors"])), 100.0 * float(row["events"]) / peak,
            _esc(_short(str(row["top_template"]), 78)),
        )
        for row in rows
    )

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title><style>%(css)s</style></head>
<body><div class="wrap">
<h1>logscope</h1>
<p class="sub">%(origin)s &nbsp;&#183;&nbsp; %(events)s events &nbsp;&#183;&nbsp; format %(fmt)s
 &nbsp;&#183;&nbsp; bucket %(bucket)s &nbsp;&#183;&nbsp; %(span)s &nbsp;&#183;&nbsp; parsed %(parsed)s</p>
<div class="cards">%(cards)s</div>
<h2>Volume across the run <span class="n">per %(bucket)s bucket, gaps filled with zero</span></h2>
<div class="panel">%(spark)s
<div class="row"><span>peak %(peak)s events/bucket</span><span>%(buckets)d buckets</span>
<span>first %(first)s</span><span>last %(last)s</span></div></div>
<h2>Incidents <span class="n">%(inc_count)d ranked, grouped by time + template prefix</span></h2>
%(incidents)s
<h2>Top templates <span class="n">drain-style clusters, &lt;*&gt; = masked variable</span></h2>
<div class="panel"><table><thead><tr><th class="num">count</th><th class="num">errors</th>
<th>level</th><th>template</th><th>volume shape</th></tr></thead><tbody>%(templates)s</tbody></table></div>
<h2>Timeline <span class="n">last %(nrows)d buckets</span></h2>
<div class="panel"><table><thead><tr><th>bucket</th><th class="num">events</th>
<th class="num">errors</th><th>relative volume</th><th>leading template</th></tr></thead>
<tbody>%(timeline)s</tbody></table></div>
<p class="foot">Generated by logscope %(version)s -- offline, zero dependencies, deterministic.
Every number above comes from the log you passed in; no template, sample or figure is synthesised.</p>
</div></body></html>""" % {
        "title": _esc(title),
        "css": _CSS,
        "origin": _esc(analysis.origin),
        "events": _num(analysis.events),
        "fmt": _esc(analysis.fmt),
        "bucket": _esc(_bucket_label(analysis.bucket_seconds)),
        "span": _human_duration(analysis.span_seconds),
        "parsed": _pct(analysis.stats.parsed, analysis.stats.lines),
        "cards": card_html,
        "spark": svg_bars(
            hist,
            width=1100,
            height=96,
            error_values=[analysis.error_histogram().get(key, 0) for key in analysis.histogram()],
        ),
        "peak": _num(max(hist) if hist else 0),
        "buckets": len(hist),
        "first": _esc(analysis.span_start.strftime("%Y-%m-%d %H:%M:%S")
                      if analysis.span_start else "-"),
        "last": _esc(analysis.span_end.strftime("%Y-%m-%d %H:%M:%S")
                     if analysis.span_end else "-"),
        "inc_count": len(analysis.incidents),
        "incidents": incidents_html,
        "templates": "".join(template_rows),
        "timeline": timeline_rows,
        "nrows": len(rows),
        "version": _esc(__import__("logscope").__version__),
    }
