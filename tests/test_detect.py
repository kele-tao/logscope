"""Anomaly detection: spikes, bursts, new error classes, thresholds."""

from __future__ import annotations

import datetime as _dt

import pytest

from logscope.detect import Detector, DetectorConfig, estimate_bucket
from logscope.formats import Event
from logscope.miner import Miner


def _feed(miner: Miner, message: str, level: str, minute: int, count: int = 1) -> None:
    for second in range(count):
        ts = _dt.datetime(2026, 5, 12, 10, minute, second % 60)
        miner.add(Event(ts=ts, message=message, level=level, line=0, raw=message))


def _incident_miner() -> Miner:
    """30 steady minutes, then a three-minute error storm from minute 20."""
    miner = Miner()
    for minute in range(30):
        _feed(miner, "request handled status=200 duration=12ms", "INFO", minute, 10)
    for minute in range(20, 23):
        _feed(miner, "checkout failed reason=upstream_timeout", "ERROR", minute, 60)
    return miner


def test_config_validation():
    with pytest.raises(ValueError):
        DetectorConfig(z_threshold=0).validate()
    with pytest.raises(ValueError):
        DetectorConfig(ewma_alpha=0).validate()
    with pytest.raises(ValueError):
        DetectorConfig(min_count=0).validate()
    with pytest.raises(ValueError):
        DetectorConfig(bootstrap=0).validate()


def test_spike_is_detected_with_explanation():
    miner = _incident_miner()
    for minute in range(30):
        _feed(miner, "slow query table=orders duration=900ms", "WARNING", minute, 2)
    for minute in range(20, 23):
        _feed(miner, "slow query table=orders duration=900ms", "WARNING", minute, 40)
    anomalies = Detector().detect(miner.clusters, 60)
    spikes = [a for a in anomalies if a.kind in ("spike", "error-burst")]
    assert spikes
    assert all(a.explanation for a in anomalies)
    assert all("baseline" in a.explanation or "average" in a.explanation for a in spikes)
    assert spikes[0].evidence


def test_new_error_class_detected():
    miner = _incident_miner()
    anomalies = Detector().detect(miner.clusters, 60)
    new_errors = [a for a in anomalies if a.kind == "new-errors"]
    assert new_errors
    assert "new error" in new_errors[0].explanation
    assert new_errors[0].evidence


def test_quiet_log_produces_no_anomalies():
    miner = Miner()
    for minute in range(40):
        _feed(miner, "steady heartbeat ok", "INFO", minute, 5)
    assert Detector().detect(miner.clusters, 60) == []


def test_short_log_produces_no_anomalies():
    miner = Miner()
    for minute in range(3):
        _feed(miner, "steady heartbeat ok", "INFO", minute, 5)
    assert Detector().detect(miner.clusters, 60) == []


def test_anomaly_as_dict_is_json_safe():
    miner = _incident_miner()
    anomalies = Detector().detect(miner.clusters, 60)
    payload = anomalies[0].as_dict()
    assert set(payload) >= {"kind", "template", "severity", "explanation", "evidence", "score"}
    assert isinstance(payload["score"], float)


def test_anomaly_severity_mapping():
    miner = _incident_miner()
    anomalies = Detector().detect(miner.clusters, 60)
    for anomaly in anomalies:
        assert anomaly.severity in ("CRITICAL", "WARNING")
        if anomaly.kind in ("new-errors", "error-burst"):
            assert anomaly.severity == "CRITICAL"


def test_estimate_bucket_scales_with_span():
    assert estimate_bucket(0) == 60
    assert estimate_bucket(30) == 60
    assert estimate_bucket(600) == 300
    huge = estimate_bucket(10**6)
    assert huge >= estimate_bucket(600)
    assert huge * 400 >= 10**6 * 60


def test_detector_can_be_tuned_to_be_quiet():
    miner = _incident_miner()
    strict = Detector(DetectorConfig(z_threshold=99.0, burst_factor=99.0, min_count=10**6))
    assert [a for a in strict.detect(miner.clusters, 60) if a.kind in ("spike", "burst")] == []


def test_burst_detector_catches_tail():
    miner = Miner()
    for minute in range(20):
        _feed(miner, "batch job tick", "INFO", minute, 2)
    for minute in range(20, 40):
        _feed(miner, "batch job tick", "INFO", minute, 40)
    anomalies = Detector().detect(miner.clusters, 60)
    assert any(a.kind in ("burst", "spike") for a in anomalies)


def test_anomalies_sorted_by_time():
    miner = _incident_miner()
    anomalies = Detector().detect(miner.clusters, 60)
    times = [a.start for a in anomalies]
    assert times == sorted(times)
