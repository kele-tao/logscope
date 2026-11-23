"""Streaming log ingestion with format auto-detection.

Every parser is a pure function from one line to an Event (or None when the
line carries no signal). Reader streams a file with a fixed memory ceiling:
the file is read through a small bounded buffer, never loaded in one piece, so
a 200 MB log costs the same as a 200 KB one.

Supported dialects
------------------
jsonl    newline-delimited JSON (Docker, Kubernetes, Serilog, Pino, ...)
syslog   RFC 5424 and RFC 3164
cli      nginx/apache combined and common access logs
python   logging default and "%(asctime)s %(levelname)s" formats
text     the generic fallback: finds an embedded timestamp, else first seen
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import IO

__all__ = [
    "Event",
    "ParseStats",
    "Reader",
    "detect_format",
    "parse_line",
    "parse_timestamp",
    "normalise_level",
    "open_events",
    "FORMATS",
]

#: Dialect identifiers, in the order detection tries them.
FORMATS = ("jsonl", "syslog", "cli", "python", "text")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

LEVEL_ALIASES = {
    "emerg": "CRITICAL", "emergency": "CRITICAL", "alert": "CRITICAL",
    "crit": "CRITICAL", "critical": "CRITICAL", "fatal": "CRITICAL", "panic": "CRITICAL",
    "err": "ERROR", "error": "ERROR",
    "warn": "WARNING", "warning": "WARNING",
    "notice": "INFO", "info": "INFO", "information": "INFO",
    "debug": "DEBUG", "trace": "DEBUG", "verbose": "DEBUG",
}
LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}

#: Human readable one-liners for incident narratives.
LEVEL_LABEL = {
    "DEBUG": "debug", "INFO": "info", "WARNING": "warning",
    "ERROR": "error", "CRITICAL": "critical",
}


def normalise_level(raw: str | None) -> str:
    """Map a log level token onto DEBUG/INFO/WARNING/ERROR/CRITICAL."""
    if not raw:
        return "INFO"
    token = str(raw).strip().strip("[]()").lower()
    return LEVEL_ALIASES.get(token, "INFO")


def _ms(value: int) -> _dt.datetime:
    try:
        return _dt.datetime.fromtimestamp(value / 1000.0, tz=_dt.timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return _dt.datetime(1970, 1, 1)


@dataclass(slots=True)
class Event:
    """One normalised log record."""

    ts: _dt.datetime | None
    message: str
    level: str = "INFO"
    source: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    line: int = 0
    raw: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts.isoformat(sep=" ") if self.ts else None,
            "level": self.level,
            "message": self.message,
            "source": self.source,
            "line": self.line,
        }


@dataclass(slots=True)
class ParseStats:
    """Coverage of one parse run -- reported so silent bad parses cannot hide."""

    lines: int = 0
    parsed: int = 0
    timestamped: int = 0
    by_format: dict[str, int] = field(default_factory=dict)

    def record(self, fmt: str, event: Event | None) -> None:
        self.lines += 1
        if event is None:
            return
        self.parsed += 1
        if event.ts is not None:
            self.timestamped += 1
        self.by_format[fmt] = self.by_format.get(fmt, 0) + 1


# --------------------------------------------------------------------------
# Timestamp helpers
# --------------------------------------------------------------------------

_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?"
    r"(Z|[+-]\d{2}:?\d{2})?"
)
#: 10 digits = epoch seconds, 13 digits = epoch milliseconds, optional fraction.
_EPOCH_RE = re.compile(r"(?<![\d.])(1[6-9]\d{8}(?:\d{3})?|2\d{9}(?:\d{3})?)(?:\.(\d{1,9}))?(?![\d])")
_SYSLOG_TS_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\b")
_CLF_TS_RE = re.compile(r"\[?(\d{1,2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})")


def _from_iso(match: re.Match[str]) -> _dt.datetime | None:
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    frac = match.group(7) or ""
    micro = int((frac + "000000")[:6]) if frac else 0
    try:
        return _dt.datetime(year, month, day, hour, minute, second, micro)
    except ValueError:
        return None


def _apply_offset(ts: _dt.datetime, offset: str | None) -> _dt.datetime:
    if not offset or offset == "Z":
        return ts
    sign = 1 if offset[0] == "+" else -1
    digits = offset[1:].replace(":", "")
    try:
        delta = _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    except ValueError:
        return ts
    return ts - sign * delta


def parse_timestamp(text: str, default_year: int = 2026) -> _dt.datetime | None:
    """Extract a timestamp from free text; None when nothing plausible is there."""
    match = _ISO_RE.search(text)
    if match:
        ts = _from_iso(match)
        if ts is not None:
            return _apply_offset(ts, match.group(8))
    match = _SYSLOG_TS_RE.search(text)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            try:
                return _dt.datetime(
                    default_year, month, int(match.group(2)),
                    int(match.group(3)), int(match.group(4)), int(match.group(5)),
                )
            except ValueError:
                return None
    match = _CLF_TS_RE.search(text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            try:
                return _dt.datetime(
                    int(match.group(3)), month, int(match.group(1)),
                    int(match.group(4)), int(match.group(5)), int(match.group(6)),
                )
            except ValueError:
                return None
    match = _EPOCH_RE.search(text)
    if match:
        digits = match.group(1)
        frac = match.group(2) or ""
        micro = int((frac + "000000")[:6]) if frac else 0
        # 10 digits are epoch seconds, 13 digits epoch milliseconds.
        value = int(digits)
        base = _ms(value) if len(digits) > 10 else _ms(value * 1000)
        return base.replace(microsecond=micro) if micro else base
    return None


# --------------------------------------------------------------------------
# Dialect parsers
# --------------------------------------------------------------------------

_PY_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*)\s+"
    r"(?:(?P<level>[A-Za-z]{3,9})\s+)?"
    r"(?P<msg>.*)$"
)
#: "logger: message" -- the canonical logging format.
_PY_NAMED_RE = re.compile(r"^(?P<name>[A-Za-z_][\w.\-]*):\s+(?P<msg>.*)$")
#: "service<3+ spaces>message" -- the service-column layout used by our demo.
_PY_COLUMN_RE = re.compile(r"^(?P<name>[A-Za-z_][\w.\-]{0,23})\s{2,}(?P<msg>\S.*)$")

_LEVEL_TOKEN_RE = re.compile(
    r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRIT|CRITICAL|FATAL)\b"
)

_CLF_RE = re.compile(
    r'^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<ts>[^\]]+)\]\s+"(?P<req>[^"]*)"\s+'
    r"(?P<status>\d{3})\s+(?P<size>\S+)"
    r'(?:\s+"(?P<ref>[^"]*)"\s+"(?P<ua>[^"]*)")?'
)

_LEVEL_STATUS = {2: "INFO", 3: "INFO", 4: "WARNING", 5: "ERROR"}

_JSON_TS_KEYS = ("timestamp", "time", "ts", "@timestamp", "date", "datetime", "t")
_JSON_MSG_KEYS = ("message", "msg", "event", "log", "text", "error", "body")
_JSON_LEVEL_KEYS = ("level", "severity", "lvl", "levelname", "log_level", "priority")
_JSON_SOURCE_KEYS = ("logger", "source", "service", "name", "component", "module", "app", "stream")


def _from_json_obj(obj: dict[str, object], line_no: int, raw: str) -> Event:
    ts = None
    for key in _JSON_TS_KEYS:
        if key in obj:
            value = obj[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                ts = _ms(int(value * 1000)) if value < 1e11 else _ms(int(value))
            elif isinstance(value, str):
                ts = parse_timestamp(value)
            if ts is not None:
                break
    message = ""
    for key in _JSON_MSG_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            message = value.strip()
            break
        if isinstance(value, (dict, list)):
            message = json.dumps(value, ensure_ascii=False, sort_keys=True)
            break
    level = "INFO"
    for key in _JSON_LEVEL_KEYS:
        if key in obj and isinstance(obj[key], (str, int)):
            level = normalise_level(str(obj[key]))
            break
    source = ""
    for key in _JSON_SOURCE_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value:
            source = value
            break
    if not message:
        message = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    extra = {
        str(k): str(v)
        for k, v in obj.items()
        if isinstance(v, (str, int, float, bool)) and k not in _JSON_MSG_KEYS
    }
    return Event(ts=ts, message=message, level=level, source=source,
                 extra=extra, line=line_no, raw=raw)


def parse_jsonl(line: str, line_no: int = 0) -> Event | None:
    """Parse a single JSON object line."""
    stripped = line.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return _from_json_obj(obj, line_no, line)


_SYSLOG_5424_RE = re.compile(
    r"^(?:<(?P<pri>\d{1,3})>)(?P<ver>\d)\s+(?P<ts>\S+)\s+(?P<host>\S+)\s+"
    r"(?P<app>\S+)\s+(?P<proc>\S+)\s+(?P<msgid>\S+)\s+(?P<rest>.*)$"
)
_SYSLOG_3164_RE = re.compile(
    r"^(?:<(?P<pri>\d{1,3})>)?(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<app>[^:\s]+)(?:\[\d+\])?:\s*(?P<rest>.*)$"
)
_PRI_LEVEL = {0: "CRITICAL", 1: "CRITICAL", 2: "CRITICAL", 3: "ERROR",
              4: "WARNING", 5: "INFO", 6: "INFO", 7: "DEBUG"}


def _pri_level(pri: str | None) -> str | None:
    if not pri:
        return None
    try:
        return _PRI_LEVEL[int(pri) % 8]
    except ValueError:
        return None


def parse_syslog(line: str, line_no: int = 0) -> Event | None:
    """Parse RFC 5424 or RFC 3164 syslog."""
    stripped = line.strip()
    if not stripped:
        return None
    match = _SYSLOG_5424_RE.match(stripped)
    if match:
        level = _pri_level(match.group("pri"))
        if level is None:
            token = _LEVEL_TOKEN_RE.search(match.group("rest"))
            level = normalise_level(token.group(1)) if token else "INFO"
        message = match.group("rest").strip()
        if message.startswith("- "):
            message = message[2:].strip()
        return Event(ts=parse_timestamp(match.group("ts")), message=message,
                     level=level, source=match.group("app").strip("-") or "",
                     line=line_no, raw=line)
    match = _SYSLOG_3164_RE.match(stripped)
    if match:
        level = _pri_level(match.group("pri"))
        if level is None:
            token = _LEVEL_TOKEN_RE.search(match.group("rest"))
            level = normalise_level(token.group(1)) if token else "INFO"
        app = match.group("app").strip()
        if app.endswith("]"):
            app = app.split("[", 1)[0]
        return Event(ts=parse_timestamp(match.group("ts")), message=match.group("rest").strip(),
                     level=level, source=app, line=line_no, raw=line)
    return None


def parse_cli(line: str, line_no: int = 0) -> Event | None:
    """Parse an nginx/apache combined (or common) access log line."""
    match = _CLF_RE.match(line.strip())
    if not match:
        return None
    status = int(match.group("status"))
    size = match.group("size")
    request = match.group("req") or "-"
    method, path = "HTTP", request
    if " " in request:
        method, path = request.split(" ", 1)
        path = path.split(" ")[0]
    return Event(
        ts=parse_timestamp(match.group("ts")),
        message="request=%s path=%s status=%d bytes=%s ip=%s" % (
            method, path, status, "0" if size in ("-", "") else size,
            match.group("ip") or "-",
        ),
        level=_LEVEL_STATUS.get(status // 100, "INFO"),
        source="http",
        extra={
            "status": str(status),
            "ip": match.group("ip") or "",
            "user_agent": match.group("ua") or "",
            "referer": match.group("ref") or "",
        },
        line=line_no,
        raw=line,
    )


_PY_FAST_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?\s+"
    r"(?:(?P<level>[A-Za-z]{3,9})\s+)?"
    r"(?P<msg>.*)$"
)


def _fast_ts(match: re.Match[str]) -> _dt.datetime | None:
    """Build a datetime straight from the regex groups: no second regex pass."""
    try:
        return _dt.datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
            int(match.group(4)), int(match.group(5)), int(match.group(6)),
            int((match.group(7) or "000000").ljust(6, "0")[:6]),
        )
    except ValueError:
        return None


def parse_python(line: str, line_no: int = 0) -> Event | None:
    """Parse Python logging output: timestamp, optional level, optional logger, message."""
    stripped = line.rstrip()
    match = _PY_FAST_RE.match(stripped)
    if match is not None:
        ts = _fast_ts(match)
        raw_level = match.group("level")
        rest = match.group("msg")
    else:
        match = _PY_RE.match(stripped)
        if not match:
            return None
        ts = parse_timestamp(match.group("ts"))
        if ts is None:
            return None
        raw_level = match.group("level")
        rest = match.group("msg")
    raw_level = match.group("level")
    rest = match.group("msg")
    level = "INFO"
    if raw_level and raw_level.lower() in LEVEL_ALIASES:
        level = normalise_level(raw_level)
    else:
        # The token was not a level after all; keep it in the message.
        rest = (raw_level + " " + rest) if raw_level else rest
    source = ""
    named = _PY_NAMED_RE.match(rest)
    if named:
        source = named.group("name")
        rest = named.group("msg")
    else:
        column = _PY_COLUMN_RE.match(rest)
        if column:
            source = column.group("name")
            rest = column.group("msg")
    return Event(ts=ts, message=rest.strip(), level=level, source=source,
                 line=line_no, raw=line)


def parse_text(line: str, line_no: int = 0) -> Event | None:
    """Generic fallback: timestamp if present, first level token if present."""
    stripped = line.rstrip()
    if not stripped.strip():
        return None
    level = "INFO"
    token = _LEVEL_TOKEN_RE.search(stripped[:160])
    if token:
        level = normalise_level(token.group(1))
    return Event(ts=parse_timestamp(stripped), message=stripped.strip(),
                 level=level, line=line_no, raw=line)


_PARSERS = {
    "jsonl": parse_jsonl,
    "syslog": parse_syslog,
    "cli": parse_cli,
    "python": parse_python,
    "text": parse_text,
}


def parse_line(line: str, fmt: str = "text", line_no: int = 0) -> Event | None:
    """Parse one line with an explicit dialect."""
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ValueError("unknown log format %r (choose from %s)" % (fmt, ", ".join(FORMATS)))
    return parser(line, line_no)


def _score(sample: list[str]) -> dict[str, int]:
    scores = dict.fromkeys(FORMATS, 0)
    for line in sample:
        if parse_jsonl(line, 0) is not None:
            scores["jsonl"] += 3
        if _SYSLOG_5424_RE.match(line.strip()) or _SYSLOG_3164_RE.match(line.strip()):
            scores["syslog"] += 3
        if _CLF_RE.match(line.strip()):
            scores["cli"] += 3
        if parse_python(line, 0) is not None:
            scores["python"] += 2
        if parse_timestamp(line) is not None:
            scores["text"] += 1
    return scores


def detect_format(lines: Iterable[str], sample: int = 200) -> str:
    """Guess the dialect of a log from up to sample non-empty lines."""
    taken: list[str] = []
    for line in lines:
        if line.strip():
            taken.append(line)
        if len(taken) >= sample:
            break
    if not taken:
        return "text"
    scores = _score(taken)
    threshold = max(1, len(taken) // 2)
    for fmt in ("jsonl", "syslog", "cli"):
        if scores[fmt] >= threshold * 3:
            return fmt
    if scores["python"] >= threshold:
        return "python"
    return "text"


class Reader:
    """Stream events out of a text stream at constant memory."""

    def __init__(self, stream: IO[str], fmt: str = "auto", origin: str = "<stream>"):
        self.stream = stream
        self.origin = origin
        self.stats = ParseStats()
        self._fmt = fmt
        self._resolved = fmt != "auto"
        self._fallback_ts: _dt.datetime | None = None

    @property
    def format(self) -> str:
        """Resolved dialect; triggers auto-detection on first access."""
        if not self._resolved:
            head: list[str] = []
            for line in self.stream:
                head.append(line)
                if line.strip() and len(head) >= 400:
                    break
            if self.stream.seekable():
                self.stream.seek(0)
            else:
                self._buffer = head
            self._fmt = detect_format(head)
            self._resolved = True
        return self._fmt

    def __iter__(self) -> Iterator[Event]:
        fmt = self.format
        parser = _PARSERS[fmt]
        stats = self.stats
        by_format = stats.by_format
        buffered = getattr(self, "_buffer", None)
        if buffered is not None:
            self._buffer = None
            stream: Iterable[str] = buffered
        else:
            stream = self.stream
        fallback = self._fallback_ts
        for line_no, line in enumerate(stream, 1):
            event = parser(line, line_no)
            stats.lines += 1
            if event is None:
                continue
            stats.parsed += 1
            by_format[fmt] = by_format.get(fmt, 0) + 1
            if event.ts is None:
                event.ts = fallback
            else:
                stats.timestamped += 1
                if fallback is None:
                    fallback = event.ts
            yield event
        self._fallback_ts = fallback


def open_events(path: str, fmt: str = "auto") -> Reader:
    """Open a path (or stdin for "-") and return a streaming Reader."""
    if path == "-":
        return Reader(sys.stdin, fmt=fmt, origin="<stdin>")
    stream = open(path, encoding="utf-8", errors="replace", buffering=1 << 16)
    return Reader(stream, fmt=fmt, origin=path)
