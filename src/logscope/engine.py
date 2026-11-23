"""The analysis pipeline: stream -> mine -> detect -> group.

This is the one function every CLI subcommand funnels through, so the API, the
report and the tests all see exactly the same numbers.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from logscope.detect import Anomaly, Detector, DetectorConfig, estimate_bucket
from logscope.formats import Event, ParseStats, Reader, open_events
from logscope.incidents import Incident, IncidentConfig, group_incidents
from logscope.miner import Cluster, Miner

__all__ = ["Analysis", "AnalysisConfig", "analyze", "analyze_lines", "TimeFilter", "parse_time"]


@dataclass(slots=True)
class AnalysisConfig:
    """Everything the pipeline needs, shared by the CLI and the API."""

    fmt: str = "auto"
    bucket: int = 0
    threshold: float = 0.7
    since: _dt.datetime | None = None
    until: _dt.datetime | None = None
    since_tod: bool = False
    until_tod: bool = False
    grep: str | None = None
    invert: bool = False
    min_level: str | None = None
    top: int = 10
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    incidents: IncidentConfig = field(default_factory=IncidentConfig)

    def make_regex(self) -> re.Pattern[str] | None:
        if not self.grep:
            return None
        try:
            return re.compile(self.grep, re.IGNORECASE)
        except re.error as exc:
            raise ValueError("invalid --grep pattern %r: %s" % (self.grep, exc)) from exc

    def validate(self) -> None:
        from logscope.formats import FORMATS

        if self.fmt not in FORMATS + ("auto",):
            raise ValueError(
                "unknown --format %r (choose from %s)" % (self.fmt, ", ".join(FORMATS + ("auto",)))
            )
        if self.bucket < 0:
            raise ValueError("--bucket must be positive")
        if self.top < 1:
            raise ValueError("--top must be >= 1")
        if self.since and self.until and not (self.since_tod or self.until_tod) \
                and self.since > self.until:
            raise ValueError("--since is after --until; check the timestamps")
        self.detector.validate()
        self.incidents.validate()


@dataclass(slots=True)
class TimeFilter:
    """Post-parse filter shared by every read path."""

    regex: re.Pattern[str] | None = None
    invert: bool = False
    since: _dt.datetime | None = None
    until: _dt.datetime | None = None
    since_tod: bool = False
    until_tod: bool = False
    min_level: str | None = None

    def accepts(self, event: Event) -> bool:
        if self.min_level is not None:
            from logscope.formats import LEVEL_ORDER

            if LEVEL_ORDER.get(event.level, 1) < LEVEL_ORDER.get(self.min_level, 1):
                return False
        if event.ts is not None:
            if self.since is not None:
                floor = self.since.time() if self.since_tod else self.since
                value = event.ts.time() if self.since_tod else event.ts
                if value < floor:
                    return False
            if self.until is not None:
                ceiling = self.until.time() if self.until_tod else self.until
                value = event.ts.time() if self.until_tod else event.ts
                if value > ceiling:
                    return False
        if self.regex is not None:
            matched = bool(self.regex.search(event.raw) or self.regex.search(event.message))
            if matched == self.invert:
                return False
        return True


@dataclass(slots=True)
class Analysis:
    """The full result of one run -- everything the renderers need."""

    clusters: list[Cluster]
    anomalies: list[Anomaly]
    incidents: list[Incident]
    stats: ParseStats
    fmt: str
    bucket_seconds: int
    span_start: _dt.datetime | None
    span_end: _dt.datetime | None
    events: int
    errors: int
    warnings: int
    levels: dict[str, int]
    origin: str
    duration_s: float = 0.0
    peak_per_bucket: int = 0

    # -- derived -----------------------------------------------------------
    @property
    def template_count(self) -> int:
        return len(self.clusters)

    @property
    def error_rate(self) -> float:
        return (self.errors / self.events) if self.events else 0.0

    @property
    def span_seconds(self) -> float:
        if not self.span_start or not self.span_end:
            return 0.0
        return (self.span_end - self.span_start).total_seconds()

    def histogram(self) -> dict[_dt.datetime, int]:
        """Total accepted events per bucket, gaps filled with zeros."""
        agg: dict[_dt.datetime, int] = {}
        for cluster in self.clusters:
            for key, count in cluster.buckets(self.bucket_seconds).items():
                if key is None:
                    continue
                agg[key] = agg.get(key, 0) + count
        if not agg:
            return {}
        return fill_gaps(agg, self.bucket_seconds)

    def error_histogram(self) -> dict[_dt.datetime, int]:
        """Errors per bucket, for the report's error-rate strip."""
        agg: dict[_dt.datetime, int] = {}
        for cluster in self.clusters:
            if cluster.severity not in ("ERROR", "CRITICAL"):
                continue
            for key, count in cluster.buckets(self.bucket_seconds).items():
                if key is None:
                    continue
                agg[key] = agg.get(key, 0) + count
        if not agg:
            return {}
        return fill_gaps(agg, self.bucket_seconds)

    def timeline_rows(self, limit: int | None = None) -> list[dict[str, object]]:
        """Per-bucket rows: events, errors, new templates, top template."""
        hist = self.histogram()
        errors = self.error_histogram()
        firsts: dict[_dt.datetime, list[str]] = {}
        leaders: dict[_dt.datetime, tuple[str, int]] = {}
        for cluster in self.clusters:
            for key, count in cluster.buckets(self.bucket_seconds).items():
                if key is None:
                    continue
                current = leaders.get(key)
                if current is None or count > current[1]:
                    leaders[key] = (cluster.template, count)
            if cluster.first_seen is not None:
                bucket = self._floor(cluster.first_seen)
                firsts.setdefault(bucket, []).append(cluster.template)
        rows = []
        for bucket, count in hist.items():
            top = leaders.get(bucket)
            rows.append(
                {
                    "bucket": bucket,
                    "events": count,
                    "errors": errors.get(bucket, 0),
                    "new_templates": len(firsts.get(bucket, [])),
                    "top_template": top[0] if top else "",
                    "top_count": top[1] if top else 0,
                }
            )
        if limit:
            rows = rows[-limit:]
        return rows

    def _floor(self, ts: _dt.datetime) -> _dt.datetime:
        epoch = int(ts.timestamp())
        return _dt.datetime.fromtimestamp(epoch - epoch % self.bucket_seconds)

    def to_dict(self, top: int = 10) -> dict[str, object]:
        """JSON-safe snapshot of the whole analysis."""
        return {
            "tool": "logscope",
            "origin": self.origin,
            "format": self.fmt,
            "bucket_seconds": self.bucket_seconds,
            "duration_s": round(self.duration_s, 4),
            "events": self.events,
            "errors": self.errors,
            "warnings": self.warnings,
            "error_rate": round(self.error_rate, 6),
            "levels": dict(sorted(self.levels.items())),
            "span": {
                "start": self.span_start.isoformat(sep=" ") if self.span_start else None,
                "end": self.span_end.isoformat(sep=" ") if self.span_end else None,
                "seconds": round(self.span_seconds, 3),
            },
            "parse": {
                "lines": self.stats.lines,
                "parsed": self.stats.parsed,
                "timestamped": self.stats.timestamped,
                "by_format": dict(self.stats.by_format),
            },
            "templates": [cluster.as_dict(self.bucket_seconds)
                          for cluster in sorted(self.clusters, key=lambda c: (-c.count, c.cluster_id))[:top]],
            "anomalies": [anomaly.as_dict() for anomaly in self.anomalies[:top]],
            "incidents": [incident.as_dict() for incident in self.incidents[:top]],
            "timeline": [
                {
                    "bucket": row["bucket"].isoformat(sep=" "),
                    "events": row["events"],
                    "errors": row["errors"],
                    "new_templates": row["new_templates"],
                    "top_template": row["top_template"],
                }
                for row in self.timeline_rows()
            ],
        }


