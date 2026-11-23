# Changelog

All notable changes to logscope. Format follows Keep a Changelog,
versioning follows Semantic Versioning.

## [1.0.0] - 2026-05-12

The first release. Everything below is implemented, tested and reproducible from
the commands in README.md.

### Added

- Streaming ingestion with constant memory for five dialects: JSON lines, RFC 5424
  and RFC 3164 syslog, nginx/apache combined access logs, Python logging output
  and a generic timestamp-scavenging fallback. Auto-detection samples the first
  lines and reports its scores through "logscope formats".
- Drain-style template mining in pure Python: tokenisation, variable masking
  (<*>), a bucketed similarity walk, per-template histograms, samples and raw
  evidence lines.
- Four anomaly detectors -- EWMA/robust-z rate spikes, trailing bursts, new
  templates and new error classes -- each emitting a one-line human explanation.
- Incident grouping by time proximity plus template relation, with overlapping
  windows merged, severity labels (SEV-1/2/3) and a ranked root-cause list where
  every candidate carries the reasons it was chosen and the raw lines that prove
  it was there.
- analyze, incidents, timeline, templates, report and formats subcommands, --json
  on all analysis commands, --since/--until/--grep filters, --fail-on for CI
  gates, and documented exit codes 0/1/2.
- A single-file dark HTML report with inline SVG sparklines: no CDN, no
  JavaScript, no network.
- Deterministic demo generator (benchmarks/gen.py) and a reproducible benchmark
  harness (benchmarks/run.py).

[1.0.0]: https://github.com/kele-tao/logscope/releases/tag/v1.0.0
