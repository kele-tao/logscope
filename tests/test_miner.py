"""Template mining: tokenisation, masking, clustering, ranking."""

from __future__ import annotations

import datetime as _dt

import pytest

from logscope.formats import Event
from logscope.miner import (
    MASK,
    Cluster,
    Miner,
    mask_tokens,
    render_template,
    similarity,
    tokenize,
)


def _event(message: str, level: str = "INFO", minute: int = 0) -> Event:
    ts = _dt.datetime(2026, 5, 12, 10, minute, 0)
    return Event(ts=ts, message=message, level=level, source="api", line=minute + 1, raw=message)


def test_tokenize_splits_key_value():
    tokens = tokenize("request handled status=200 duration=12ms")
    assert tokens == ["request", "handled", "status=", "200", "duration=", "12ms"]


def test_tokenize_keeps_paths_whole():
    tokens = tokenize("GET /v1/cart/checkout HTTP/1.1")
    assert "/v1/cart/checkout" in tokens


def test_mask_replaces_variables():
    masked = mask_tokens(tokenize("user alice failed from 10.0.0.1 after 30ms"))
    assert masked == ["user", MASK, "failed", "from", MASK, "after", MASK]


def test_mask_keeps_categorical_values():
    masked = mask_tokens(tokenize("request handled status=503 method=GET"))
    assert "503" in masked
    assert "GET" in masked


def test_render_template_round_trip():
    masked = mask_tokens(tokenize("user alice failed from 10.0.0.1"))
    assert render_template(masked) == "user <*> failed from <*>"


def test_similarity_bounds():
    assert similarity([], []) == 0.0
    assert similarity(["a"], ["a"]) == 1.0
    assert similarity(["a", "b"], ["a", "c"]) == 0.5
    assert similarity(["a", "b", "c"], ["a", "b"]) < 0.9


def test_miner_collapses_variable_lines():
    miner = Miner(threshold=0.7)
    for index in range(20):
        miner.add(_event("user u%d failed from 10.0.0.%d" % (index, index), "ERROR"))
    assert miner.template_count() == 1
    cluster = miner.clusters[0]
    assert cluster.count == 20
    assert cluster.template == "user <*> failed from <*>"
    assert cluster.error_count == 20


def test_miner_separates_different_messages():
    miner = Miner(threshold=0.7)
    miner.add(_event("cache hit key=abc"))
    miner.add(_event("payment settled order=123"))
    miner.add(_event("user alice authenticated from 10.0.0.1"))
    assert miner.template_count() == 3


def test_miner_keeps_status_codes_apart():
    miner = Miner(threshold=0.7)
    for status in (200, 500, 503):
        for _ in range(5):
            miner.add(_event("request handled status=%d bytes=12" % status))
    templates = sorted(c.template for c in miner.clusters)
    assert len(templates) == 3
    assert any("status=500" in t for t in templates)


def test_miner_rejects_bad_threshold():
    with pytest.raises(ValueError):
        Miner(threshold=0.0)
    with pytest.raises(ValueError):
        Miner(threshold=1.5)
    with pytest.raises(ValueError):
        Miner(depth=0)


def test_miner_sample_cap_and_evidence():
    miner = Miner()
    for index in range(10):
        miner.add(_event("boom code=%d" % index, "ERROR"))
    cluster = miner.clusters[0]
    assert len(cluster.samples) <= 3
    assert len(cluster.evidence) <= 3
    assert cluster.evidence[0].startswith("boom code=")


def test_cluster_histogram_buckets():
    miner = Miner()
    for minute in range(5):
        for _ in range(minute + 1):
            miner.add(_event("steady heartbeat", minute=minute))
    cluster = miner.clusters[0]
    minute_buckets = cluster.buckets(60)
    assert len(minute_buckets) == 5
    two_minute = cluster.buckets(120)
    assert sum(two_minute.values()) == cluster.count


def test_cluster_as_dict_is_json_safe():
    miner = Miner()
    miner.add(_event("hello world"))
    payload = miner.clusters[0].as_dict()
    assert payload["template"] == "hello world"
    assert payload["count"] == 1
    assert isinstance(payload["histogram"], dict)


def test_ranked_prefers_rare_templates():
    miner = Miner()
    for _ in range(100):
        miner.add(_event("routine heartbeat"))
    for _ in range(3):
        miner.add(_event("rare fatal disk failure", "ERROR"))
    ranked = [cluster.template for cluster, _ in miner.ranked(2)]
    assert "rare fatal disk failure" in ranked


def test_miner_histogram_totals_match_events():
    miner = Miner()
    for minute in range(4):
        miner.add(_event("tick", minute=minute))
    histogram = miner.histogram(60)
    assert sum(histogram.values()) == 4


def test_miner_shrink_keeps_dictionary_consistent():
    miner = Miner(max_clusters=8)
    for index in range(200):
        miner.add(_event("unique template number %d for shrink" % index))
    assert miner.template_count() <= 8
    for cluster in miner.clusters:
        assert cluster.cluster_id < len(miner.clusters)


def test_cluster_severity_is_highest_seen():
    cluster = Cluster(pattern=["x"], cluster_id=0, levels={"INFO": 3, "ERROR": 1})
    assert cluster.severity == "ERROR"
    assert cluster.masked_ratio == 0.0
