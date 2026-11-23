"""Reproduce every performance number quoted in the README.

    python benchmarks/run.py            # writes results.json + RESULTS.md
    python benchmarks/run.py --scale    # adds the 200 MB end-to-end run

Nothing here is cached or hard-coded: the script generates its own inputs, runs
the real pipeline through subprocess-free in-process calls, and writes whatever
it measured. Numbers in the README come from this file's output.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks"))

from gen import generate  # noqa: E402

from logscope.engine import AnalysisConfig, analyze  # noqa: E402  # noqa: E402

START = _dt.datetime(2026, 5, 12, 9, 0, 0)
SIZES = (5000, 20000, 60000)


def _write(lines: int, seed: int, directory: pathlib.Path) -> pathlib.Path:
    path = directory / ("bench-%d.log" % lines)
    body = "\n".join(generate(lines, seed, START)) + "\n"
    path.write_text(body, encoding="utf-8")
    return path


def _time_once(path: pathlib.Path) -> tuple[float, int, int, int]:
    started = time.perf_counter()
    analysis = analyze(str(path), AnalysisConfig())
    elapsed = time.perf_counter() - started
    return elapsed, analysis.template_count, len(analysis.anomalies), len(analysis.incidents)


def _peak_memory(path: pathlib.Path) -> int:
    tracemalloc.start()
    analyze(str(path), AnalysisConfig())
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def _peak_rss_mib(argv: list[str]) -> tuple[float, float]:
    """Run a command and return (elapsed_s, peak resident set in MiB).

    Windows has no getrusage, so the child is polled through psapi; POSIX reads
    RUSAGE_CHILDREN after the child exits. Both report the true high-water mark.
    """
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    started = time.perf_counter()
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes as wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32")
        proc = subprocess.Popen(argv, env=env)
        peak = 0
        while proc.poll() is None:
            handle = kernel32.OpenProcess(0x0400 | 0x1000, False, proc.pid)
            if handle:
                counters = _Counters()
                counters.cb = ctypes.sizeof(_Counters)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    peak = max(peak, counters.PeakWorkingSetSize)
                kernel32.CloseHandle(handle)
            time.sleep(0.05)
        elapsed = time.perf_counter() - started
        return elapsed, peak / (1024 * 1024)

    import resource

    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    subprocess.run(argv, env=env, check=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    elapsed = time.perf_counter() - started
    peak_kib = max(0, after - before) if sys.platform != "darwin" else (after - before) / 1024
    return elapsed, peak_kib / 1024


def measure_scale(directory: pathlib.Path) -> dict[str, object]:
    """End-to-end run on a ~200 MB log, measured as a separate process."""
    path = _write(2_100_000, 99, directory)
    size_mb = path.stat().st_size / (1024 * 1024)
    argv = [sys.executable, "-m", "logscope", "analyze", str(path), "--color", "never", "--quiet"]
    elapsed, peak = _peak_rss_mib(argv)
    path.unlink()
    return {
        "lines": 2_100_000,
        "size_mb": round(size_mb, 1),
        "seconds": round(elapsed, 1),
        "peak_mib": round(peak, 1),
        "mb_per_s": round(size_mb / elapsed, 1),
    }


def measure(directory: pathlib.Path) -> list[dict[str, object]]:
    rows = []
    for lines in SIZES:
        path = _write(lines, 20260512, directory)
        size_mb = path.stat().st_size / (1024 * 1024)
        runs = []
        for _ in range(3):
            elapsed, templates, anomalies, incidents = _time_once(path)
            runs.append(elapsed)
        best = min(runs)
        median = statistics.median(runs)
        rows.append(
            {
                "lines": lines,
                "size_mb": round(size_mb, 3),
                "templates": templates,
                "anomalies": anomalies,
                "incidents": incidents,
                "median_s": round(median, 3),
                "best_s": round(best, 3),
                "mb_per_s": round(size_mb / median, 2),
                "peak_mb": round(_peak_memory(path) / (1024 * 1024), 2),
            }
        )
        path.unlink()
    return rows


def write_markdown(rows: list[dict[str, object]], path: pathlib.Path) -> None:
    lines = [
        "| input | MB | median | throughput | templates | anomalies | incidents | peak MiB |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| %s lines | %.2f | %.2fs | %.1f MB/s | %d | %d | %d | %.1f |"
            % (
                format(int(row["lines"]), ","), float(row["size_mb"]),
                float(row["median_s"]), float(row["mb_per_s"]),
                int(row["templates"]), int(row["anomalies"]),
                int(row["incidents"]), float(row["peak_mb"]),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "benchmarks")
    parser.add_argument("--out", default=str(ROOT / "benchmarks" / "results.json"))
    parser.add_argument("--markdown", default=str(ROOT / "benchmarks" / "RESULTS.md"))
    parser.add_argument("--scale", action="store_true",
                        help="also analyse a ~200 MB log and report wall time and peak RSS")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        rows = measure(pathlib.Path(tmp))
        scale = measure_scale(pathlib.Path(tmp)) if args.scale else None
    payload = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "generated_by": "benchmarks/run.py",
        "runs_per_size": 3,
        "results": rows,
        "scale": scale,
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    markdown = pathlib.Path(args.markdown)
    write_markdown(rows, markdown)
    print(markdown.read_text(encoding="utf-8"))
    if scale is not None:
        print(
            "scale: %s lines, %.1f MB -> %.1fs end-to-end, %.1f MiB peak RSS, %.1f MB/s"
            % (
                format(int(scale["lines"]), ","), float(scale["size_mb"]),
                float(scale["seconds"]), float(scale["peak_mib"]),
                float(scale["mb_per_s"]),
            )
        )
    print("wrote %s and %s" % (out, markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
