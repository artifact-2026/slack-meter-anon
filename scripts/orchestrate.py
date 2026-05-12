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
    Workers are added one at a time.  For each new worker, binary-search its
    intensity in [0, 1.0] while all previously added workers remain locked at
    intensity 1.0.  If a drop is found, the max-ok intensity (lo) is the
    boundary.  If intensity=1.0 causes no drop, lock that worker at 1.0 and
    binary-search the next one.

    The result is (n_full, partial_intensity): n_full workers locked at 1.0,
    plus one additional worker at partial_intensity.  For example, (2, 0.375)
    means 2 full-speed slack workers and a third at 37.5% intensity can run
    alongside the baseline without interference.
    """
    print(f"\n[slack-{slack_resource}]  baseline_procs={baseline_procs}"
          f"  baseline_tput={baseline_tput:.1f}")

    # Pure CPU → io_mix=0 ;  Pure I/O → io_mix=1
    slack_io_mix = 0.0 if slack_resource == "cpu" else 1.0

    data_points: list[dict] = []

    probe_index = 0

    def run_batch(
        slack_intensities: list[float],
        ref_tput: float | None = None,
    ) -> tuple[bool, float, float, float, float]:
        """
        Spawn N baseline workers and one slack worker per entry in
        slack_intensities, all concurrently.  Returns:
            (dropped, obs_cpu_tput, obs_io_tput, obs_total_tput, slack_tput)

        All Popen() calls happen before any communicate(), so every worker
        runs at the same time and competes for the same resources.

        Drop detection uses the throughput dimension that matches slack_resource
        (cpu_throughput for CPU slack, io_throughput for I/O slack), so we only
        flag genuine contention on the resource under test.

        ref_tput: live reference from calibrate().  Defaults to the saturation
                  baseline_tput if not provided.
        """
        nonlocal probe_index

        def make_cmd(io_mix: float, intensity: float, seed: int) -> list[str]:
            return [
                str(WORKER_BIN),
                "--io-mix",    str(io_mix),
                "--intensity", str(intensity),
                "--duration",  str(duration),
                "--tmp-dir",   tmp_dir,
                "--seed",      str(seed),
            ]

        base_seed_offset  = 100_000 + probe_index * 1000
        slack_seed_offset = 200_000 + probe_index * 1000
        probe_index += 1

        # Launch all workers before waiting on any of them.
        base_procs_list = [
            subprocess.Popen(make_cmd(baseline_io_mix, baseline_intensity,
                                      base_seed + base_seed_offset + i),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            for i in range(baseline_procs)
        ]
        slack_procs_list = [
            subprocess.Popen(make_cmd(slack_io_mix, intensity,
                                      base_seed + slack_seed_offset + i),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            for i, intensity in enumerate(slack_intensities)
        ]

        # Collect — workers are already running concurrently above.
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

        obs_cpu_tput   = total_cpu_throughput(base_results)
        obs_io_tput    = total_io_throughput(base_results)
        obs_total_tput = total_throughput(base_results)
        # Resource-matched throughput for primary drop detection.
        obs_resource   = tput_for_resource(base_results, slack_resource)
        # When the baseline has zero resource-specific throughput (e.g. a
        # cpu-only baseline has io_throughput=0), fall back to total.
        obs_tput       = obs_resource if obs_resource > 0 else obs_total_tput
        slack_tput     = total_throughput(slack_results)
        _ref           = ref_tput if ref_tput is not None else baseline_tput
        # DROP if EITHER the resource-matched OR the total baseline throughput
        # drops by drop_pct.  Covers pure-CPU and pure-I/O baselines without
        # a special-case branch.
        drop_resource = (_ref - obs_tput)       / max(_ref, 1.0) >= drop_pct
        drop_total    = (_ref - obs_total_tput) / max(_ref, 1.0) >= drop_pct
        dropped = drop_resource or drop_total
        return dropped, obs_cpu_tput, obs_io_tput, obs_total_tput, slack_tput

    def calibrate(n_full: int) -> float:
        """
        Measure the live baseline throughput with all slack workers sleeping
        (n_full locked workers + 1 new worker, all at intensity=0).  This
        accounts for throughput drift and OS scheduling overhead of the extra
        processes.  All subsequent probes this round compare against this
        reference so only genuine resource contention triggers a drop.
        """
        total_sleeping = n_full + 1
        print(f"[slack-{slack_resource}]  calibrating with {total_sleeping} sleeping proc(s)...",
              end=" ", flush=True)
        _, cpu_t, io_t, total_t, _ = run_batch([0.0] * total_sleeping)
        obs_resource = cpu_t if slack_resource == "cpu" else io_t
        tput = obs_resource if obs_resource > 0 else total_t
        print(f"current baseline_{slack_resource}_tput = {tput:.1f} ops/s")
        return tput

    n_full:            int   = 0
    partial_intensity: float = 0.0
    lo_slack_tput:     float = 0.0
    found:             bool  = False
    MAX_SLACK_PROCS          = 16

    while n_full < MAX_SLACK_PROCS:
        print(f"[slack-{slack_resource}]  binary-searching worker #{n_full + 1}"
              f"  ({n_full} locked at 1.0)")

        # Calibrate: fresh reference with all slack workers sleeping this round.
        ref = calibrate(n_full)

        lo        = 0.0
        hi        = 1.0
        drop_seen = False

        # ~6 bisections → precision ≈ 0.015
        for step in range(6):
            mid = (lo + hi) / 2.0
            # n_full workers locked at 1.0, one new worker at mid.
            dropped, obs_cpu, obs_io, obs_total, slack_obs = run_batch(
                [1.0] * n_full + [mid], ref_tput=ref
            )
            tag = "DROP" if dropped else "ok"
            print(f"[slack-{slack_resource}]    n_full={n_full}  partial={mid:.3f}"
                  f"  baseline_cpu={obs_cpu:.1f}  baseline_io={obs_io:.1f}"
                  f"  slack_tput={slack_obs:.1f}  [{tag}]")
            data_points.append({
                "n_full":            n_full,
                "partial_intensity": mid,
                "baseline_cpu_tput": obs_cpu,
                "baseline_io_tput":  obs_io,
                "baseline_tput":     obs_total,
                "slack_tput":        slack_obs,
                "ref_tput":          ref,
                "dropped":           dropped,
            })
            if dropped:
                hi        = mid
                drop_seen = True
            else:
                lo            = mid
                lo_slack_tput = slack_obs

        if drop_seen:
            # lo is the highest intensity that caused no drop.
            partial_intensity = lo
            found = True
            break

        # Bisection found no drop — check intensity=1.0 explicitly before
        # deciding to lock this worker and move on.
        dropped_max, obs_cpu_max, obs_io_max, obs_total_max, slack_obs_max = run_batch(
            [1.0] * n_full + [1.0], ref_tput=ref
        )
        data_points.append({
            "n_full":            n_full,
            "partial_intensity": 1.0,
            "baseline_cpu_tput": obs_cpu_max,
            "baseline_io_tput":  obs_io_max,
            "baseline_tput":     obs_total_max,
            "slack_tput":        slack_obs_max,
            "ref_tput":          ref,
            "dropped":           dropped_max,
        })
        if dropped_max:
            # 1.0 drops but bisection didn't catch it — lo is the max-ok point.
            partial_intensity = lo
            found = True
            break

        # intensity=1.0 is safe: lock this worker and search the next one.
        lo_slack_tput = slack_obs_max
        print(f"[slack-{slack_resource}]  worker #{n_full + 1} locked at 1.0;"
              f" searching worker #{n_full + 2}")
        n_full += 1

    if not found:
        print(f"[slack-{slack_resource}]  WARNING: slack boundary not found"
              f" within {MAX_SLACK_PROCS} processes")

    print(f"[slack-{slack_resource}]  result = ({n_full} full, {partial_intensity:.3f} partial)")

    return {
        "type":              "slack",
        "resource":          slack_resource,
        "slack_measurement": {
            "n_full":            n_full,
            "partial_intensity": partial_intensity,
            "slack_tput":        lo_slack_tput,
        },
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

    # ------------------------------------------------------------------
    # Append one summary row to a CSV for later plotting.
    # Columns: io_mix, intensity, saturation_n,
    #          cpu_slack, cpu_slack_pct, io_slack, io_slack_pct
    #
    # *_slack      – max-ok intensity for that resource
    # *_slack_pct  – slack workers' throughput at that point divided by
    #                the saturation peak throughput (slack_xput / peak_xput)
    # ------------------------------------------------------------------
    csv_path = out_path.with_suffix(".csv")
    write_header = not csv_path.exists()

    cpu_result = next((r for r in all_results if r.get("resource") == "cpu"), None)
    io_result  = next((r for r in all_results if r.get("resource") == "io"),  None)
    peak_tput  = sat["peak_throughput"] if sat else 0.0
    sat_n      = sat["saturation_procs"] if sat else 0

    def _slack_row(result: Optional[dict]) -> float:
        """Return slack_tput / peak_tput for a slack result dict."""
        if result is None:
            return float("nan")
        m = result["slack_measurement"]
        return m["slack_tput"] / peak_tput if peak_tput > 0 else float("nan")

    cpu_slack_pct = _slack_row(cpu_result)
    io_slack_pct  = _slack_row(io_result)

    with open(csv_path, "a") as f:
        if write_header:
            f.write("io_mix,intensity,saturation_n,"
                    "cpu_slack_pct,io_slack_pct\n")
        f.write(f"{args.io_mix},{args.intensity},{sat_n},"
                f"{cpu_slack_pct:.4f},{io_slack_pct:.4f}\n")

    print(f"[done] CSV     → {csv_path}")


if __name__ == "__main__":
    main()
