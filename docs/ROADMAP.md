# Roadmap

The honest list of what v1.0 does not do, in rough priority order. Anything that
was cut from the release is recorded here rather than quietly dropped.

## Cut from v1.0, deliberately

1. **Multi-file ingestion.** Each analysis command takes one path (or stdin).
   `logscope analyze a.log b.log` exits 2 with a message telling you to
   concatenate first. Merging several files with different dialects needs a
   per-file clock reconciliation story, and a half-done version would produce
   misleading timelines -- so it waits for a real design.
2. **Multi-line stack traces.** A Java or Python traceback is currently treated
   as N separate events; only the first line carries a timestamp and the rest
   are bucketed onto it. Stack-aware folding is the single biggest quality gap
   for application logs.
3. **Kubernetes / Docker log prefix unwrapping.** Docker JSON is supported as
   JSON lines, but the inner `log`/`stream` envelope of `docker logs` and the
   CRI `2016-... stdout F` prefix are not yet peeled before template mining.
4. **Time-zone aware buckets.** Timestamps are normalised to naive local time
   taken from the log itself. `--tz` is not implemented; a log mixing offsets
   will bucket by wall-clock time, not by instant.
5. **Windows Event Log export.** Only text logs are read.

## Next

- **Stack folding.** Continuation-line detection (no timestamp + indented or
  starts with "at " / "Traceback" / "Caused by") attached to the parent event.
- **Per-host baselines.** Detecting that api-01 spikes while api-02 stays flat is
  a much stronger signal than a service-wide spike. Needs a `--group-by` flag
  and a per-group EWMA.
- **Trend-aware novelty.** The current novelty score is count x novelty against
  the whole file. A template that is rare *and* rising fast should rank higher
  than one that is rare and dead.
- **SARIF / JUnit output** so `--fail-on error` findings can be surfaced in code
  scanning or CI test reports.
- **Template curation file** (`logscope.toml`) for teams that want to pin known
  templates or suppress a noisy one without lowering `--threshold` globally.

## Not planned

- Streaming/daemon mode tailing a live file. This is a batch analyser; for live
  tailing, pipe the last N lines through stdin.
- A web UI. The HTML report is deliberately a single static file.
