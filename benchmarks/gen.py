"""Generate the demo logs used by the README, examples/ and the screenshot.

The log is synthetic but not decorative: it encodes a real incident story that
logscope has to discover on its own.

    09:00-09:11  steady traffic, ~40 lines/min, healthy 200s
    09:12:00     deploy: new deploy/config/cache-warm templates appear
    09:12:40     the checkout path starts throwing 500s (a template never seen before)
    09:13-09:17  error volume ramps 3x, then a retry storm on payment.charge
    09:18-09:26  recovery: errors decay, a single late "still degraded" warning
    09:26-09:27  tail: back to baseline

Run:
    python benchmarks/gen.py --lines 20000 --out examples/app.log
    python benchmarks/gen.py --lines 4000 --out examples/nginx.log --format nginx

Deterministic: the same --seed gives byte-identical output.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import random
import sys

TEMPLATES = [
    ("request handled method=<*> path=<*> status=200 duration=<*>ms", "INFO", 26),
    ("db query completed table=<*> rows=<*> duration=<*>ms", "DEBUG", 12),
    ("cache hit key=<*> ttl=<*>s", "DEBUG", 9),
    ("cache miss key=<*> fill=<*>ms", "DEBUG", 5),
    ("kafka consumer lag group=<*> partition=<*> lag=<*>", "INFO", 4),
    ("healthcheck ok component=<*> uptime=<*>s", "DEBUG", 6),
    ("user <*> authenticated from <*>", "INFO", 7),
    ("payment charge accepted id=<*> amount=<*>", "INFO", 5),
    ("email queued to=<*> template=<*>", "INFO", 3),
    ("slow query table=<*> duration=<*>ms", "WARNING", 2),
]

DEPLOY = [
    ("deploy starting version=<*> commit=<*>", "INFO"),
    ("deploy finished version=<*> replicas=<*> duration=<*>s", "INFO"),
    ("config reloaded file=<*> keys=<*>", "INFO"),
    ("cache warm started shards=<*> entries=<*>", "INFO"),
]

ONSET = [
    ("upstream timeout path=<*> after <*>ms", "ERROR"),
    ("connection pool exhausted pool=<*> waiting=<*>", "ERROR"),
    ("cache warm started shards=<*> entries=<*>", "INFO"),
]

PEAK = [
    ("checkout failed user=<*> reason=upstream_timeout order=<*>", "ERROR"),
    ("retry scheduled attempt=<*> operation=<*> backoff=<*>ms", "WARNING"),
    ("circuit breaker open dependency=<*> failures=<*>", "ERROR"),
]

RECOVERY = [
    ("payment settled id=<*> attempts=<*> duration=<*>ms", "INFO"),
    ("circuit breaker half-open dependency=<*>", "WARNING"),
    ("still degraded replicas=<*> target=<*>", "WARNING"),
]

HOSTS = ["api-01", "api-02", "api-03", "worker-01", "worker-02"]
PATHS = ["/v1/cart", "/v1/checkout", "/v1/search", "/v1/user/me", "/v1/catalog"]
SERVICES = ["api", "checkout", "db", "cache", "payments", "worker"]
UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "curl/8.5.0",
    "python-requests/2.32",
]
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

SPAN_MINUTES = 27.0


#: Slots whose value is a category from a fixed vocabulary.
SLOT_VALUES = {
    "method": ("GET", "POST", "PUT"),
    "status": ("200", "200", "200", "201"),
    "component": ("api", "checkout", "db", "cache", "payments", "worker"),
    "table": ("orders", "users", "sessions", "catalog"),
    "operation": ("payment.charge", "order.create", "cart.update"),
    "dependency": ("payments", "catalog", "identity", "search"),
    "reason": ("upstream_timeout",),
    "group": ("orders", "billing", "catalog"),
    "template": ("welcome", "receipt", "reset-password"),
    "file": ("app.yaml", "routes.yaml", "limits.yaml"),
    "path": PATHS,
    "user": ("alice", "bob", "carol", "dave", "erin", "frank"),
    "from": ("10.0.0.7", "10.0.1.22", "10.0.2.9", "10.0.3.51"),
    "to": ("alice@example.com", "bob@example.com", "carol@example.com"),
    "version": ("2.14.0", "2.14.1", "2.15.0"),
    "target": ("12", "24", "36"),
    "shards": ("8", "16", "32"),
}
#: Slots whose value is a plain integer.
SLOT_NUMERIC = {
    "rows", "amount", "ttl", "lag", "attempt", "uptime", "entries", "replicas",
    "failures", "partition", "keys", "duration", "backoff", "after", "fill",
}
#: Slots whose value is a hex identifier.
SLOT_HEX = {"id", "key", "commit", "order", "trace", "span"}


def _value(slot: str, rng: random.Random) -> str:
    """Pick a plausible value for a named <*> slot."""
    if slot in SLOT_VALUES:
        return rng.choice(SLOT_VALUES[slot])
    if slot in SLOT_NUMERIC:
        return str(rng.randrange(1, 1200))
    if slot in SLOT_HEX:
        return "%08x" % rng.getrandbits(32)
    kind = rng.randrange(0, 3)
    if kind == 0:
        return str(rng.randrange(1, 99999))
    if kind == 1:
        return "%dms" % rng.randrange(1, 1200)
    return rng.choice(PATHS)


def _fill(template: str, rng: random.Random) -> str:
    """Replace every <*> slot with a plausible value, keyed by the slot name.

    The slot name is the "key=" immediately before the marker; a marker glued to
    a unit ("<*>ms") gets a bare number with that unit appended.
    """
    parts = template.split("<*>")
    out = parts[0]
    for index, part in enumerate(parts[1:]):
        before = parts[index]
        token = before.split()[-1] if before.split() else ""
        slot = token[:-1] if token.endswith("=") else ""
        if part.startswith("ms"):
            out += str(rng.randrange(1, 1200)) + part
        else:
            out += _value(slot, rng) + part
    return out


def _app_line(stamp: _dt.datetime, level: str, message: str, rng: random.Random) -> str:
    return "%s %-7s %-9s %s" % (
        stamp.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (stamp.microsecond // 1000),
        level,
        rng.choice(SERVICES),
        message,
    )


def generate(lines: int, seed: int, start: _dt.datetime) -> list[str]:
    """Build lines of app log containing exactly one real incident."""
    rng = random.Random(seed)
    weights = [(template, level, weight) for template, level, weight in TEMPLATES]
    total_weight = sum(w for _, _, w in weights)
    step = (SPAN_MINUTES * 60.0) / max(1, lines)
    clock = start.timestamp()
    out: list[str] = []
    while len(out) < lines:
        clock += step
        stamp = _dt.datetime.fromtimestamp(clock)
        minute = (clock - start.timestamp()) / 60.0
        if 12.0 <= minute < 12.4:
            template, level = DEPLOY[min(len(DEPLOY) - 1, int((minute - 12.0) * 8))]
            out.append(_app_line(stamp, level, _fill(template, rng), rng))
            continue
        if 12.4 <= minute < 13.0 and rng.random() < 0.35:
            # A trickle of brand-new errors before the storm: the deploy went out
            # and the first requests started failing.
            template, level = ONSET[rng.randrange(0, len(ONSET))]
            out.append(_app_line(stamp, level, _fill(template, rng), rng))
            continue
        pressure = 0.0
        if 13.0 <= minute < 18.0:
            pressure = min(1.0, (minute - 13.0) / 3.0)
        elif 18.0 <= minute < 26.0:
            pressure = max(0.0, 1.0 - (minute - 18.0) / 8.0)
        if pressure > 0 and rng.random() < 0.06 + 0.44 * pressure:
            template, level = PEAK[rng.randrange(0, len(PEAK))]
            out.append(_app_line(stamp, level, _fill(template, rng), rng))
            continue
        if minute >= 26.0 and rng.random() < 0.06:
            template, level = RECOVERY[rng.randrange(0, len(RECOVERY))]
            out.append(_app_line(stamp, level, _fill(template, rng), rng))
            continue
        pick = rng.randrange(total_weight)
        running = 0
        chosen = weights[0]
        for item in weights:
            running += item[2]
            if pick < running:
                chosen = item
                break
        template, level, _ = chosen
        out.append(_app_line(stamp, level, _fill(template, rng), rng))
    return out[:lines]


def generate_nginx(lines: int, seed: int, start: _dt.datetime) -> list[str]:
    """Access-log sample with the same incident window visible as 5xx."""
    rng = random.Random(seed + 1)
    step = (SPAN_MINUTES * 60.0) / max(1, lines)
    clock = start.timestamp()
    out: list[str] = []
    for _ in range(lines):
        clock += step
        stamp = _dt.datetime.fromtimestamp(clock)
        minute = (clock - start.timestamp()) / 60.0
        status = 200
        if 13.0 <= minute < 18.0:
            pressure = min(1.0, (minute - 13.0) / 3.0)
            if rng.random() < 0.05 + 0.45 * pressure:
                status = rng.choice((500, 502, 503, 504))
        elif 18.0 <= minute < 26.0:
            pressure = max(0.0, 1.0 - (minute - 18.0) / 8.0)
            if rng.random() < 0.05 + 0.35 * pressure:
                status = rng.choice((500, 502, 503))
        elif rng.random() < 0.004:
            status = rng.choice((301, 304, 404))
        path = rng.choice(PATHS) + ("/checkout" if status >= 500 else "")
        size = rng.randrange(300, 18000)
        duration = rng.randrange(4, 900) if status < 500 else rng.randrange(3000, 12000)
        stamp_text = "%02d/%s/%04d:%02d:%02d:%02d +0000" % (
            stamp.day, MONTHS[stamp.month - 1], stamp.year,
            stamp.hour, stamp.minute, stamp.second,
        )
        out.append(
            '%s - - [%s] "%s %s HTTP/1.1" %d %d "-" "%s" %d'
            % (
                "10.0.%d.%d" % (rng.randrange(0, 4), rng.randrange(1, 254)),
                stamp_text,
                rng.choice(("GET", "POST")),
                path,
                status,
                size,
                rng.choice(UA),
                duration,
            )
        )
    return out


_LINE_RE = __import__("re").compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) +(\S+) +(\S+) +(.*)$"
)


def _split_line(line: str) -> tuple[str, str, str, str]:
    """Split a generated app line into (timestamp, level, service, message)."""
    match = _LINE_RE.match(line)
    if match is None:
        raise ValueError("cannot split generated line: %r" % line)
    return match.group(1), match.group(2), match.group(3), match.group(4)


def _to_json(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        stamp, level, logger, message = _split_line(line)
        out.append(json.dumps(
            {"timestamp": stamp, "level": level, "logger": logger, "message": message},
            ensure_ascii=False,
        ))
    return out


def _to_syslog(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        stamp, level, logger, message = _split_line(line)
        moment = _dt.datetime.fromisoformat(stamp)
        pri = {"ERROR": 11, "WARNING": 12, "INFO": 14, "DEBUG": 15}.get(level, 14)
        out.append(
            "<%d>1 %s host-%s %s - - %s"
            % (
                pri,
                moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000),
                logger,
                logger,
                message,
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gen.py",
        description="Generate the deterministic demo logs used by logscope's README.",
    )
    parser.add_argument("--lines", type=int, default=20000, help="number of lines (default 20000)")
    parser.add_argument("--out", default="examples/app.log", help="output path")
    parser.add_argument("--format", default="app", choices=("app", "nginx", "json", "syslog"),
                        help="dialect to emit (default app)")
    parser.add_argument("--seed", type=int, default=20260512, help="RNG seed (default 20260512)")
    parser.add_argument("--start", default="2026-05-12T09:00:00", help="first timestamp (ISO)")
    args = parser.parse_args(argv)
    if args.lines < 1:
        sys.stderr.write("gen.py: error: --lines must be >= 1\n")
        return 2
    start = _dt.datetime.fromisoformat(args.start)
    if args.format == "nginx":
        lines = generate_nginx(args.lines, args.seed, start)
    elif args.format == "json":
        lines = _to_json(generate(args.lines, args.seed, start))
    elif args.format == "syslog":
        lines = _to_syslog(generate(args.lines, args.seed, start))
    else:
        lines = generate(args.lines, args.seed, start)
    path = pathlib.Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.stdout.write("wrote %s (%d lines, %d bytes)\n"
                     % (path, len(lines), path.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
