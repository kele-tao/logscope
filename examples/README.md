# Examples

Every file here is produced by code in this repository, so the numbers in the
README can be regenerated from scratch.

| File | How it is made | What it demonstrates |
| --- | --- | --- |
| app.log | python benchmarks/gen.py --lines 20000 --out examples/app.log | Python logging dialect, 20k lines, one planted incident |
| nginx.log | python benchmarks/gen.py --lines 4000 --out examples/nginx.log --format nginx | nginx combined access logs with a 5xx storm |
| app.jsonl | python benchmarks/gen.py --lines 800 --out examples/app.jsonl --format json | JSON lines / Docker-style structured logs |

## The story inside app.log

The generator plants a genuine incident, and nothing in the analyser is told
about it:

    09:00-09:11  steady traffic, ~740 lines per minute, healthy 200s
    09:12:00     a deploy lands: deploy / config-reload / cache-warm templates appear
    09:12:24     the first upstream timeouts, then pool exhaustion
    09:13        a checkout failure template that had never been seen before
    09:15-09:17  a retry storm and the circuit breaker opening
    09:18-09:25  recovery, retries succeeded, errors decay back to zero
    09:26        a short tail of half-open breaker warnings, then baseline

Run logscope against it:

    python -m logscope incidents examples/app.log --top 2

Neither the templates, the anomaly thresholds nor the incident grouping are
fitted to this file; the same code runs on nginx.log and on any log you bring.
