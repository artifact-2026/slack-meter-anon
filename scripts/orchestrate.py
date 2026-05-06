#!/usr/bin/env python3
"""
Slack Meter Orchestrator
========================
Runs saturation and slack-measurement experiments by spawning `worker`
processes and collecting their JSON output.

Usage
-----
    python3 scripts/orchestrate.py [OPTIONS]

Options
-------
    --mode          saturation | slack-cpu | slack-io | full  (default: full)
    --io-mix        baseline io_mix  (default: 0.3)
    --intensity     baseline intensity  (default: 0.75)
    --duration      seconds per worker run  (default: 30)
    --max-procs     max processes in saturation sweep  (default: 32)
    --tmp-dir       scratch directory for I/O ops  (default: /tmp/slack-meter)
    --output        path for JSON results file  (default: results/experiment.json)
    --drop-pct      fraction throughput drop counted as interference  (default: 0.05)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Locate the worker binary.  Expect it in <repo>/build/worker.
# ---------------------------------------------------------------------------
REPO_ROOT   = Path(__file__).parent.parent.resolve()
WORKER_BIN  = REPO_ROOT / "build" / "worker"
DEFAULT_TMP = "/tmp/slack-meter"
DEFAULT_DUR = 30


def _check_worker() -> None:
    if not WORKER_BIN.exists():
        sys.exit(
            f"[ERROR] worker binary not found at {WORKER_BIN}\n"
            "  Run: cmake -B build && cmake --build build"
        )


# ---------------------------------------------------------------------------
# Worker helpers
# ---------------------------------------------------------------------------

def spawn_workers(
    n: int,
    io_mix: float,
    intensity: float,
    duration: int,
    tmp_dir: str,
) -> list[dict]:
    """Launch n workers in parallel, wait for all, return parsed JSON results."""
    procs = [
        subprocess.Popen(
            [
                str(WORKER_BIN),
                "--io-mix",    str(io_mix),
                "--intensity", str(intensity),
                "--duration",  str(duration),
                "--tmp-dir",   tmp_dir,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(n)
    ]
    results: list[dict] = []
    for p in procs:
        stdout, _ = p.communicate()
        if p.returncode == 0 and stdout.strip():
            try:
                results.append(json.loads(stdout.strip()))
            except json.JSONDecodeError as e:
                print(f"[WARN] JSON parse error: {e}", file=sys.stderr)
    return results


def total_throughput(results: list[dict]) -> float:
    return sum(r["throughput"] for r in results)


# ---------------------------------------------------------------------------
# Saturation experiment
# ---------------------------------------------------------------------------

def run_saturation(
    io_mix: float   = 0.3,
    intensity: float = 0.75,
    max_procs: int  = 32,
    duration: int   = DEFAULT_DUR,
    tmp_dir: str    = DEFAULT_TMP,
) -> dict:
    """
    Ramp the number of baseline-workload processes from 1 to max_procs.
    Stop after two consecutive drops in aggregate throughput.

    Returns a dict with:
        saturation_procs  – number of processes at peak throughput
        peak_throughput   – peak aggregate ops/sec
        data_points       – list of {n_procs, throughput}
    """
    print(f"\n[saturation] io_mix={io_mix}  intensity={intensity}  max_procs={max_procs}")
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)

    data_points: list[dict]  = []
    peak_tput:   float       = 0.0
    peak_procs:  int         = 1
    consecutive_drops: int   = 0

    for n in range(1, max_procs + 1):
        print(f"[saturation]  n={n} ...", end=" ", flush=True)
        results = spawn_workers(n, io_mix, intensity, duration, tmp_dir)
        if not results:
            print("(no results – skipping)")
            continue

        tput = total_throughput(results)
        data_points.append({"n_procs": n, "throughput": tput})
        print(f"throughput={tput:.1f} ops/s")

        if tput > peak_tput:
            peak_tput   = tput
            peak_procs  = n
            consecutive_drops = 0
        else:
            consecutive_drops += 1

        if consecutive_drops >= 2:
            print(f"[saturation] >> saturated at n={peak_procs}  peak={peak_tput:.1f} ops/s")
            break

    return {
        "type":             "saturation",
        "params":           {"io_mix": io_mix, "intensity": intensity},
        "data_points":      data_points,
        "saturation_procs": peak_procs,
        "peak_throughput":  peak_tput,
    }


# ---------------------------------------------------------------------------
# Slack measurement
# ---------------------------------------------------------------------------

def measure_slack(
    baseline_procs:     int,
    baseline_io_mix:    float,
    baseline_intensity: float,
    baseline_tput:      float,
    slack_resource:     str,   # "cpu" or "io"
    duration:           int   = DEFAULT_DUR,
    tmp_dir:            str   = DEFAULT_TMP,
    drop_pct:           float = 0.05,
) -> dict:
    """
    Determine how much of `slack_resource`-only load can be added before the
    baseline workload's throughput drops by drop_pct.

    Algorithm
    ---------
    For each additional slack-process count (1, 2, 3, …):
      Binary-search intensity in [0.05, 1.0] to find the first intensity that
      causes a drop.  If intensity=1.0 causes no drop, add another process.

    The slack measurement is (procs, intensity): the point at which
    interference first appeared.
    """
    print(f"\n[slack-{slack_resource}]  baseline_procs={baseline_procs}"
          f"  baseline_tput={baseline_tput:.1f}")

    # Pure CPU → io_mix=0 ;  Pure I/O → io_mix=1
    slack_io_mix = 0.0 if slack_resource == "cpu" else 1.0

    data_points: list[dict] = []

    def probe(slack_n: int, slack_intensity: float) -> tuple[bool, float]:
        """
        Run baseline workers and slack workers simultaneously (best effort).
        Returns (dropped, observed_baseline_tput).
        """
        # We run both sets of workers concurrently in the same shell; each
        # worker already runs for `duration` seconds, so they naturally overlap.
        base_results  = spawn_workers(
            baseline_procs, baseline_io_mix, baseline_intensity, duration, tmp_dir
        )
        slack_results = spawn_workers(
            slack_n, slack_io_mix, slack_intensity, duration, tmp_dir
        )
        obs_tput  = total_throughput(base_results)
        drop_frac = (baseline_tput - obs_tput) / max(baseline_tput, 1.0)
        dropped   = drop_frac >= drop_pct
        return dropped, obs_tput

    slack_procs:     int   = 1
    slack_intensity: float = 1.0
    found: bool            = False
    MAX_SLACK_PROCS        = 16

    while slack_procs <= MAX_SLACK_PROCS:
        print(f"[slack-{slack_resource}]  sweeping intensity with {slack_procs} extra proc(s)")
        lo, hi    = 0.05, 1.0
        drop_seen = False

        # ~6 bisections → precision ≈ 0.015
        for step in range(6):
            mid = (lo + hi) / 2.0
            dropped, obs = probe(slack_procs, mid)
            tag = "DROP" if dropped else "ok"
            print(f"[slack-{slack_resource}]    intensity={mid:.3f}  obs_tput={obs:.1f}  [{tag}]")
            data_points.append({
                "slack_procs":          slack_procs,
                "slack_intensity":      mid,
                "baseline_throughput":  obs,
                "dropped":              dropped,
            })
            if dropped:
                hi        = mid
                drop_seen = True
            else:
                lo = mid

        if drop_seen:
            slack_intensity = hi
            found = True
            break

        # No drop even at intensity=1 — check explicitly before adding a process
        dropped_max, obs_max = probe(slack_procs, 1.0)
        data_points.append({
            "slack_procs":         slack_procs,
            "slack_intensity":     1.0,
            "baseline_throughput": obs_max,
            "dropped":             dropped_max,
        })
        if dropped_max:
            slack_intensity = 1.0
            found = True
            break

        print(f"[slack-{slack_resource}]  no drop at intensity=1.0; adding another process")
        slack_procs += 1

    if not found:
        print(f"[slack-{slack_resource}]  WARNING: slack boundary not found within {MAX_SLACK_PROCS} processes")

    print(f"[slack-{slack_resource}]  result = ({slack_procs}, {slack_intensity:.3f})")

    return {
        "type":              "slack",
        "resource":          slack_resource,
        "slack_measurement": {"procs": slack_procs, "intensity": slack_intensity},
        "data_points":       data_points,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Slack Meter Orchestrator")
    parser.add_argument("--mode",       choices=["saturation", "slack-cpu", "slack-io", "full"],
                        default="full")
    parser.add_argument("--io-mix",     type=float, default=0.3,   metavar="F")
    parser.add_argument("--intensity",  type=float, default=0.75,  metavar="F")
    parser.add_argument("--duration",   type=int,   default=DEFAULT_DUR, metavar="S")
    parser.add_argument("--max-procs",  type=int,   default=32,    metavar="N")
    parser.add_argument("--tmp-dir",    default=DEFAULT_TMP)
    parser.add_argument("--output",     default="results/experiment.json")
    parser.add_argument("--drop-pct",   type=float, default=0.05,  metavar="F",
                        help="Fraction of baseline throughput drop to count as interference")
    args = parser.parse_args()

    _check_worker()
    os.makedirs("results", exist_ok=True)
    all_results: list[dict] = []
    sat: Optional[dict]     = None

    if args.mode in ("saturation", "full"):
        sat = run_saturation(
            io_mix    = args.io_mix,
            intensity = args.intensity,
            max_procs = args.max_procs,
            duration  = args.duration,
            tmp_dir   = args.tmp_dir,
        )
        all_results.append(sat)

    if args.mode in ("slack-cpu", "slack-io", "full") and sat:
        base_procs = sat["saturation_procs"]
        base_tput  = sat["peak_throughput"]

        if args.mode in ("slack-cpu", "full"):
            all_results.append(measure_slack(
                baseline_procs     = base_procs,
                baseline_io_mix    = args.io_mix,
                baseline_intensity = args.intensity,
                baseline_tput      = base_tput,
                slack_resource     = "cpu",
                duration           = args.duration,
                tmp_dir            = args.tmp_dir,
                drop_pct           = args.drop_pct,
            ))

        if args.mode in ("slack-io", "full"):
            all_results.append(measure_slack(
                baseline_procs     = base_procs,
                baseline_io_mix    = args.io_mix,
                baseline_intensity = args.intensity,
                baseline_tput      = base_tput,
                slack_resource     = "io",
                duration           = args.duration,
                tmp_dir            = args.tmp_dir,
                drop_pct           = args.drop_pct,
            ))

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n[done] Results → {out_path}")


if __name__ == "__main__":
    main()
