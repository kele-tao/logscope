"""Anomaly detection over mined templates.

Three independent detectors run over the per-minute histogram of every template:

rate     EWMA + robust z-score on the minute series.  The first BOOTSTRAP
         buckets only train the baseline; after that a bucket that is both
         above the EWMA and a z-score away from the historical spread fires.
burst    the trailing window is far above the whole-file average, catching
         storms that never look unusual per-minute but dominate the tail.
new      a template whose first occurrence lands after the warm-up window and
         carries WARNING or worse -- the classic "error class we never saw".

Every anomaly carries a one-line human explanation; there are no bare numbers.
"""

from __future__ import annotations

import datetime as _dt
import math
import statistics
from dataclasses import dataclass, field

from logscope.miner import Cluster

__all__ = [
    "Anomaly",
    "Detector",
    "DetectorConfig",
    "KINDS",
    "BUCKET_SIZES",
    "estimate_bucket",
]

#: Detector names, stable enough to filter on from the CLI (--kind).
KINDS = ("spike", "burst", "new", "new-errors", "first-seen", "error-burst")

BOOTSTRAP_BUCKETS = 5
Z_THRESHOLD = 3.0


@dataclass(slots=True)
class DetectorConfig:
    """Knobs for the detectors; all thresholds documented in the README."""

    z_threshold: float = Z_THRESHOLD
    ewma_alpha: float = 0.3
    min_count: int = 3
    min_ratio: float = 2.0
    burst_factor: float = 4.0
    bootstrap: int = BOOTSTRAP_BUCKETS
    quiet_gap_buckets: int = 10
    error_share: float = 0.5

    def validate(self) -> None:
        if self.z_threshold <= 0:
            raise ValueError("z_threshold must be > 0")
        if not 0.0 < self.ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0, 1]")
        if self.min_count < 1:
            raise ValueError("min_count must be >= 1")
        if self.bootstrap < 1:
            raise ValueError("bootstrap must be >= 1")


@dataclass(slots=True)
class Anomaly:
    """One detected anomaly with its evidence and explanation."""

    kind: str
    template: str
    bucket: _dt.datetime | None
    observed: float
    expected: float
    score: float
    count: int
    total: int
    level: str
    explanation: str
    evidence: list[str] = field(default_factory=list)
    cluster_id: int = 0
    start: _dt.datetime | None = None
    end: _dt.datetime | None = None

    @property
    def severity(self) -> str:
        """CRITICAL for error-class events, WARNING for rate-only surprises."""
        if self.kind in ("new-errors", "error-burst"):
            return "CRITICAL"
        if self.level in ("ERROR", "CRITICAL"):
            return "CRITICAL"
        return "WARNING"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "template": self.template,
            "severity": self.severity,
            "level": self.level,
            "bucket": self.bucket.isoformat(sep=" ") if self.bucket else None,
            "start": self.start.isoformat(sep=" ") if self.start else None,
            "end": self.end.isoformat(sep=" ") if self.end else None,
            "count": self.count,
            "total": self.total,
            "observed": round(self.observed, 2),
            "expected": round(self.expected, 2),
            "score": round(self.score, 2),
            "cluster_id": self.cluster_id,
            "explanation": self.explanation,
            "evidence": list(self.evidence),
        }


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _fmt(x: float) -> str:
    if x >= 100:
        return "%.0f" % x
    if x >= 10:
        return "%.1f" % x
    return "%.2f" % x


#: Candidate bucket widths in seconds, from one minute to ten days.
BUCKET_SIZES = (60, 300, 600, 900, 1800, 3600, 7200, 18000, 36000, 108000,
                216000, 432000, 864000)


def estimate_bucket(minutes: float) -> int:
    """Pick a bucket size (seconds) that yields at most 400 buckets for the span."""
    if minutes <= 0:
        return BUCKET_SIZES[0]
    span = minutes * 60.0
    for size in BUCKET_SIZES:
        if span / size <= 400:
            return size
    return BUCKET_SIZES[-1]