def fill_gaps(series: dict[_dt.datetime, int], bucket_seconds: int) -> dict[_dt.datetime, int]:
    """Insert zero buckets so every sparkline shares the same x axis."""
    if not series:
        return {}
    keys = sorted(series)
    filled: dict[_dt.datetime, int] = {}
    current = keys[0]
    last = keys[-1]
    step = _dt.timedelta(seconds=bucket_seconds)
    while current <= last:
        filled[current] = series.get(current, 0)
        current += step
    return filled


def parse_time_spec(value: str, now: _dt.datetime | None = None) -> tuple[_dt.datetime, bool]:
    """Like parse_time, but also says whether the value was a bare time-of-day.

    "09:12" means "the 09:12 in this log", not "09:12 today": logs are usually
    analysed long after they were written, so a time-of-day filter compares
    against the timestamp's clock face rather than its date.
    """
    text = value.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        today = now or _dt.datetime.now()
        return parsed.replace(year=today.year, month=today.month, day=today.day), True
    return parse_time(value, now), False


def parse_time(value: str, now: _dt.datetime | None = None) -> _dt.datetime:
    """Parse --since/--until values: ISO 8601 or a relative offset like 15m/2h/1d."""
    text = value.strip()
    if not text:
        raise ValueError("empty time value")
    match = re.fullmatch(r"(\d+)([smhd])", text, re.IGNORECASE)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta = {
            "s": _dt.timedelta(seconds=amount),
            "m": _dt.timedelta(minutes=amount),
            "h": _dt.timedelta(hours=amount),
            "d": _dt.timedelta(days=amount),
        }[unit]
        return (now or _dt.datetime.now()) - delta
    cleaned = text.replace("/", "-").replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M:%S", "%H:%M"):
        try:
            parsed = _dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if fmt in ("%H:%M:%S", "%H:%M"):
            today = now or _dt.datetime.now()
            parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
        return parsed
    raise ValueError(
        "cannot parse time %r (use 2026-05-12T09:30:00, 2026-05-12 09:30, or 15m/2h/1d)" % value
    )


