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
    --sat-epsilon   saturation non-increasing threshold.  A measurement resets
                    the counter (counts as "still growing") only when
                        tput > sat_epsilon * peak_tput
                    With sat_epsilon=1.0 any non-new-peak counts (original
                    behavior).  Values > 1.0 require a minimum improvement
                    margin before resetting; e.g. 1.02 means throughput must
                    beat the current peak by at least 2%% to be considered
                    progress.  This prevents tiny sub-noise gains from
                    indefinitely deferring saturation detection on a plateau.
                    (default: 1.02)
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
    base_seed: int = 42,
    seed_offset: int = 0,
) -> list[dict]:
    """Launch n workers in parallel, wait for all, return parsed JSON results.

    Each worker gets seed = base_seed + seed_offset + worker_index so that
    workers within a run diverge from each other, but the overall experiment
    is fully reproducible given the same base_seed.
    """
    procs = [
        subprocess.Popen(
            [
                str(WORKER_BIN),
                "--io-mix",    str(io_mix),
                "--intensity", str(intensity),
                "--duration",  str(duration),
                "--tmp-dir",   tmp_dir,
                "--seed",      str(base_seed + seed_offset + i),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        for i in range(n)
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
    """Combined (cpu + io) ops/s — used for saturation phase."""
    return sum(r["throughput"] for r in results)


def total_cpu_throughput(results: list[dict]) -> float:
    """CPU-only ops/s across all workers."""
    return sum(r.get("cpu_throughput", 0.0) for r in results)


def total_io_throughput(results: list[dict]) -> float:
    """I/O-only ops/s across all workers."""
    return sum(r.get("io_throughput", 0.0) for r in results)


def tput_for_resource(results: list[dict], resource: str) -> float:
    """Return the throughput dimension that matches the slack resource type.

    For slack measurement we compare like-for-like:
      - CPU slack probes  → baseline cpu_throughput (unaffected by I/O contention)
      - I/O slack probes  → baseline io_throughput  (unaffected by CPU contention)
    """
    if resource == "cpu":
        return total_cpu_throughput(results)
    elif resource == "io":
        return total_io_throughput(results)
    else:
        return total_throughput(results)


# ---------------------------------------------------------------------------
# Saturation experiment
# ---------------------------------------------------------------------------

def run_saturation(
    io_mix: float              = 0.3,
    intensity: float           = 0.75,
    max_procs: int             = 32,
    min_procs: int             = 4,
    duration: int              = DEFAULT_DUR,
    tmp_dir: str               = DEFAULT_TMP,
    base_seed: int             = 42,
    sat_epsilon: float         = 1.02,
    marginal_threshold: float  = 0.05,
    min_consecutive_small: int = 1,
) -> dict:
    """
    Ramp the number of baseline-workload processes from 1 to max_procs and
    stop when throughput has stopped growing meaningfully.

    Two complementary stopping conditions are checked after every step.
    Both require n >= min_procs before they can fire.

    1. Diminishing-returns (primary): stop when the step-to-step relative
       gain drops below `marginal_threshold` for `min_consecutive_small`
       consecutive steps.  This catches the "elbow" of the throughput curve
       early — even when absolute throughput is still creeping up.

         rel_gain = (tput - prev_tput) / prev_tput
         if rel_gain < marginal_threshold  →  increment small-gains counter

       Default: marginal_threshold=0.05 (5%), min_consecutive_small=1.
       Raise min_consecutive_small to 2 for more noise tolerance (reports
       one step later but is immune to a single anomalous measurement).

    2. Post-peak plateau (safety net): stop after two consecutive steps that
       fail to exceed sat_epsilon * peak_tput.  This catches cases where
       throughput actually *declines* after the peak, which the relative-gain
       check may miss if the drop is gradual.

         tput > sat_epsilon * peak_tput  →  reset counter, update peak
         otherwise                       →  increment non-increasing counter

    sat_epsilon must be >= 1.0.  Values < 1.0 are wrong: they allow declining
    measurements to pass (tput > 0.98*peak is trivially true for small drops),
    causing the peak to be updated downward and the counter to reset on every
    step.

    Returns a dict with:
        saturation_procs      – number of processes at peak throughput
        peak_throughput       – peak aggregate ops/sec
        data_points           – list of {n_procs, throughput}
        sat_epsilon           – epsilon value used
        marginal_threshold    – marginal-gain threshold used
        min_consecutive_small – consecutive-small-steps threshold used
    """
    print(f"\n[saturation] io_mix={io_mix}  intensity={intensity}"
          f"  max_procs={max_procs}  min_procs={min_procs}"
          f"  marginal_threshold={marginal_threshold:.0%}  sat_epsilon={sat_epsilon}")
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)

    data_points: list[dict]         = []
    peak_tput:   float              = 0.0
    peak_procs:  int                = 1
    prev_tput:   float              = 0.0
    consecutive_non_increasing: int = 0
    consecutive_small_gains:    int = 0

    for n in range(1, max_procs + 1):
        print(f"[saturation]  n={n} ...", end=" ", flush=True)
        results = spawn_workers(n, io_mix, intensity, duration, tmp_dir,
                                base_seed=base_seed, seed_offset=n * 1000)
        if not results:
            print("(no results – skipping)")
            continue

        tput = total_throughput(results)
        data_points.append({"n_procs": n, "throughput": tput})

        # --- condition 1: diminishing returns (step-to-step relative gain) ---
        if prev_tput > 0:
            rel_gain = (tput - prev_tput) / prev_tput
            if rel_gain < marginal_threshold:
                consecutive_small_gains += 1
            else:
                consecutive_small_gains = 0
        else:
            rel_gain = float("inf")

        # --- condition 2: post-peak plateau (absolute comparison to peak) ---
        if tput > sat_epsilon * peak_tput:
            peak_tput  = tput
            peak_procs = n
            consecutive_non_increasing = 0
        else:
            consecutive_non_increasing += 1

        print(f"throughput={tput:.1f} ops/s  rel_gain={rel_gain:+.1%}"
              f"  small={consecutive_small_gains}  non-incr={consecutive_non_increasing}")

        prev_tput = tput

        if n >= min_procs:
            if consecutive_small_gains >= min_consecutive_small:
                print(f"[saturation] >> saturated (diminishing returns) at"
                      f" n={peak_procs}  peak={peak_tput:.1f} ops/s")
                break
            if consecutive_non_increasing >= 2:
                print(f"[saturation] >> saturated (post-peak plateau) at"
                      f" n={peak_procs}  peak={peak_tput:.1f} ops/s")
                break

    return {
        "type":                "saturation",
        "params":              {"io_mix": io_mix, "intensity": intensity,
                                "seed": base_seed, "sat_epsilon": sat_epsilon},
        "data_points":         data_points,
        "saturation_procs":    peak_procs,
        "peak_throughput":     peak_tput,
        "sat_epsilon":         sat_epsilon,
        "marginal_threshold":  marginal_threshold,
        "min_consecutive_small": min_consecutive_small,
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
    base_seed:          int   = 42,
) -> dict:
    """
    Determine how much of `slack_resource`-only load can be added before the
    baseline workload's throughput drops by drop_pct.

    Throughput comparison is type-matched to the slack resource:
      - slack_resource="cpu"  → drop detection uses baseline cpu_throughput only
      - slack_resource="io"   → drop detection uses baseline io_throughput only
    This ensures a CPU-only slack thread cannot trigger a false drop via its
    effect on I/O, and vice versa.

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

    probe_index = 0

    def probe(slack_n: int, slack_intensity: float,
              ref_tput: float | None = None) -> tuple[bool, float]:
        nonlocal probe_index
        """
        Spawn baseline and slack workers simultaneously in one batch, wait for
        all to finish, then return (dropped, observed_type_matched_baseline_tput,
        observed_type_matched_slack_tput).

        Critically: all Popen() calls happen before any communicate(), so every
        worker is running at the same time and competing for the same resources.

        Drop detection uses the throughput dimension that matches slack_resource
        (cpu_throughput for CPU slack, io_throughput for I/O slack), so we only
        flag genuine contention on the resource under test.

        ref_tput: reference throughput to compare against for drop detection.
                  Should be the calibrated live reference for this slack_n round.
                  Defaults to baseline_tput from saturation if not given.
        """
        def make_cmd(io_mix: float, intensity: float, seed: int) -> list[str]:
            return [
                str(WORKER_BIN),
                "--io-mix",    str(io_mix),
                "--intensity", str(intensity),
                "--duration",  str(duration),
                "--tmp-dir",   tmp_dir,
                "--seed",      str(seed),
            ]

        # Launch all workers before waiting on any of them.
        base_seed_offset  = 100_000 + probe_index * 1000
        slack_seed_offset = 200_000 + probe_index * 1000
        probe_index += 1

        base_procs_list = [
            subprocess.Popen(make_cmd(baseline_io_mix, baseline_intensity,
                                      base_seed + base_seed_offset + i),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            for i in range(baseline_procs)
        ]
        slack_procs_list = [
            subprocess.Popen(make_cmd(slack_io_mix, slack_intensity,
                                      base_seed + slack_seed_offset + i),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            for i in range(slack_n)
        ]

        # Now collect — workers are already running concurrently above.
        base_results:  list[dict] = []
        slack_results: list[dict] = []
        for p in base_procs_list:
            stdout, _ = p.communicate()
            if p.returncode == 0 and stdout.strip():
                try:
                    base_results.append(json.loads(stdout.strip()))
                except json.JSONDecodeError:
                    pass
        for p in slack_procs_list:
            stdout, _ = p.communicate()
            if p.returncode == 0 and stdout.strip():
                try:
                    slack_results.append(json.loads(stdout.strip()))
                except json.JSONDecodeError:
                    pass

        # Use total (cpu + io) throughput for both baseline drop detection and
        # slack reporting, so contention on any resource is reflected.
        obs_tput   = total_throughput(base_results)
        slack_tput = total_throughput(slack_results)
        _ref      = ref_tput if ref_tput is not None else baseline_tput
        drop_frac = (_ref - obs_tput) / max(_ref, 1.0)
        dropped   = drop_frac >= drop_pct
        return dropped, obs_tput, slack_tput

    def calibrate(slack_n: int) -> float:
        """
        Measure the current total baseline throughput with slack_n workers fully
        sleeping (intensity=0).  This accounts for:
          - natural throughput drift since the saturation measurement
          - OS scheduling overhead of having extra processes in the table
        All subsequent probes for this slack_n round compare against this
        live reference, so only active resource contention triggers a drop.
        """
        print(f"[slack-{slack_resource}]  calibrating with {slack_n} sleeping proc(s)...",
              end=" ", flush=True)
        _, tput, _ = probe(slack_n, 0.0)   # intensity=0 → all slack workers sleep
        print(f"current baseline_tput = {tput:.1f} ops/s")
        return tput

    slack_procs:     int   = 1
    slack_intensity: float = 1.0
    found: bool            = False
    MAX_SLACK_PROCS        = 16

    while slack_procs <= MAX_SLACK_PROCS:
        print(f"[slack-{slack_resource}]  sweeping intensity with {slack_procs} extra proc(s)")

        # Calibrate: fresh baseline with these slack workers sleeping.
        # All probes this round compare against this reference, not the stale
        # saturation measurement, eliminating false drops from drift/overhead.
        ref = calibrate(slack_procs)

        # Start the binary search above the previously confirmed safe load.
        # We know (slack_procs-1) procs at intensity=1.0 caused no drop, so
        # set lo = (N-1)/N so that N*lo equals that confirmed-safe total load.
        lo        = (slack_procs - 1) / slack_procs
        hi        = 1.0
        drop_seen = False

        # ~6 bisections → precision ≈ 0.015
        for step in range(6):
            mid = (lo + hi) / 2.0
            dropped, obs, slack_obs = probe(slack_procs, mid, ref_tput=ref)
            tag = "DROP" if dropped else "ok"
            print(f"[slack-{slack_resource}]    intensity={mid:.3f}"
                  f"  baseline_tput={obs:.1f}"
                  f"  slack_tput={slack_obs:.1f}  [{tag}]")
            data_points.append({
                "slack_procs":     slack_procs,
                "slack_intensity": mid,
                "baseline_tput":   obs,
                "slack_tput":      slack_obs,
                "ref_tput":        ref,
                "dropped":         dropped,
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
        dropped_max, obs_max, slack_obs_max = probe(slack_procs, 1.0, ref_tput=ref)
        data_points.append({
            "slack_procs":     slack_procs,
            "slack_intensity": 1.0,
            "baseline_tput":   obs_max,
            "slack_tput":      slack_obs_max,
            "ref_tput":        ref,
            "dropped":         dropped_max,
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
    parser.add_argument("--min-procs",  type=int,   default=4,     metavar="N",
                        help="Minimum processes to sweep before saturation early-stop (default: 4)")
    parser.add_argument("--tmp-dir",    default=DEFAULT_TMP)
    parser.add_argument("--output",     default="results/experiment.json")
    parser.add_argument("--drop-pct",    type=float, default=0.05,  metavar="F",
                        help="Fraction of baseline throughput drop to count as interference")
    parser.add_argument("--sat-epsilon", type=float, default=1.02, metavar="F",
                        help="Min improvement ratio to count as progress (>1.0; default 1.02 → 1%% margin)")
    parser.add_argument("--marginal-threshold", type=float, default=0.05, metavar="F",
                        help="Step-to-step relative gain below which a step counts as 'small' (default 0.05 → 5%%)")
    parser.add_argument("--min-consecutive-small", type=int, default=1, metavar="N",
                        help="Consecutive small-gain steps required to declare saturation (default 1; use 2 for noise tolerance)")
    parser.add_argument("--seed",        type=int,   default=42,   metavar="N",
                        help="Base RNG seed for all workers (fixed for reproducibility)")
    args = parser.parse_args()

    _check_worker()
    os.makedirs("results", exist_ok=True)
    all_results: list[dict] = []
    sat: Optional[dict]     = None

    if args.mode in ("saturation", "full"):
        sat = run_saturation(
            io_mix                = args.io_mix,
            intensity             = args.intensity,
            max_procs             = args.max_procs,
            min_procs             = args.min_procs,
            duration              = args.duration,
            tmp_dir               = args.tmp_dir,
            base_seed             = args.seed,
            sat_epsilon           = args.sat_epsilon,
            marginal_threshold    = args.marginal_threshold,
            min_consecutive_small = args.min_consecutive_small,
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
                base_seed          = args.seed,
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
                base_seed          = args.seed,
            ))

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n[done] Results → {out_path}")


if __name__ == "__main__":
    main()
