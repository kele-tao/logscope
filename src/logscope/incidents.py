"""Group anomalies into ranked incidents with root-cause candidates.

An incident is a set of anomalies that are close in time and share a template
prefix -- the log's own way of saying "these things broke together".  For each
incident we keep a timeline (start, peak, end, still-open) and rank the
templates most likely to be the cause, each with the evidence line that proves
it was there.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass, field

from logscope.detect import Anomaly
from logscope.formats import LEVEL_ORDER
from logscope.miner import Cluster

__all__ = ["Incident", "RootCause", "group_incidents", "IncidentConfig"]

BUCKET = _dt.timedelta(minutes=1)


@dataclass(slots=True)
class IncidentConfig:
    """Grouping policy for incidents."""

    proximity_buckets: int = 3
    prefix_tokens: int = 3
    keep_open_buckets: int = 2
    max_root_causes: int = 5
    min_anomalies: int = 1

    def validate(self) -> None:
        if self.proximity_buckets < 1:
            raise ValueError("proximity_buckets must be >= 1")
        if self.prefix_tokens < 1:
            raise ValueError("prefix_tokens must be >= 1")
        if self.max_root_causes < 1:
            raise ValueError("max_root_causes must be >= 1")


@dataclass(slots=True)
class RootCause:
    """One ranked cause candidate with the evidence that supports it."""

    template: str
    cluster_id: int
    score: float
    events: int
    level: str
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        return "CRITICAL" if self.level in ("ERROR", "CRITICAL") else "WARNING"

    def as_dict(self) -> dict[str, object]:
        return {
            "template": self.template,
            "cluster_id": self.cluster_id,
            "score": round(self.score, 2),
            "events": self.events,
            "level": self.level,
            "severity": self.severity,
            "reasons": list(self.reasons),
            "evidence": list(self.evidence),
            "kinds": list(self.kinds),
        }


@dataclass(slots=True)
class Incident:
    """A time-localised group of anomalies and its ranked causes."""

    incident_id: int
    anomalies: list[Anomaly]
    root_causes: list[RootCause] = field(default_factory=list)
    start: _dt.datetime | None = None
    end: _dt.datetime | None = None
    peak: _dt.datetime | None = None
    peak_events: int = 0
    events: int = 0
    errors: int = 0
    still_open: bool = False
    confidence: float = 0.0

    @property
    def title(self) -> str:
        """A one-line name a human can paste into a ticket."""
        if not self.anomalies:
            return "unnamed incident"
        lead = self.root_causes[0] if self.root_causes else None
        if lead is not None:
            label = _shorten(lead.template, 68)
            if "new-errors" in lead.kinds:
                return "new error class: %s" % label
            if "error-burst" in lead.kinds:
                return "error burst: %s" % label
            if "burst" in lead.kinds:
                return "sustained burst: %s" % label
            return "rate spike: %s" % label
        return _shorten(self.anomalies[0].template, 68)

    @property
    def severity(self) -> str:
        if any(a.severity == "CRITICAL" for a in self.anomalies):
            return "CRITICAL"
        return "WARNING"

    @property
    def severity_label(self) -> str:
        if self.severity == "CRITICAL":
            return "SEV-1"
        if self.errors:
            return "SEV-2"
        return "SEV-3"

    @property
    def duration_seconds(self) -> float:
        if not self.start or not self.end:
            return 0.0
        return max(0.0, (self.end - self.start).total_seconds())

    @property
    def templates(self) -> list[str]:
        seen: list[str] = []
        for anomaly in self.anomalies:
            if anomaly.template not in seen:
                seen.append(anomaly.template)
        return seen

    def summary(self) -> str:
        """Deterministic sentence describing the incident."""
        parts = [
            "%s: %s" % (self.severity_label, self.title),
            "%d anomalies across %d templates" % (len(self.anomalies), len(self.templates)),
        ]
        if self.start and self.end:
            parts.append(
                "%s -> %s (%s)"
                % (self.start.strftime("%H:%M:%S"), self.end.strftime("%H:%M:%S"),
                   _human_duration(self.duration_seconds))
            )
        if self.events:
            parts.append("%d events, %d errors" % (self.events, self.errors))
        if self.still_open:
            parts.append("still open at end of log")
        return " | ".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "severity": self.severity,
            "severity_label": self.severity_label,
            "start": self.start.isoformat(sep=" ") if self.start else None,
            "peak": self.peak.isoformat(sep=" ") if self.peak else None,
            "end": self.end.isoformat(sep=" ") if self.end else None,
            "still_open": self.still_open,
            "duration_seconds": round(self.duration_seconds, 3),
            "events": self.events,
            "errors": self.errors,
            "confidence": round(self.confidence, 2),
            "summary": self.summary(),
            "root_causes": [cause.as_dict() for cause in self.root_causes],
            "anomalies": [anomaly.as_dict() for anomaly in self.anomalies],
        }


_PREFIX_SPLIT = re.compile(r"[\s,;:=|/]+")


def _prefix(template: str, tokens: int) -> str:
    parts = [p for p in _PREFIX_SPLIT.split(template) if p and p != "<*>"]
    return " ".join(parts[:tokens]).lower()


def _shorten(text: str, width: int) -> str:
    text = text.strip()
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "\u2026"


def _human_duration(seconds: float) -> str:
    if seconds < 1:
        return "<1s"
    if seconds < 90:
        return "%ds" % round(seconds)
    if seconds < 5400:
        return "%dm" % round(seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)


def _gap(a: Anomaly, b: Anomaly, bucket_seconds: int) -> float:
    if a.start is None or b.start is None:
        return 0.0
    left = min(a.start, b.start)
    right = max(a.start, b.start)
    return (right - left).total_seconds()


def _joinable(a: Anomaly, group: list[Anomaly], cfg: IncidentConfig, bucket_seconds: int) -> bool:
    """An anomaly joins an incident when it is close in time and related.

    Two anomalies are related when they share a template prefix, are the same
    template, share any literal token, or are both error-class anomalies -- an
    outage throwing four different errors in the same three minutes is one
    incident with four symptoms, not four incidents.
    """
    window = cfg.proximity_buckets * bucket_seconds
    for member in group:
        if _gap(a, member, bucket_seconds) > window:
            continue
        if _prefix(a.template, cfg.prefix_tokens) == _prefix(member.template, cfg.prefix_tokens):
            return True
        if a.cluster_id == member.cluster_id:
            return True
        if set(a.template.split()) & set(member.template.split()):
            return True
        if a.severity == "CRITICAL" and member.severity == "CRITICAL":
            return True
    return False


def group_incidents(
    anomalies: list[Anomaly],
    clusters: list[Cluster],
    bucket_seconds: int = 60,
    config: IncidentConfig | None = None,
    span_end: _dt.datetime | None = None,
) -> list[Incident]:
    """Cluster anomalies into incidents, then rank each incident's causes."""
    cfg = config or IncidentConfig()
    cfg.validate()
    if not anomalies:
        return []
    by_id = {cluster.cluster_id: cluster for cluster in clusters}
    ordered = sorted(anomalies, key=lambda a: (a.start or _EPOCH, -a.score))
    groups: list[list[Anomaly]] = []
    for anomaly in ordered:
        placed = False
        for group in groups:
            if _joinable(anomaly, group, cfg, bucket_seconds):
                group.append(anomaly)
                placed = True
                break
        if not placed:
            groups.append([anomaly])

    incidents: list[Incident] = []
    for group in groups:
        incident = _build_incident(group, by_id, cfg, bucket_seconds, span_end)
        if len(incident.anomalies) >= cfg.min_anomalies:
            incidents.append(incident)
    incidents = _merge_overlapping(incidents, by_id, cfg, bucket_seconds, span_end)
    incidents.sort(key=lambda i: (i.start or _EPOCH, -i.confidence))
    for index, incident in enumerate(incidents, 1):
        incident.incident_id = index
    return incidents