def _pipeline(
    reader: Reader,
    source: str,
    config: AnalysisConfig,
    started: float,
) -> Analysis:
    import time

    miner = Miner(threshold=config.threshold)
    time_filter = TimeFilter(
        regex=config.make_regex(),
        invert=config.invert,
        since=config.since,
        until=config.until,
        since_tod=config.since_tod,
        until_tod=config.until_tod,
        min_level=config.min_level,
    )
    events = 0
    errors = 0
    warnings = 0
    levels: dict[str, int] = {}
    for event in reader:
        if not time_filter.accepts(event):
            continue
        events += 1
        levels[event.level] = levels.get(event.level, 0) + 1
        if event.level in ("ERROR", "CRITICAL"):
            errors += 1
        elif event.level == "WARNING":
            warnings += 1
        miner.add(event)

    span_start, span_end = _span(miner.clusters)
    bucket_seconds = config.bucket or estimate_bucket(
        (span_end - span_start).total_seconds() / 60.0 if span_start and span_end else 0.0
    )
    detector = Detector(config.detector)
    anomalies = detector.detect(miner.clusters, bucket_seconds)
    incidents = group_incidents(
        anomalies, miner.clusters, bucket_seconds, config.incidents, span_end
    )
    peak = 0
    hist: dict[_dt.datetime, int] = {}
    for cluster in miner.clusters:
        for key, count in cluster.buckets(bucket_seconds).items():
            if key is not None:
                hist[key] = hist.get(key, 0) + count
    if hist:
        peak = max(hist.values())
    return Analysis(
        clusters=miner.clusters,
        anomalies=anomalies,
        incidents=incidents,
        stats=reader.stats,
        fmt=reader.format,
        bucket_seconds=bucket_seconds,
        span_start=span_start,
        span_end=span_end,
        events=events,
        errors=errors,
        warnings=warnings,
        levels=levels,
        origin=source,
        duration_s=time.perf_counter() - started,
        peak_per_bucket=peak,
    )


def _span(clusters: Iterable[Cluster]) -> tuple[_dt.datetime | None, _dt.datetime | None]:
    start = None
    end = None
    for cluster in clusters:
        if cluster.first_seen and (start is None or cluster.first_seen < start):
            start = cluster.first_seen
        if cluster.last_seen and (end is None or cluster.last_seen > end):
            end = cluster.last_seen
    return start, end


def analyze(path: str, config: AnalysisConfig | None = None) -> Analysis:
    """Analyse a log file (or "-" for stdin) and return the full result."""
    import time

    cfg = config or AnalysisConfig()
    cfg.validate()
    started = time.perf_counter()
    reader = open_events(path, fmt=cfg.fmt)
    try:
        return _pipeline(reader, path, cfg, started)
    finally:
        if path != "-":
            reader.stream.close()


def analyze_lines(lines: Iterable[str], config: AnalysisConfig | None = None) -> Analysis:
    """Analyse an in-memory iterable of lines -- used by tests and the API."""
    import io
    import time

    cfg = config or AnalysisConfig()
    cfg.validate()
    started = time.perf_counter()
    reader = Reader(io.StringIO("".join(lines)), fmt=cfg.fmt, origin="<memory>")
    return _pipeline(reader, "<memory>", cfg, started)


def iter_events(path: str, config: AnalysisConfig | None = None) -> Iterator[Event]:
    """Yield accepted events one at a time (used by the timeline command)."""
    cfg = config or AnalysisConfig()
    cfg.validate()
    reader = open_events(path, fmt=cfg.fmt)
    time_filter = TimeFilter(
        regex=cfg.make_regex(),
        invert=cfg.invert,
        since=cfg.since,
        until=cfg.until,
        since_tod=cfg.since_tod,
        until_tod=cfg.until_tod,
        min_level=cfg.min_level,
    )
    try:
        for event in reader:
            if time_filter.accepts(event):
                yield event
    finally:
        if path != "-":
            reader.stream.close()
