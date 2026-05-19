#!/usr/bin/env python3
"""
sweep_io_loaded.py
==================
Sweeps pure I/O workers on top of a background workload, stopping when the
BACKGROUND throughput drops by DROP_PCT — not when I/O throughput plateaus.

This is the correct stopping condition for run_loaded_sweep.sh SWEEP=io:

  calibrate_io.py  → stops when I/O throughput stops growing
  sweep_io_loaded  → stops when background workers are being interfered with

Methodology
-----------
Phase 0  Baseline: run BG_PROCS background workers alone for DURATION seconds.
         Record their throughput as the reference (baseline_tput).

Phase 1  Linear sweep: add one full-intensity (io_mix=1, intensity=1) I/O
         worker per round.  Each round runs all background + all current I/O
         workers together for DURATION seconds.  Stop as soon as background
         throughput drops by >= DROP_PCT relative to baseline, or when the
         background recovers after a brief overshoot (see below), or when
         MAX_IO_PROCS is reached.

Phase 2  Binary search: with (n_full_io - 1) locked at intensity=1.0, search
         for the highest fractional intensity on the last I/O worker that still
         leaves background throughput undisturbed.

Result is reported as (n_full, partial_intensity): n_full I/O workers at
intensity=1.0 plus one more at partial_intensity can run without affecting
the background workload.

Usage
-----
    python3 scripts/sweep_io_loaded.py \\
        --bg-procs 8 --bg-io-mix 0.5 --bg-intensity 0.9 \\
        --duration 30 --drop-pct 0.05 \\
        --tmp-dir /tmp/slack-meter \\
        --output results/loaded_sweep/sweep_io.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent.resolve()
WORKER_BIN = str(REPO_ROOT / "build" / "worker")

# Seeds: background workers get 1000+i, I/O sweep workers get 2000+i
_BG_SEED_BASE = 1000
_IO_SEED_BASE = 2000


# ---------------------------------------------------------------------------
# Core probe: run background + I/O workers concurrently, return throughputs
# ---------------------------------------------------------------------------

def run_probe(
    bg_procs:     int,
    bg_io_mix:    float,
    bg_intensity: float,
    n_io:         int,
    io_intensity: float,
    duration:     int,
    tmp_dir:      str,
    worker_bin:   str,
) -> tuple[float, float]:
    """
    Spawn bg_procs background workers and n_io pure-I/O workers simultaneously.
    Wait for all, then return (bg_throughput, io_throughput).
    """
    def make_cmd(io_mix: float, intensity: float, seed: int) -> list[str]:
        return [
            worker_bin,
            "--io-mix",    str(io_mix),
            "--intensity", str(intensity),
            "--duration",  str(duration),
            "--tmp-dir",   tmp_dir,
            "--seed",      str(seed),
        ]

    procs: list[subprocess.Popen] = []

    for i in range(bg_procs):
        procs.append(subprocess.Popen(
            make_cmd(bg_io_mix, bg_intensity, _BG_SEED_BASE + i),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ))
    for i in range(n_io):
        procs.append(subprocess.Popen(
            make_cmd(1.0, io_intensity, _IO_SEED_BASE + i),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ))

    bg_tput = 0.0
    io_tput = 0.0

    for idx, p in enumerate(procs):
        stdout, _ = p.communicate()
        if p.returncode != 0:
            print(f"\n[sweep-io] Worker {idx} exited non-zero — skipping sample")
            continue
        try:
            data = json.loads(stdout.strip())
            if idx < bg_procs:
                bg_tput += data.get("throughput", 0.0)
            else:
                io_tput += data.get("io_throughput", 0.0)
        except (json.JSONDecodeError, KeyError):
            pass

    return bg_tput, io_tput


# ---------------------------------------------------------------------------
# Main sweep logic
# ---------------------------------------------------------------------------

def sweep(
    bg_procs:     int,
    bg_io_mix:    float,
    bg_intensity: float,
    duration:     int,
    tmp_dir:      str,
    worker_bin:   str,
    drop_pct:     float = 0.05,
    max_io_procs: int   = 64,
    binary_steps: int   = 5,
) -> dict:
    os.makedirs(tmp_dir, exist_ok=True)

    kw = dict(bg_procs=bg_procs, bg_io_mix=bg_io_mix, bg_intensity=bg_intensity,
              duration=duration, tmp_dir=tmp_dir, worker_bin=worker_bin)

    # ------------------------------------------------------------------
    # Phase 0: baseline (no I/O sweep workers)
    # ------------------------------------------------------------------
    print("--- Phase 0: Baseline (background workers only) ---")
    baseline_tput, _ = run_probe(n_io=0, io_intensity=0.0, **kw)
    threshold = baseline_tput * (1.0 - drop_pct)
    print(f"  Baseline bg throughput : {baseline_tput:,.0f} ops/s")
    print(f"  Interference threshold : {threshold:,.0f} ops/s  (drop >= {drop_pct*100:.0f}%)")

    # ------------------------------------------------------------------
    # Phase 1: linear sweep of full-intensity I/O workers
    # ------------------------------------------------------------------
    print("\n--- Phase 1: Linear I/O sweep (stopping on background interference) ---")
    n_full = 0   # number of full-intensity I/O workers that are safe

    for n_io in range(1, max_io_procs + 1):
        bg_tput, io_tput = run_probe(n_io=n_io, io_intensity=1.0, **kw)
        interfered = bg_tput < threshold
        flag = "  <-- INTERFERENCE" if interfered else ""
        print(f"  IO workers={n_io:3d}  bg={bg_tput:>10,.0f} ops/s  io={io_tput:>10,.0f} ops/s{flag}")

        if interfered:
            # n_io workers is too many; n_io-1 were safe
            n_full = n_io - 1
            break
        n_full = n_io   # still safe, keep going
    else:
        print(f"\n  Reached max_io_procs={max_io_procs} without interference.")

    # ------------------------------------------------------------------
    # Phase 2: binary search on fractional last worker
    # ------------------------------------------------------------------
    print(f"\n--- Phase 2: Binary search on fractional I/O worker "
          f"(locked: {n_full} × intensity=1.0) ---")

    low, high = 0.0, 1.0
    best_intensity = 0.0

    for step in range(binary_steps):
        mid = (low + high) / 2.0
        bg_tput, io_tput = run_probe(n_io=n_full + 1, io_intensity=mid, **kw)
        interfered = bg_tput < threshold
        flag = "interferes" if interfered else "ok"
        print(f"  step {step+1}/{binary_steps}  partial_intensity={mid:.3f}  "
              f"bg={bg_tput:,.0f}  [{flag}]")

        if not interfered:
            best_intensity = mid
            low = mid        # can push further
        else:
            high = mid       # too much, back off

    print(f"\n  I/O slack: {n_full} full worker(s) + 1 at intensity {best_intensity:.3f}")
    if n_full == 0 and best_intensity == 0.0:
        print("  (background is saturated — even a single low-intensity I/O worker interferes)")

    return dict(
        type              = "sweep_io_loaded",
        baseline_tput     = baseline_tput,
        interference_threshold = threshold,
        drop_pct          = drop_pct,
        bg_procs          = bg_procs,
        bg_io_mix         = bg_io_mix,
        bg_intensity      = bg_intensity,
        io_slack_full     = n_full,
        io_slack_partial  = best_intensity,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep I/O workers under background load; stop on background interference.")
    parser.add_argument("--bg-procs",     type=int,   required=True, metavar="N",
                        help="number of background workers")
    parser.add_argument("--bg-io-mix",    type=float, default=0.3,   metavar="F",
                        help="background io_mix (default: 0.3)")
    parser.add_argument("--bg-intensity", type=float, default=0.75,  metavar="F",
                        help="background intensity (default: 0.75)")
    parser.add_argument("--duration",     type=int,   default=30,    metavar="S",
                        help="seconds per probe (default: 30)")
    parser.add_argument("--drop-pct",     type=float, default=0.05,  metavar="F",
                        help="background throughput drop fraction to count as interference (default: 0.05)")
    parser.add_argument("--max-io-procs", type=int,   default=64,    metavar="N",
                        help="max I/O sweep workers before giving up (default: 64)")
    parser.add_argument("--tmp-dir",      default="/tmp/slack-meter", metavar="DIR",
                        help="scratch dir for I/O ops")
    parser.add_argument("--worker-bin",   default=WORKER_BIN,        metavar="PATH",
                        help="path to the worker binary")
    parser.add_argument("--output",       default=None,              metavar="FILE",
                        help="write JSON result to this file")
    args = parser.parse_args()

    if not os.path.exists(args.worker_bin):
        print(f"[sweep-io] ERROR: worker binary not found at {args.worker_bin}")
        print("  Run: cmake -B build && cmake --build build")
        sys.exit(1)

    print("=" * 54)
    print("  I/O Sweep Under Background Load")
    print("=" * 54)
    print(f"  Background : {args.bg_procs} workers  "
          f"io_mix={args.bg_io_mix}  intensity={args.bg_intensity}")
    print(f"  Probe dur  : {args.duration}s   drop_pct={args.drop_pct*100:.0f}%")
    print(f"  Tmp dir    : {args.tmp_dir}")
    print("=" * 54)

    result = sweep(
        bg_procs     = args.bg_procs,
        bg_io_mix    = args.bg_io_mix,
        bg_intensity = args.bg_intensity,
        duration     = args.duration,
        tmp_dir      = args.tmp_dir,
        worker_bin   = args.worker_bin,
        drop_pct     = args.drop_pct,
        max_io_procs = args.max_io_procs,
    )

    print("\n" + "=" * 54)
    print("  Result")
    print("=" * 54)
    print(f"  Baseline bg throughput : {result['baseline_tput']:,.0f} ops/s")
    print(f"  I/O slack              : {result['io_slack_full']} full worker(s) "
          f"+ 1 at intensity {result['io_slack_partial']:.3f}")
    print("=" * 54)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nResult written to {args.output}")


if __name__ == "__main__":
    main()
