#!/usr/bin/env python3
import argparse
import subprocess
import json
import os
import sys
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent.resolve()
WORKER_BIN = str(REPO_ROOT / "build" / "worker")

def run_workers(num_full_workers, fractional_intensity=0.0, *,
                resource_type, duration, warmup, tmp_dir, worker_bin, io_mode="rand_write"):
    """Spawns N full workers + 1 optional fractional worker and returns total resource throughput."""
    msg = f"Running {num_full_workers} worker(s)"
    if fractional_intensity > 0:
        msg += f" + 1 fractional ({fractional_intensity:.2f})"
    print(f"{msg}... ", end="", flush=True)

    io_mix = 1.0 if resource_type == "io" else 0.0
    mem_mix = 1.0 if resource_type == "ram" else 0.0

    def make_cmd(intensity, seed):
        return [worker_bin, 
                "--io-mix", str(io_mix),
                "--mem-mix", str(mem_mix),
                "--intensity", str(intensity),
                "--duration",  str(duration),
                "--warmup",    str(warmup),
                "--tmp-dir",   tmp_dir,
                "--seed",      str(seed),
                "--io-mode",   io_mode]

    processes = []
    for i in range(num_full_workers):
        p = subprocess.Popen(make_cmd(1.0, 1337 + i),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(p)

    if fractional_intensity > 0:
        p = subprocess.Popen(make_cmd(fractional_intensity, 1337 + num_full_workers),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(p)

    total_throughput = 0.0
    for p in processes:
        stdout, stderr = p.communicate()
        if p.returncode != 0:
            print(f"\nWorker failed!\nSTDERR: {stderr}")
            sys.exit(1)
        try:
            data = json.loads(stdout.strip())
            if resource_type == "cpu":
                total_throughput += data.get("cpu_throughput", 0.0)
            elif resource_type == "ram":
                total_throughput += data.get("mem_throughput", 0.0)
            else:
                total_throughput += data.get("io_throughput", 0.0)
        except json.JSONDecodeError:
            print(f"\nFailed to parse worker output: {stdout}")
            sys.exit(1)

    print(f"{total_throughput:,.0f} ops/s")
    return total_throughput


def calibrate(*, resource_type, duration, warmup, tmp_dir, worker_bin,
              io_mode="rand_write", step=1, start_n=None):
    """Run the full capacity calibration and return a result dict.

    start_n  – skip straight to this concurrency level; useful when you already
               know the device saturates well above N=1.  If None, starts at
               the first step boundary (i.e. step).  start_n need not be a
               multiple of step — the sweep then increments by step from there.
    """
    os.makedirs(tmp_dir, exist_ok=True)

    kw = dict(resource_type=resource_type, duration=duration, warmup=warmup, tmp_dir=tmp_dir, worker_bin=worker_bin, io_mode=io_mode)

    history = []

    # Phase 1: linear sweep — stop only when throughput has clearly declined,
    # not merely stopped growing.  A flat-top saturation curve (common with
    # O_DIRECT writes without fsync) can plateau for many concurrency levels
    # before the overhead of context-switching and cache eviction causes an
    # actual drop.  Requiring three consecutive *declines* (not just
    # sub-threshold gains) prevents stopping prematurely on a still-climbing
    # curve.
    first_n = start_n if start_n is not None else step
    n = max(1, first_n)  # clamp to at least 1
    if start_n is not None:
        print(f"  Starting sweep at n={n} (--start-n supplied; skipping 1..{n-1})")
    running_max = 0.0
    steps_since_improvement = 0
    MAX_STAGNATION = 5   # stop after this many steps with no new peak

    while True:
        throughput = run_workers(n, 0.0, **kw)
        history.append((n, 0.0, throughput))

        if throughput > running_max:
            running_max = throughput
            steps_since_improvement = 0
        else:
            steps_since_improvement += 1

        if steps_since_improvement >= MAX_STAGNATION:
            print("\nThroughput stagnated. Stopping integer sweep.")
            break
        if n >= 128:
            print("\nReached 128 processes. Stopping integer sweep.")
            break
        n += step

    # Find the n that gave the absolute peak during phase 1
    best_p1 = max(history, key=lambda x: x[2])
    best_n = best_p1[0]

    # Phase 2: Fixed grid search on fractional worker.
    # Probe on both sides of best_n to catch cases where the true peak sits
    # between (best_n-1)+frac and best_n+frac.
    print(f"\n--- Phase 2: Fractional Worker Grid Search ---")
    base_candidates = [c for c in [best_n - 1, best_n] if c >= 1]
    for base_n in base_candidates:
        print(f"Searching for hidden capacity with {base_n} full + fractional worker")
        for frac in [0.25, 0.50, 0.75]:
            t = run_workers(base_n, frac, **kw)
            history.append((base_n, frac, t))

    # The true capacity is simply the absolute maximum throughput observed anywhere
    absolute_best = max(history, key=lambda x: x[2])
    
    return dict(
        resource           = resource_type,
        io_mode            = io_mode,
        peak_throughput    = absolute_best[2],
        optimal_workers    = absolute_best[0],
    )


def main():
    parser = argparse.ArgumentParser(description="Calibrate maximum resource capacity.")
    parser.add_argument("--resource-type", choices=["cpu", "io", "ram"], required=True,
                        help="The resource type to calibrate.")
    parser.add_argument("--duration",   type=int,   default=60,
                        metavar="S",   help="seconds per worker probe (default: 60)")
    parser.add_argument("--warmup",     type=int,   default=5,
                        metavar="S",   help="warmup duration in seconds (default: 5)")
    default_tmp = "/holly/slack-meter-calibrate" if os.path.isdir("/holly") and os.access("/holly", os.W_OK) else "/tmp/slack-meter-calibrate"
    parser.add_argument("--tmp-dir",    default=default_tmp,
                        metavar="DIR", help="scratch dir for ops")
    parser.add_argument("--worker-bin", default=WORKER_BIN,
                        metavar="PATH",help="path to the worker binary")
    parser.add_argument("--io-mode",    default="rand_write",
                        help="IO Mode: rand_write | rand_read | rand_read_64k | seq_read")
    parser.add_argument("--step",       type=int, default=1, metavar="N",
                        help="concurrency step size for Phase 1 sweep (default: 1; use 4 for read modes)")
    parser.add_argument("--start-n",    type=int, default=None, metavar="N",
                        help="skip straight to this concurrency level; useful when saturating near a known point")
    parser.add_argument("--output",     default=None,
                        metavar="FILE",help="write JSON result to this file")
    args = parser.parse_args()

    if not os.path.exists(args.worker_bin):
        print(f"Error: worker binary not found at {args.worker_bin}")
        print("Please build the project first.")
        sys.exit(1)

    print("==================================================")
    print(f" Calibrating Maximum Capacity for: {args.resource_type.upper()} ")
    print("==================================================")
    print(f"Configuration: {args.duration}s duration")
    print(f"Tmp dir: {args.tmp_dir}")
    print("--------------------------------------------------")

    result = calibrate(resource_type=args.resource_type, duration=args.duration, warmup=args.warmup,
                       tmp_dir=args.tmp_dir, worker_bin=args.worker_bin, io_mode=args.io_mode,
                       step=args.step, start_n=args.start_n)

    print("==================================================")
    print(" Calibration Complete ")
    print("==================================================")
    print(f"Peak {args.resource_type.upper()} Throughput: {result['peak_throughput']:,.0f} tokens/s")
    print(f"Achieved at concurrency:    {result['optimal_workers']} worker(s)")
    k = result['peak_throughput'] / 1000.0
    print(f"System Capacity:            {k:,.2f} kTokens/s")
    print("==================================================")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Result written to {args.output}")


if __name__ == "__main__":
    main()
