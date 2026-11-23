"""Incident grouping, root-cause ranking and confidence."""

from __future__ import annotations

import datetime as _dt

import pytest

from logscope.detect import Anomaly
from logscope.incidents import Incident, IncidentConfig, group_incidents
from logscope.miner import Cluster


def _cluster(cluster_id: int, template: str, count: int, errors: int, level: str = "INFO") -> Cluster:
    cluster = Cluster(pattern=template.split(), cluster_id=cluster_id, count=count)
    cluster.error_count = errors
    cluster.levels = {level: count}
    cluster.first_seen = _dt.datetime(2026, 5, 12, 10, cluster_id, 0)
    cluster.last_seen = _dt.datetime(2026, 5, 12, 10, cluster_id, 59)
    cluster.evidence = ["raw line for " + template]
    return cluster


def _anomaly(kind: str, template: str, cluster_id: int, minute: int, score: float,
             level: str = "ERROR") -> Anomaly:
    bucket = _dt.datetime(2026, 5, 12, 10, minute, 0)
    return Anomaly(
        kind=kind,
        template=template,
        bucket=bucket,
        observed=10.0,
        expected=1.0,
        score=score,
        count=10,
        total=100,
        level=level,
        explanation="%s fired" % kind,
        evidence=["evidence line"],
        cluster_id=cluster_id,
        start=bucket,
        end=bucket,
    )


def test_empty_input_returns_no_incidents():
    assert group_incidents([], [], 60) == []


def test_nearby_anomalies_group_together():
    clusters = [
        _cluster(0, "checkout failed user=<*> reason=upstream_timeout", 40, 40, "ERROR"),
        _cluster(1, "checkout failed user=<*> reason=db_timeout", 30, 30, "ERROR"),
    ]
    anomalies = [
        _anomaly("error-burst", clusters[0].template, 0, 5, 9.0),
        _anomaly("spike", clusters[1].template, 1, 6, 8.0),
    ]
    incidents = group_incidents(anomalies, clusters, 60)
    assert len(incidents) == 1
    assert len(incidents[0].anomalies) == 2


def test_distant_anomalies_stay_separate():
    clusters = [
        _cluster(0, "alpha failed completely", 10, 10, "ERROR"),
        _cluster(1, "zulu timed out cleanly", 10, 10, "ERROR"),
    ]
    anomalies = [
        _anomaly("error-burst", clusters[0].template, 0, 1, 9.0),
        _anomaly("error-burst", clusters[1].template, 1, 40, 9.0),
    ]
    incidents = group_incidents(anomalies, clusters, 60)
    assert len(incidents) == 2


def test_incident_severity_and_label():
    clusters = [_cluster(0, "checkout failed order=<*>", 20, 20, "ERROR")]
    anomalies = [_anomaly("new-errors", clusters[0].template, 0, 5, 12.0)]
    incident = group_incidents(anomalies, clusters, 60)[0]
    assert incident.severity == "CRITICAL"
    assert incident.severity_label == "SEV-1"
    assert incident.title.startswith("new error class:")


def test_incident_summary_is_informative():
    clusters = [_cluster(0, "checkout failed order=<*>", 20, 20, "ERROR")]
    anomalies = [_anomaly("new-errors", clusters[0].template, 0, 5, 12.0)]
    incident = group_incidents(anomalies, clusters, 60)[0]
    summary = incident.summary()
    assert "anomalies" in summary
    assert "errors" in summary


def test_root_causes_rank_errors_above_chatty_warnings():
    clusters = [
        _cluster(0, "retry scheduled attempt=<*> backoff=<*>", 5000, 0, "WARNING"),
        _cluster(1, "checkout failed user=<*> reason=upstream_timeout", 200, 200, "ERROR"),
    ]
    anomalies = [
        _anomaly("new", clusters[0].template, 0, 5, 40.0, "WARNING"),
        _anomaly("new-errors", clusters[1].template, 1, 5, 6.0, "ERROR"),
    ]
    incident = group_incidents(anomalies, clusters, 60)[0]
    assert incident.root_causes[0].cluster_id == 1
    assert incident.root_causes[0].severity == "CRITICAL"


def test_root_cause_reasons_are_present():
    clusters = [_cluster(0, "checkout failed order=<*>", 20, 20, "ERROR")]
    anomalies = [_anomaly("new-errors", clusters[0].template, 0, 5, 12.0)]
    cause = group_incidents(anomalies, clusters, 60)[0].root_causes[0]
    assert cause.reasons
    assert cause.evidence


def test_still_open_when_last_bucket():
    clusters = [_cluster(0, "checkout failed order=<*>", 20, 20, "ERROR")]
    anomalies = [_anomaly("new-errors", clusters[0].template, 0, 59, 12.0)]
    span_end = _dt.datetime(2026, 5, 12, 10, 59, 30)
    incident = group_incidents(anomalies, clusters, 60, span_end=span_end)[0]
    assert incident.still_open is True


def test_recovered_when_not_last_bucket():
    clusters = [_cluster(0, "checkout failed order=<*>", 20, 20, "ERROR")]
    anomalies = [_anomaly("new-errors", clusters[0].template, 0, 5, 12.0)]
    span_end = _dt.datetime(2026, 5, 12, 10, 40, 0)
    incident = group_incidents(anomalies, clusters, 60, span_end=span_end)[0]
    assert incident.still_open is False


def test_incident_as_dict_round_trips():
    clusters = [_cluster(0, "checkout failed order=<*>", 20, 20, "ERROR")]
    anomalies = [_anomaly("new-errors", clusters[0].template, 0, 5, 12.0)]
    payload = group_incidents(anomalies, clusters, 60)[0].as_dict()
    assert payload["title"]
    assert payload["root_causes"][0]["evidence"]
    assert isinstance(payload["confidence"], float)


def test_incident_config_validation():
    with pytest.raises(ValueError):
        IncidentConfig(proximity_buckets=0).validate()
    with pytest.raises(ValueError):
        IncidentConfig(prefix_tokens=0).validate()


def test_overlapping_incidents_merge():
    clusters = [
        _cluster(0, "checkout failed order=<*>", 20, 20, "ERROR"),
        _cluster(1, "circuit breaker open dependency=<*>", 15, 15, "ERROR"),
    ]
    anomalies = [
        _anomaly("new-errors", clusters[0].template, 0, 5, 10.0),
        _anomaly("error-burst", clusters[1].template, 1, 6, 8.0),
    ]
    incidents = group_incidents(anomalies, clusters, 60)
    assert len(incidents) == 1
    assert incidents[0].severity_label == "SEV-1"
    assert len(incidents[0].root_causes) == 2


def test_min_anomalies_filter():
    clusters = [_cluster(0, "single weird line", 1, 0, "INFO")]
    anomalies = [_anomaly("new", clusters[0].template, 0, 5, 1.0, "INFO")]
    cfg = IncidentConfig(min_anomalies=2)
    assert group_incidents(anomalies, clusters, 60, config=cfg) == []
    assert len(group_incidents(anomalies, clusters, 60)) == 1


def test_duration_and_templates():
    clusters = [
        _cluster(0, "checkout failed order=<*>", 20, 20, "ERROR"),
        _cluster(1, "checkout failed user=<*>", 10, 10, "ERROR"),
    ]
    anomalies = [
        _anomaly("new-errors", clusters[0].template, 0, 5, 10.0),
        _anomaly("spike", clusters[1].template, 1, 8, 8.0),
    ]
    incident = group_incidents(anomalies, clusters, 60)[0]
    assert incident.duration_seconds >= 0
    assert len(incident.templates) == 2
    assert isinstance(incident, Incident)