def _merge_overlapping(
    incidents: list[Incident],
    clusters: dict[int, Cluster],
    cfg: IncidentConfig,
    bucket_seconds: int,
    span_end: _dt.datetime | None,
) -> list[Incident]:
    """Merge incidents whose active windows overlap.

    One outage produces several symptom families at once -- 500s, timeouts and a
    retry storm interleaved minute by minute. Reporting them as separate
    incidents would hide the fact that they are the same event, so overlapping
    windows collapse into one incident with several ranked causes.
    """
    merged = True
    while merged:
        merged = False
        incidents.sort(key=lambda i: (i.start or _EPOCH, i.incident_id))
        for index in range(len(incidents)):
            for other in range(index + 1, len(incidents)):
                left = incidents[index]
                right = incidents[other]
                if not _windows_overlap(left, right):
                    continue
                combined = _build_incident(
                    left.anomalies + right.anomalies, clusters, cfg, bucket_seconds, span_end
                )
                incidents[index] = combined
                del incidents[other]
                merged = True
                break
            if merged:
                break
    incidents.sort(key=lambda i: (i.start or _EPOCH, -i.confidence))
    for number, incident in enumerate(incidents, 1):
        incident.incident_id = number
    return incidents


def _floor(value: _dt.datetime, bucket_seconds: int) -> _dt.datetime:
    epoch = int(value.timestamp())
    return _dt.datetime.fromtimestamp(epoch - epoch % bucket_seconds)


