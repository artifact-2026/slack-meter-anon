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
                duration, tmp_dir, worker_bin):
    """Spawns N full workers + 1 optional fractional worker and returns total I/O throughput."""
    msg = f"Running {num_full_workers} worker(s)"
    if fractional_intensity > 0:
        msg += f" + 1 fractional ({fractional_intensity:.2f})"
    print(f"{msg}... ", end="", flush=True)

    def make_cmd(intensity, seed):
        return [worker_bin, "--io-mix", "1.0",
                "--intensity", str(intensity),
                "--duration",  str(duration),
                "--tmp-dir",   tmp_dir,
                "--seed",      str(seed)]

    processes = []
    for i in range(num_full_workers):
        p = subprocess.Popen(make_cmd(1.0, 1337 + i),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(p)

    if fractional_intensity > 0:
        p = subprocess.Popen(make_cmd(fractional_intensity, 1337 + num_full_workers),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(p)

    total_io_throughput = 0.0
    for p in processes:
        stdout, stderr = p.communicate()
        if p.returncode != 0:
            print(f"\nWorker failed!\nSTDERR: {stderr}")
            sys.exit(1)
        try:
            data = json.loads(stdout.strip())
            total_io_throughput += data["io_throughput"]
        except json.JSONDecodeError:
            print(f"\nFailed to parse worker output: {stdout}")
            sys.exit(1)

    print(f"{total_io_throughput:,.0f} ops/s")
    return total_io_throughput


def calibrate(*, duration, tmp_dir, worker_bin):
    """Run the full I/O calibration and return a result dict."""
    os.makedirs(tmp_dir, exist_ok=True)

    kw = dict(duration=duration, tmp_dir=tmp_dir, worker_bin=worker_bin)

    peak_throughput = 0.0
    optimal_workers = 0
    plateau_strikes = 0

    # Phase 1: linear sweep
    n = 1
    while True:
        throughput = run_workers(n, **kw)

        if throughput > peak_throughput * 1.02:
            peak_throughput = throughput
            optimal_workers = n
            plateau_strikes = 0
        else:
            plateau_strikes += 1

        if plateau_strikes >= 3:
            print("\nThroughput has plateaued. Stopping integer sweep.")
            break
        if n >= 128:
            print("\nReached 128 processes. Stopping integer sweep.")
            break
        n += 1

    # Phase 2: binary search on fractional worker
    print(f"\n--- Phase 2: Binary Search on Fractional Worker ---")
    print(f"Searching for hidden capacity with {optimal_workers} full + 1 fractional worker")

    low, high = 0.0, 1.0
    best_throughput = peak_throughput
    best_intensity  = 0.0

    for _ in range(5):   # ~3 % precision
        mid = (low + high) / 2.0
        t = run_workers(optimal_workers, mid, **kw)
        if t > best_throughput:
            best_throughput = t
            best_intensity  = mid
            low = mid
        else:
            high = mid

    return dict(
        peak_io_throughput = best_throughput,
        optimal_workers    = optimal_workers,
        best_intensity     = best_intensity,
    )


def main():
    parser = argparse.ArgumentParser(description="Calibrate maximum I/O capacity (T_io).")
    parser.add_argument("--duration",   type=int,   default=30,
                        metavar="S",   help="seconds per worker probe (default: 30)")
    parser.add_argument("--tmp-dir",    default="/tmp/slack-meter-calibrate",
                        metavar="DIR", help="scratch dir for I/O ops")
    parser.add_argument("--worker-bin", default=WORKER_BIN,
                        metavar="PATH",help="path to the worker binary")
    parser.add_argument("--output",     default=None,
                        metavar="FILE",help="write JSON result to this file")
    args = parser.parse_args()

    if not os.path.exists(args.worker_bin):
        print(f"Error: worker binary not found at {args.worker_bin}")
        print("Please build the project first.")
        sys.exit(1)

    print("==================================================")
    print(" Calibrating Maximum I/O Capacity (T_io) ")
    print("==================================================")
    print(f"Configuration: pure I/O (4KiB sync random writes), {args.duration}s duration")
    print(f"Tmp dir: {args.tmp_dir}")
    print("--------------------------------------------------")

    result = calibrate(duration=args.duration, tmp_dir=args.tmp_dir,
                       worker_bin=args.worker_bin)

    print("==================================================")
    print(" Calibration Complete ")
    print("==================================================")
    print(f"Peak I/O Throughput (T_io): {result['peak_io_throughput']:,.0f} ops/s")
    print(f"Achieved at concurrency:    {result['optimal_workers']} full "
          f"+ 1 fractional ({result['best_intensity']:.2f})")
    k = result['peak_io_throughput'] / 1000.0
    print(f"System I/O Capacity:        {k:,.2f} kTokens")
    print("==================================================")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Result written to {args.output}")


if __name__ == "__main__":
    main()
