#!/usr/bin/env python3
"""
saturate.py
===========
Sweeps worker concurrency to find the peak throughput (saturation point) of a
resource or mixed workload.

Two modes
---------
Pure resource mode (--resource-type cpu|io|ram)
    Workers are 100% of one resource type.  Throughput is the resource-specific
    metric (cpu_throughput, io_throughput, or mem_throughput).  Use this when
    calibrating the raw capacity of a single resource.

Mixed workload mode (--io-mix / --mem-mix / --intensity, no --resource-type)
    Workers use the specified blend of CPU, I/O, and RAM.  Throughput is the
    combined "throughput" field.  Use this when finding the saturation point of
    a realistic mixed workload before a probe sweep.

Algorithm
---------
Phase 1  Linear sweep: increment worker count by --step until throughput
         stagnates for MAX_STAGNATION consecutive steps.  Stagnation means the
         current measurement does not improve the running maximum.

Output JSON always contains:
    optimal_workers   – worker count at peak throughput
    peak_throughput   – peak ops/s
    resource          – "mixed" or the --resource-type value
    io_mode           – io mode used (pure resource mode only)
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


# ---------------------------------------------------------------------------
# Worker runner
# ---------------------------------------------------------------------------

def run_workers(
    num_full_workers: int,
    *,
    io_mix: float,
    mem_mix: float,
    intensity: float,
    duration: int,
    warmup: int,
    tmp_dir: str,
    worker_bin: str,
    io_mode: str = "rand_write",
    queue_depth: int = 1,
    cpu_mode: str = "cpu_int",
    tput_key: str = "throughput",
) -> float:
    """Spawn *num_full_workers* workers in parallel; return total throughput (ops/s)."""
    msg = f"Running {num_full_workers} worker(s)"
    print(f"{msg}... ", end="", flush=True)

    def make_cmd(seed: int) -> list[str]:
        return [
            worker_bin,
            "--io-mix",      str(io_mix),
            "--mem-mix",     str(mem_mix),
            "--intensity",   str(intensity),
            "--duration",    str(duration),
            "--warmup",      str(warmup),
            "--tmp-dir",     tmp_dir,
            "--seed",        str(seed),
            "--io-mode",     io_mode,
            "--queue-depth", str(queue_depth),
            "--cpu-mode",    cpu_mode,
        ]

    processes = [
        subprocess.Popen(make_cmd(1337 + i),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(num_full_workers)
    ]

    total = 0.0
    for p in processes:
        stdout, stderr = p.communicate()
        if stderr and stderr.strip():
            print(f"\n[worker warning]: {stderr.strip()}", file=sys.stderr)
        if p.returncode != 0:
            print(f"\nWorker failed (exit code {p.returncode})")
            sys.exit(1)
        try:
            data = json.loads(stdout.strip())
            total += data.get(tput_key, 0.0)
        except json.JSONDecodeError:
            print(f"\nFailed to parse worker output: {stdout}")
            sys.exit(1)

    print(f"{total:,.0f} ops/s")
    return total


# ---------------------------------------------------------------------------
# Core saturate
# ---------------------------------------------------------------------------

def saturate(
    *,
    io_mix: float,
    mem_mix: float,
    intensity: float,
    tput_key: str,
    duration: int,
    warmup: int,
    tmp_dir: str,
    worker_bin: str,
    io_mode: str = "rand_write",
    queue_depth: int = 1,
    cpu_mode: str = "cpu_int",
    step: int = 1,
    start_n: int | None = None,
) -> dict:
    """
    Sweep worker concurrency until throughput stagnates.

    start_n  – skip straight to this concurrency level; useful when you already
               know the device saturates well above N=1.  The sweep then
               increments by *step* from there.
    """
    os.makedirs(tmp_dir, exist_ok=True)

    kw = dict(
        io_mix=io_mix, mem_mix=mem_mix, intensity=intensity,
        duration=duration, warmup=warmup, tmp_dir=tmp_dir,
        worker_bin=worker_bin, io_mode=io_mode, queue_depth=queue_depth,
        cpu_mode=cpu_mode, tput_key=tput_key,
    )

    MAX_STAGNATION = 5

    first_n = start_n if start_n is not None else step
    n = max(1, first_n)
    if start_n is not None:
        print(f"  Starting sweep at n={n} (--start-n supplied; skipping 1..{n-1})")

    history: list[tuple[int, float]] = []
    running_max = 0.0
    peak_n = n                 # worker count at which running_max was last set
    steps_since_improvement = 0

    while True:
        tput = run_workers(n, **kw)
        history.append((n, tput))

        # Improvement threshold: 2% of the per-worker contribution at the current peak.
        # Scales with concurrency — strict early (one worker matters a lot) and lenient
        # late (marginal contribution of one more worker is small), so noise near a
        # flat plateau doesn't keep resetting the counter indefinitely.
        min_gain = (running_max / peak_n * 0.02) if running_max > 0 else 0.0
        if tput > running_max + min_gain:
            running_max = tput
            peak_n = n
            steps_since_improvement = 0
        else:
            steps_since_improvement += 1

        if steps_since_improvement >= MAX_STAGNATION:
            print("\nThroughput stagnated. Stopping sweep.")
            break
        if n >= 1024:
            print("\nReached 1024 processes. Stopping sweep.")
            break
        n += step

    best_n, best_tput = max(history, key=lambda x: x[1])

    return dict(
        resource        = "mixed" if tput_key == "throughput" else tput_key.replace("_throughput", ""),
        io_mode         = io_mode,
        peak_throughput = best_tput,
        optimal_workers = best_n,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep worker concurrency to find the saturation (peak throughput) point.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Pure resource mode (single resource, e.g. to calibrate raw IO capacity):
  saturate.py --resource-type io --io-mode rand_write

Mixed workload mode (realistic blend, e.g. before a probe sweep):
  saturate.py --io-mix 0.3 --intensity 0.75
""",
    )

    # Mode selection
    mode_group = parser.add_argument_group("workload mode (pick one)")
    mode_group.add_argument(
        "--resource-type", choices=["cpu", "io", "ram"],
        help="Pure resource type (derives io-mix/mem-mix automatically).",
    )
    mode_group.add_argument("--io-mix",    type=float, default=None, metavar="F",
                            help="Fraction of work that is I/O (mixed mode).")
    mode_group.add_argument("--mem-mix",   type=float, default=None, metavar="F",
                            help="Fraction of work that is RAM (mixed mode).")
    mode_group.add_argument("--intensity", type=float, default=None, metavar="F",
                            help="Worker intensity in mixed mode (default 0.75).")

    # Common sweep params
    parser.add_argument("--duration",    type=int,   default=60,            metavar="S")
    parser.add_argument("--warmup",      type=int,   default=5,             metavar="S")
    parser.add_argument("--step",        type=int,   default=1,             metavar="N",
                        help="Concurrency step size (default: 1; try 4 for read-heavy IO modes).")
    parser.add_argument("--start-n",     type=int,   default=None,          metavar="N",
                        help="Skip straight to this concurrency level.")
    parser.add_argument("--queue-depth", type=int,   default=1,             metavar="QD")
    parser.add_argument("--io-mode",     default="rand_write",
                        help="IO mode: rand_write | rand_read | rand_read_64k | seq_read")
    parser.add_argument("--cpu-mode",    default="cpu_int",
                        help="CPU mode: cpu_int | cpu_fp | cpu_hash")

    # Infra
    default_tmp = (
        "/holly/slack-meter-saturate"
        if os.path.isdir("/holly") and os.access("/holly", os.W_OK)
        else "/tmp/slack-meter-saturate"
    )
    parser.add_argument("--tmp-dir",    default=default_tmp,  metavar="DIR")
    parser.add_argument("--worker-bin", default=WORKER_BIN,   metavar="PATH")
    parser.add_argument("--output",     default=None,          metavar="FILE")

    args = parser.parse_args()

    if not os.path.exists(args.worker_bin):
        print(f"Error: worker binary not found at {args.worker_bin}")
        print("Please build the project first.")
        sys.exit(1)

    # Resolve workload params
    if args.resource_type:
        # Pure resource mode
        io_mix   = 1.0 if args.resource_type == "io"  else 0.0
        mem_mix  = 1.0 if args.resource_type == "ram" else 0.0
        intensity = 1.0
        tput_key = {
            "cpu": "cpu_throughput",
            "io":  "io_throughput",
            "ram": "mem_throughput",
        }[args.resource_type]
        if args.io_mix is not None or args.mem_mix is not None or args.intensity is not None:
            print("[saturate] WARNING: --resource-type overrides --io-mix/--mem-mix/--intensity.",
                  file=sys.stderr)
    elif args.io_mix is not None or args.mem_mix is not None or args.intensity is not None:
        # Mixed workload mode
        io_mix    = args.io_mix    if args.io_mix    is not None else 0.0
        mem_mix   = args.mem_mix   if args.mem_mix   is not None else 0.0
        intensity = args.intensity if args.intensity is not None else 0.75
        tput_key  = "throughput"
    else:
        parser.error(
            "Specify either --resource-type (pure resource mode) "
            "or at least one of --io-mix/--mem-mix/--intensity (mixed workload mode)."
        )

    print("=" * 60)
    if args.resource_type:
        print(f"  Saturate — pure {args.resource_type.upper()} resource")
    else:
        print(f"  Saturate — mixed workload  io_mix={io_mix}  mem_mix={mem_mix}  intensity={intensity}")
    print("=" * 60)
    print(f"  Duration : {args.duration}s   step={args.step}   queue_depth={args.queue_depth}")
    print(f"  Tmp dir  : {args.tmp_dir}")
    print("=" * 60)

    result = saturate(
        io_mix      = io_mix,
        mem_mix     = mem_mix,
        intensity   = intensity,
        tput_key    = tput_key,
        duration    = args.duration,
        warmup      = args.warmup,
        tmp_dir     = args.tmp_dir,
        worker_bin  = args.worker_bin,
        io_mode     = args.io_mode,
        queue_depth = args.queue_depth,
        cpu_mode    = args.cpu_mode,
        step        = args.step,
        start_n     = args.start_n,
    )

    print("=" * 60)
    print("  Saturation Complete")
    print("=" * 60)
    print(f"  Peak throughput : {result['peak_throughput']:,.0f} ops/s"
          f"  ({result['peak_throughput']/1000:.2f} kTokens/s)")
    print(f"  Optimal workers : {result['optimal_workers']}")
    print("=" * 60)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Result written to {args.output}")


if __name__ == "__main__":
    main()