def _windows_overlap(left: Incident, right: Incident) -> bool:
    if left.start is None or right.start is None:
        return False
    left_end = left.end or left.start
    right_end = right.end or right.start
    return left.start <= right_end and right.start <= left_end


def _build_incident(
    group: list[Anomaly],
    clusters: dict[int, Cluster],
    cfg: IncidentConfig,
    bucket_seconds: int,
    span_end: _dt.datetime | None,
) -> Incident:
    starts = [a.start for a in group if a.start]
    ends = [a.end for a in group if a.end]
    start = min(starts) if starts else None
    end = max(ends) if ends else None
    peak = None
    peak_events = 0
    events = 0
    errors = 0
    by_bucket: dict[_dt.datetime, int] = {}
    for anomaly in group:
        cluster = clusters.get(anomaly.cluster_id)
        weight = cluster.count if cluster else anomaly.count
        local = max(1, anomaly.count)
        events += weight
        if cluster is not None:
            errors += cluster.error_count
        if anomaly.start is not None:
            by_bucket[anomaly.start] = by_bucket.get(anomaly.start, 0) + local
    if by_bucket:
        peak = max(by_bucket.items(), key=lambda kv: (kv[1], -kv[0].timestamp()))[0]
        peak_events = by_bucket[peak]
    still_open = False
    if span_end is not None and end is not None:
        # "Open" means the incident is still producing anomalies in the final
        # bucket of the log. Anything that stopped earlier has recovered, even
        # if it stopped only a minute before EOF.
        final_bucket = _floor(span_end, bucket_seconds)
        incident_bucket = _floor(end, bucket_seconds)
        still_open = incident_bucket >= final_bucket
    incident = Incident(
        incident_id=0,
        anomalies=list(group),
        start=start,
        end=end,
        peak=peak,
        peak_events=peak_events,
        events=events,
        errors=errors,
        still_open=still_open,
    )
    incident.root_causes = _rank_causes(group, clusters, start, cfg)
    incident.confidence = _confidence(incident, group)
    return incident