class Detector:
    """Runs every detector over mined clusters."""

    def __init__(self, config: DetectorConfig | None = None):
        self.config = config or DetectorConfig()
        self.config.validate()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _series(cluster: Cluster, bucket_seconds: int) -> list[tuple[_dt.datetime, int]]:
        series = sorted(cluster.buckets(bucket_seconds).items(), key=lambda kv: kv[0] or 0)
        return [(key, count) for key, count in series if key is not None]

    def _span(self, clusters: list[Cluster]) -> tuple[_dt.datetime | None, _dt.datetime | None]:
        start = None
        end = None
        for cluster in clusters:
            if cluster.first_seen and (start is None or cluster.first_seen < start):
                start = cluster.first_seen
            if cluster.last_seen and (end is None or cluster.last_seen > end):
                end = cluster.last_seen
        return start, end

    # -- detectors ---------------------------------------------------------
    def detect_rate(self, cluster: Cluster, bucket_seconds: int) -> list[Anomaly]:
        """EWMA baseline plus robust z-score on the per-bucket series."""
        series = self._series(cluster, bucket_seconds)
        if len(series) <= self.config.bootstrap:
            return []
        cfg = self.config
        history = [float(count) for _, count in series[: cfg.bootstrap]]
        ewma = statistics.fmean(history)
        residuals: list[float] = []
        found: list[Anomaly] = []
        for bucket, count in series[cfg.bootstrap:]:
            spread = _quantile(residuals, 0.75) if residuals else 0.0
            mad = spread / 0.6745 if spread else 0.0
            scale = max(mad, math.sqrt(max(ewma, 1.0)), 0.5)
            z = (count - ewma) / scale
            ratio = count / max(ewma, 0.5)
            if count >= cfg.min_count and z >= cfg.z_threshold and ratio >= cfg.min_ratio:
                evidence = cluster.evidence[:2]
                found.append(
                    Anomaly(
                        kind="spike",
                        template=cluster.template,
                        bucket=bucket,
                        observed=float(count),
                        expected=ewma,
                        score=z,
                        count=cluster.count,
                        total=0,
                        level=cluster.severity,
                        explanation=(
                            "%s spiked to %d events in one %s bucket (baseline %s, z=%.1f), "
                            "%.1fx the usual rate"
                            % (cluster.template, count, _bucket_label(bucket_seconds),
                               _fmt(ewma), z, ratio)
                        ),
                        evidence=evidence,
                        cluster_id=cluster.cluster_id,
                        start=bucket,
                        end=bucket,
                    )
                )
            residuals.append(abs(count - ewma))
            ewma = cfg.ewma_alpha * count + (1.0 - cfg.ewma_alpha) * ewma
        return found

    def detect_burst(self, cluster: Cluster, bucket_seconds: int) -> list[Anomaly]:
        """Trailing window much hotter than the file-wide average."""
        series = self._series(cluster, bucket_seconds)
        if len(series) < max(6, self.config.bootstrap + 2):
            return []
        counts = [float(count) for _, count in series]
        average = statistics.fmean(counts)
        tail_len = max(2, len(counts) // 12)
        tail_index = len(counts) - tail_len
        tail = statistics.fmean(counts[tail_index:])
        if average <= 0 or tail <= 0:
            return []
        ratio = tail / average
        if ratio < self.config.burst_factor or tail < self.config.min_count:
            return []
        bucket = series[tail_index][0]
        return [
            Anomaly(
                kind="burst",
                template=cluster.template,
                bucket=bucket,
                observed=tail,
                expected=average,
                score=ratio,
                count=cluster.count,
                total=0,
                level=cluster.severity,
                explanation=(
                    "%s ran at %s events/%s over the last %d buckets -- %.1fx its "
                    "file-wide average of %s"
                    % (cluster.template, _fmt(tail), _bucket_label(bucket_seconds),
                       tail_len, ratio, _fmt(average))
                ),
                evidence=cluster.evidence[:2],
                cluster_id=cluster.cluster_id,
                start=bucket,
                end=series[-1][0],
            )
        ]

    def detect_new_errors(
        self,
        cluster: Cluster,
        bucket_seconds: int,
        span_start: _dt.datetime | None,
        span_end: _dt.datetime | None,
    ) -> list[Anomaly]:
        """A warning-or-worse template that only appears after the warm-up window."""
        if cluster.severity not in ("ERROR", "CRITICAL", "WARNING"):
            return []
        if span_start is None or span_end is None or cluster.first_seen is None:
            return []
        span = (span_end - span_start).total_seconds()
        if span <= 0:
            return []
        warmup = max(bucket_seconds * self.config.bootstrap, span * 0.05)
        offset = (cluster.first_seen - span_start).total_seconds()
        if offset < warmup:
            return []
        if cluster.count < max(2, self.config.min_count):
            return []
        share = cluster.count / max(1, self._events_seen)
        kind = "new-errors" if cluster.severity in ("ERROR", "CRITICAL") else "new"
        return [
            Anomaly(
                kind=kind,
                template=cluster.template,
                bucket=cluster.first_seen.replace(second=0, microsecond=0),
                observed=float(cluster.count),
                expected=0.0,
                score=float(cluster.count),
                count=cluster.count,
                total=0,
                level=cluster.severity,
                explanation=(
                    "new %s class first appeared %s into the log and fired %d times "
                    "(%.1f%% of all events, %.1f/min)"
                    % (cluster.severity.lower(), _human_offset(offset), cluster.count,
                       100.0 * share,
                       cluster.count / max(self._events_seen and (span / 60.0) or 1.0, 1e-9))
                ),
                evidence=cluster.evidence[:2],
                cluster_id=cluster.cluster_id,
                start=cluster.first_seen,
                end=cluster.last_seen,
            )
        ]

    def detect_error_burst(self, cluster: Cluster, bucket_seconds: int) -> list[Anomaly]:
        """Error volume in one bucket far above the template's own error average."""
        if cluster.severity not in ("ERROR", "CRITICAL"):
            return []
        series = self._series(cluster, bucket_seconds)
        if len(series) < 3:
            return []
        counts = [float(count) for _, count in series]
        average = statistics.fmean(counts)
        bucket, peak = max(series, key=lambda kv: kv[1])
        if average <= 0 or peak < self.config.min_count:
            return []
        ratio = peak / average
        if ratio < self.config.burst_factor:
            return []
        return [
            Anomaly(
                kind="error-burst",
                template=cluster.template,
                bucket=bucket,
                observed=float(peak),
                expected=average,
                score=ratio,
                count=cluster.count,
                total=0,
                level=cluster.severity,
                explanation=(
                    "%s errors concentrated: %d in one %s bucket, %.1fx the "
                    "template's average of %s"
                    % (cluster.severity.lower(), peak, _bucket_label(bucket_seconds),
                       ratio, _fmt(average))
                ),
                evidence=cluster.evidence[:2],
                cluster_id=cluster.cluster_id,
                start=bucket,
                end=bucket,
            )
        ]

    # -- driver ------------------------------------------------------------
    _events_seen = 1

    def detect(self, clusters: list[Cluster], bucket_seconds: int) -> list[Anomaly]:
        """Run every detector and return anomalies sorted by time then score."""
        self._events_seen = sum(cluster.count for cluster in clusters) or 1
        start, end = self._span(clusters)
        found: list[Anomaly] = []
        for cluster in clusters:
            found.extend(self.detect_rate(cluster, bucket_seconds))
            found.extend(self.detect_burst(cluster, bucket_seconds))
            found.extend(self.detect_error_burst(cluster, bucket_seconds))
            found.extend(self.detect_new_errors(cluster, bucket_seconds, start, end))
        for anomaly in found:
            anomaly.total = self._events_seen
        found.sort(key=lambda a: (a.start or _EPOCH, -a.score, a.template))
        return found


_EPOCH = _dt.datetime(1970, 1, 1)


def _bucket_label(bucket_seconds: int) -> str:
    if bucket_seconds % 3600 == 0:
        return "%dh" % (bucket_seconds // 3600)
    if bucket_seconds % 60 == 0:
        return "%dm" % (bucket_seconds // 60)
    return "%ds" % bucket_seconds


def _human_offset(seconds: float) -> str:
    if seconds < 90:
        return "%ds" % round(seconds)
    if seconds < 5400:
        return "%dm" % round(seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)
