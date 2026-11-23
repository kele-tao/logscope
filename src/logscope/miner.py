"""Drain-style template mining in pure Python.

A log line is tokenised, variables are masked, and the remaining skeleton is
the *template* (the cluster id).  Masked slots are visible as <*>, so
"user alice failed from 10.0.0.1" and "user bob failed from 10.0.0.2" collapse to
"user <*> failed from <*>".

The tree lives in a single level of buckets keyed by (token count, first token).
Within a bucket we compare against at most MAX_CANDIDATES live templates using a
token-position similarity walk.  That keeps mining linear in practice while
still producing readable templates -- which is the whole point of the tool.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache

from logscope.formats import LEVEL_ORDER, Event

__all__ = [
    "Cluster",
    "Miner",
    "tokenize",
    "mask_tokens",
    "mask_message",
    "similarity",
    "render_template",
]

MASK = "<*>"


#: Keys whose values are categories, not variables: status=500 is signal.
_KEEP_VALUE_AFTER = ("status=", "code=", "status", "level=", "severity=",
                     "method=", "http_status=", "proto", "request=", "reason=")
#: Keys whose value is an identifier: "user alice" masks alice, "user_id=7" masks 7.
_MASK_NEXT_AFTER = frozenset(
    {"user", "users", "from", "to", "host", "hostname", "key", "token", "session",
     "trace", "span", "request_id", "order", "account", "tenant", "customer"}
)
#: A categorical value: a short number or an upper-case enum token.
_CATEGORICAL_RE = re.compile(r"^(?:\d{1,5}|[A-Z][A-Z0-9_]{0,15})$")
#: A bare identifier (no spaces): safe to mask after "user", "from", "host".
_IDENTIFIER_RE = re.compile(r"^[\w.@:/+-]{1,64}$")

#: One pass over the message: key=value keys keep their "=", structure punctuation
#: is its own token, and everything else splits on whitespace or punctuation.
_TOKEN_RE = re.compile(
    r"[^\s=,;|{}\[\]()\"']+=|[^\s=,;|{}\[\]()\"']+|,|;|\||\{|\}|\[|\]|\(|\)|\"|'"
)


def tokenize(message: str) -> list[str]:
    """Split a message into tokens.

    Whitespace and structural punctuation separate tokens; "key=value" keeps the
    key and its "=" together so the key stays literal while the value can be
    masked. Paths and URLs stay in one piece and are masked as a whole.
    """
    return _TOKEN_RE.findall(message)


#: A token that is entirely a value: number (with unit/punctuation), hex blob,
#: shell-style placeholder or UUID.
_VARIABLE_RE = re.compile(
    r"^(?:[+-]?\d[\w.,:/-]*"
    r"|0x[0-9a-fA-F]+"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\$[\w$]+|%[\w%]+|@[\w@]+)$"
)
#: Upper-case words are enums from a fixed vocabulary (GET, ERROR, ...).
_ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,15}$")
#: Prefixes that always introduce a variable value (URLs, absolute paths).
_PREFIX_RE = re.compile(
    r"^(?:https?://|www\.|/var/|/home/|/opt/|/usr/|/tmp/|/etc/|/srv/)"
)


@lru_cache(maxsize=16384)
def _is_variable(token: str) -> bool:
    """True when a token looks like a value rather than part of the message.

    Tokens repeat heavily inside one log, so the answer is memoised; that alone
    roughly halves mining time on multi-megabyte files. Each check is a C-level
    regex or set lookup -- no per-character Python loops.
    """
    head = token[:1]
    if head.isdigit() or head in _SIGN_CHARS:
        if _VARIABLE_RE.match(token):
            return True
    if head == "/" and token.count("/") > 1:
        return True
    if _PREFIX_RE.match(token) is not None:
        return len(token) > 4
    if _DIGIT_RE.search(token) is not None and _ALPHA_RE.search(token) is not None:
        return True
    return _ENUM_RE.match(token) is not None


_SIGN_CHARS = frozenset("+-$%@")
_DIGIT_RE = re.compile(r"[0-9]")
_ALPHA_RE = re.compile(r"[A-Za-z]")


def mask_message(message: str) -> list[str]:
    """Tokenise and mask in one pass -- the hot path of the miner."""
    tokens = _TOKEN_RE.findall(message)
    masked: list[str] = []
    append = masked.append
    previous = ""
    for token in tokens:
        if previous in _KEEP_VALUE_AFTER and _CATEGORICAL_RE.match(token):
            append(token)
        elif previous in _MASK_NEXT_AFTER and _IDENTIFIER_RE.match(token):
            append(MASK)
        elif _is_variable(token):
            append(MASK)
        else:
            append(token)
        previous = token.lower()
    return masked


def mask_tokens(tokens: list[str]) -> list[str]:
    """Replace variable-looking tokens with the MASK marker.

    Values of known categorical keys (status=, code=, level=) stay literal: a
    500 and a 200 are different templates, and merging them loses the story.
    """
    masked: list[str] = []
    append = masked.append
    previous = ""
    for token in tokens:
        if previous in _KEEP_VALUE_AFTER and _CATEGORICAL_RE.match(token):
            append(token)
        elif previous in _MASK_NEXT_AFTER and _IDENTIFIER_RE.match(token):
            append(MASK)
        elif _is_variable(token):
            append(MASK)
        else:
            append(token)
        previous = token.lower()
    return masked


def render_template(masked: list[str]) -> str:
    """Join a masked token list back into a human-readable template string."""
    out = ""
    for tok in masked:
        if not out:
            out = tok
            continue
        if tok in ",;=|{}[]()\"'/:" or out.endswith((" ", "=", "|", ",", ";", "(", "[", "/")):
            out += tok
        else:
            out += " " + tok
    return out.strip()


def similarity(a: list[str], b: list[str]) -> float:
    """Positional token agreement in [0, 1]."""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        shorter = min(len(a), len(b))
        agree = sum(1 for i in range(shorter) if a[i] == b[i])
        # Unequal token counts are a weak signal: penalise them on top of ratio.
        return 0.9 * agree / max(len(a), len(b))
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


@dataclass(slots=True)
class Cluster:
    """One mined template and the statistics gathered for it."""

    pattern: list[str]
    cluster_id: int
    count: int = 0
    error_count: int = 0
    warn_count: int = 0
    first_seen: object = None
    last_seen: object = None
    first_line: int = 0
    per_minute: dict[object, int] = field(default_factory=dict)
    levels: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    sample_cap: int = 3
    hash: int = 0

    @property
    def template(self) -> str:
        """Readable template, e.g. "user <*> failed from <*>"."""
        return render_template(self.pattern)

    @property
    def masked_ratio(self) -> float:
        """Share of slots that are variables -- high means a generic template."""
        if not self.pattern:
            return 0.0
        return sum(1 for t in self.pattern if t == MASK) / len(self.pattern)

    @property
    def severity(self) -> str:
        """Highest level ever seen on this template."""
        best = "INFO"
        for level in self.levels:
            if LEVEL_ORDER.get(level, 1) > LEVEL_ORDER.get(best, 1):
                best = level
        return best

    def add_sample(self, message: str, raw: str = "") -> None:
        text = message.strip()
        if len(self.samples) < self.sample_cap and (not self.samples or self.samples[-1] != text):
            self.samples.append(text)
        line = (raw or message).rstrip()
        if line and len(self.evidence) < self.sample_cap and (not self.evidence or self.evidence[-1] != line):
            self.evidence.append(line)

    def buckets(self, bucket_seconds: int) -> dict[object, int]:
        """Aggregate per-minute counts into coarser buckets."""
        if bucket_seconds <= 60:
            return dict(self.per_minute)
        agg: dict[object, int] = {}
        for minute, count in self.per_minute.items():
            if minute is None:
                continue
            epoch = int(minute.timestamp())
            key = minute.fromtimestamp(epoch - epoch % bucket_seconds)
            agg[key] = agg.get(key, 0) + count
        return agg

    def as_dict(self, bucket_seconds: int = 60) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "template": self.template,
            "count": self.count,
            "errors": self.error_count,
            "warnings": self.warn_count,
            "severity": self.severity,
            "first_seen": self.first_seen.isoformat(sep=" ") if self.first_seen else None,
            "last_seen": self.last_seen.isoformat(sep=" ") if self.last_seen else None,
            "first_line": self.first_line,
            "samples": list(self.samples),
            "evidence": list(self.evidence),
            "sources": dict(sorted(self.sources.items(), key=lambda kv: -kv[1])[:5]),
            "histogram": {
                (k.isoformat(sep=" ") if k else "unknown"): v
                for k, v in sorted(
                    self.buckets(bucket_seconds).items(),
                    key=lambda kv: (kv[0] is None, kv[0] or 0),
                )
            },
        }


MAX_CANDIDATES = 64
REPRESENTATIVE_CAP = 40


class Miner:
    """Online template miner: feed events, get clusters back."""

    def __init__(self, threshold: float = 0.7, depth: int = 6, max_clusters: int = 20000):
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1], got %r" % (threshold,))
        if depth < 1:
            raise ValueError("depth must be >= 1, got %r" % (depth,))
        if max_clusters < 1:
            raise ValueError("max_clusters must be >= 1, got %r" % (max_clusters,))
        self.threshold = threshold
        self.depth = depth
        self.max_clusters = max_clusters
        self.clusters: list[Cluster] = []
        self._buckets: dict[tuple[int, str], list[int]] = {}
        self._representative: dict[int, list[str]] = {}
        #: Exact masked-token-sequence -> cluster id. Real logs repeat the same
        #: masked skeleton thousands of times, so this skips the similarity walk
        #: on the overwhelming majority of lines.
        self._exact: dict[tuple[str, ...], int] = {}
        self.events = 0
        self.masked_tokens = 0
        self.token_total = 0

    # -- mining ------------------------------------------------------------
    def _key(self, masked: list[str]) -> tuple[int, str]:
        return (min(len(masked), self.depth), masked[0] if masked else "")

    def _shrink(self) -> None:
        by_count = sorted(range(len(self.clusters)), key=lambda i: self.clusters[i].count)
        drop = set(by_count[: max(1, len(by_count) // 4)])
        rebuilt: list[Cluster] = []
        buckets: dict[tuple[int, str], list[int]] = {}
        for old_index, cluster in enumerate(self.clusters):
            if old_index in drop:
                continue
            cluster.cluster_id = len(rebuilt)
            representative = self._representative[old_index]
            self._representative[len(rebuilt)] = representative
            rebuilt.append(cluster)
            buckets.setdefault(self._key(representative), []).append(cluster.cluster_id)
        self.clusters = rebuilt
        self._buckets = buckets
        self._exact.clear()

    def add(self, event: Event) -> Cluster:
        """Bucket one event into a template cluster, creating it when needed."""
        masked = mask_message(event.message)
        if not masked:
            masked = ["<empty>"]
        self.events += 1
        self.token_total += len(masked)
        self.masked_tokens += sum(1 for token in masked if token == MASK)

        exact = self._exact.get(tuple(masked))
        if exact is not None:
            cluster = self.clusters[exact]
        else:
            cluster = self._match(masked)
        self._record(cluster, event)
        return cluster

    def _match(self, masked: list[str]) -> Cluster:
        """Find (or create) the cluster for a masked token list."""
        key = self._key(masked)
        candidates = self._buckets.get(key, [])
        best_index = -1
        best_score = 0.0
        for index in candidates[:MAX_CANDIDATES]:
            if _protected_conflict(self._representative[index], masked):
                continue
            score = similarity(masked, self._representative[index])
            if score > best_score:
                best_score = score
                best_index = index

        if best_index >= 0 and best_score >= self.threshold:
            cluster = self.clusters[best_index]
            merged = _merge_masks(self._representative[best_index], masked)
            if merged != self._representative[best_index]:
                cluster.pattern = merged
                self._representative[best_index] = merged
                new_key = self._key(merged)
                if new_key != key:
                    candidates.remove(best_index)
                    self._buckets.setdefault(new_key, []).append(best_index)
        else:
            cluster = Cluster(
                pattern=list(masked),
                cluster_id=len(self.clusters),
                levels={},
                sources={},
            )
            self.clusters.append(cluster)
            self._representative[cluster.cluster_id] = masked
            self._buckets.setdefault(key, []).append(cluster.cluster_id)
            if len(self.clusters) > self.max_clusters:
                self._shrink()
        if len(self._exact) < 200000:
            self._exact[tuple(masked)] = cluster.cluster_id
        return cluster

    def _record(self, cluster: Cluster, event: Event) -> None:
        """Fold one event into the cluster's statistics."""
        cluster.count += 1
        if event.ts is not None:
            if cluster.first_seen is None or event.ts < cluster.first_seen:
                cluster.first_seen = event.ts
            if cluster.last_seen is None or event.ts > cluster.last_seen:
                cluster.last_seen = event.ts
            minute = event.ts.replace(second=0, microsecond=0)
            cluster.per_minute[minute] = cluster.per_minute.get(minute, 0) + 1
        else:
            cluster.per_minute.setdefault(None, 0)  # type: ignore[arg-type]
        level = event.level
        cluster.levels[level] = cluster.levels.get(level, 0) + 1
        if level == "ERROR" or level == "CRITICAL":
            cluster.error_count += 1
        elif level == "WARNING":
            cluster.warn_count += 1
        if event.source:
            cluster.sources[event.source] = cluster.sources.get(event.source, 0) + 1
        cluster.add_sample(event.message, event.raw)

    # -- reporting ---------------------------------------------------------
    def most_common(self, limit: int = 10) -> list[Cluster]:
        """Clusters ordered by raw volume."""
        return sorted(self.clusters, key=lambda c: (-c.count, c.cluster_id))[:limit]

    def ranked(self, limit: int = 10) -> list[tuple[Cluster, float]]:
        """Clusters ordered by count x novelty (rare templates float up)."""
        total = max(1, self.events)
        scored = []
        for cluster in self.clusters:
            novelty = -math.log10((cluster.count + 1) / total)
            scored.append((cluster, cluster.count * novelty, novelty))
        scored.sort(key=lambda item: (-item[1], -item[0].count, item[0].cluster_id))
        return [(c, round(score, 2)) for c, score, _ in scored[:limit]]

    def histogram(self, bucket_seconds: int = 60) -> dict[object, int]:
        """Total event volume per bucket across all clusters."""
        agg: dict[object, int] = {}
        for cluster in self.clusters:
            for key, count in cluster.buckets(bucket_seconds).items():
                agg[key] = agg.get(key, 0) + count
        return dict(sorted(agg.items(), key=lambda kv: kv[0] or 0))

    def template_count(self) -> int:
        return len(self.clusters)


def _protected_conflict(a: list[str], b: list[str]) -> bool:
    """True when two token lists disagree on a categorical value (status=500 vs 200)."""
    limit = min(len(a), len(b))
    for index in range(1, limit):
        if a[index] == b[index]:
            continue
        if a[index - 1].lower() in _KEEP_VALUE_AFTER:
            return True
    return False


def _merge_masks(a: list[str], b: list[str]) -> list[str]:
    """Union of two masked token lists: disagreement becomes a mask."""
    if len(a) != len(b):
        size = max(len(a), len(b))
        out = []
        for i in range(size):
            left = a[i] if i < len(a) else MASK
            right = b[i] if i < len(b) else MASK
            out.append(left if left == right and left != MASK else MASK)
        return out
    return [x if x == y else MASK for x, y in zip(a, b, strict=True)]