def _rank_causes(
    group: list[Anomaly],
    clusters: dict[int, Cluster],
    start: _dt.datetime | None,
    cfg: IncidentConfig,
) -> list[RootCause]:
    """Rank candidate causes, severity first.

    Adding raw anomaly scores together is wrong: a z-score of 20 and a count of
    1000 are not the same unit, and a chatty warning template would beat the
    error class that actually broke. Instead each candidate earns points from a
    small, explicit rubric, and errors outrank warnings by construction.
    """
    agg: dict[int, dict[str, object]] = {}
    for anomaly in group:
        entry = agg.setdefault(
            anomaly.cluster_id,
            {"kinds": [], "reasons": [], "anomalies": [], "level": "INFO", "score": 0.0},
        )
        kinds = entry["kinds"]
        if isinstance(kinds, list) and anomaly.kind not in kinds:
            kinds.append(anomaly.kind)
        anomalies = entry["anomalies"]
        if isinstance(anomalies, list):
            anomalies.append(anomaly)
        level = str(entry["level"])
        if LEVEL_ORDER.get(anomaly.level, 1) > LEVEL_ORDER.get(level, 1):
            entry["level"] = anomaly.level
        entry["score"] = max(float(entry["score"]), float(anomaly.score))

    causes: list[RootCause] = []
    for cluster_id, entry in agg.items():
        cluster = clusters.get(cluster_id)
        if cluster is None:
            continue
        kinds = [str(k) for k in entry["kinds"]] if isinstance(entry["kinds"], list) else []
        critical = cluster.severity in ("ERROR", "CRITICAL")
        score = 3.0 if critical else 0.5
        reasons: list[str] = []
        if "new-errors" in kinds:
            score += 6.0
            reasons.append("new error class -- this template never fired before the incident")
        elif "new" in kinds:
            score += 2.0 if critical else 0.5
            reasons.append("new template -- first seen during the incident window")
        if "error-burst" in kinds:
            score += 3.0
            reasons.append("errors concentrated into one bucket (burst)")
        if "spike" in kinds:
            peak = float(entry["score"])
            bonus = min(2.5, max(0.0, (peak - 3.0) / 4.0))
            score += bonus
            if critical:
                reasons.append("error volume rose sharply against its own baseline")
            else:
                reasons.append("rate stepped up against its own baseline (z=%.1f)" % peak)
        if "burst" in kinds:
            score += 1.5
            reasons.append("trailing volume far above the file average")
        firsts = [a.start for a in entry["anomalies"] if a.start]  # type: ignore[union-attr]
        if firsts and start is not None and min(firsts) - start <= BUCKET:
            score += 1.5
            reasons.append("first mover -- fired within a minute of the incident opening")
        if cluster.count:
            share = cluster.error_count / cluster.count
            score += 3.0 * share
            if share >= 0.5:
                reasons.append("%.0f%% of its events are errors" % (100 * share))
        score += 2.0 * math.log10(cluster.count + 1)
        reasons.append("%d events total, %d errors" % (cluster.count, cluster.error_count))
        causes.append(
            RootCause(
                template=cluster.template,
                cluster_id=cluster_id,
                score=score,
                events=cluster.count,
                level=cluster.severity,
                reasons=reasons,
                evidence=list(cluster.evidence[:3]),
                kinds=kinds,
            )
        )
    causes.sort(key=lambda c: (-c.score, -c.events, c.template))
    return causes[: cfg.max_root_causes]


def _confidence(incident: Incident, group: list[Anomaly]) -> float:
    """0-100 score: how much the evidence agrees that this is one story."""
    score = 20.0
    kinds = {anomaly.kind for anomaly in group}
    score += 12.0 * min(4, len(kinds))
    if incident.root_causes:
        top = incident.root_causes[0]
        if top.level in ("ERROR", "CRITICAL"):
            score += 15.0
        if top.evidence:
            score += 10.0
        if len(incident.root_causes) > 1 and incident.root_causes[1].score > 0:
            runner_up = incident.root_causes[1].score
            if runner_up:
                score += 12.0 * min(1.0, top.score / (top.score + runner_up))
    score += min(18.0, 3.0 * len(group))
    if incident.still_open:
        score += 6.0
    return round(min(99.0, score), 1)


_EPOCH = _dt.datetime(1970, 1, 1)
