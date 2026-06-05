#!/usr/bin/env python3
"""
compare_slack_methods.py
========================
Exercises two methods of measuring slack across three resource classes (CPU, I/O, RAM):
1. Method 1: Absolute maximum throughput the probing processes could obtain,
   regardless of the background baseline's throughput changes.
2. Method 2: Maximum throughput of the probing processes while monitoring the
   background baseline's throughput not falling beyond the DROP_PCT.

Appends results to a CSV file in the following format:
resource_type, resource_type_mode, baseline_xput_in_method_1, unused_by_method_1, baseline_xput_in_method_2, unused_by_method_2
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

# Add scripts directory to path to import probe module
scripts_dir = Path(__file__).parent.resolve()
sys.path.append(str(scripts_dir))

try:
    from probe import run_probe, sweep
except ImportError as e:
    print(f"Error importing probe.py: {e}")
    sys.exit(1)


def clean_scratch_files(tmp_dir):
    tmp_path = Path(tmp_dir)
    if tmp_path.exists():
        print(f"\nCleaning up scratch files in {tmp_path}...")
        for p in tmp_path.glob("sm_io_*.dat"):
            try:
                p.unlink()
            except Exception as e:
                print(f"[Warning] Failed to delete {p}: {e}")


def get_bg_settings(resource_type, mode):
    if resource_type == "cpu":
        return {
            "bg_io_mix": 0.0,
            "bg_mem_mix": 0.0,
            "bg_cpu_mode": "cpu_int",
            "bg_mem_mode": "mem_copy",
            "bg_io_mode": "rand_write",
            "probe_cpu_mode": mode,
            "probe_mem_mode": "mem_copy",
            "probe_io_mode": "rand_write",
        }
    elif resource_type == "io":
        return {
            "bg_io_mix": 1.0,
            "bg_mem_mix": 0.0,
            "bg_cpu_mode": "cpu_int",
            "bg_mem_mode": "mem_copy",
            "bg_io_mode": "rand_write",
            "probe_cpu_mode": "cpu_int",
            "probe_mem_mode": "mem_copy",
            "probe_io_mode": mode,
        }
    elif resource_type == "ram":
        return {
            "bg_io_mix": 0.0,
            "bg_mem_mix": 1.0,
            "bg_cpu_mode": "cpu_int",
            "bg_mem_mode": "mem_copy",
            "bg_io_mode": "rand_write",
            "probe_cpu_mode": "cpu_int",
            "probe_mem_mode": mode,
            "probe_io_mode": "rand_write",
        }
    else:
        raise ValueError(f"Unknown resource type: {resource_type}")


def sweep_method_1(
    probe_type: str,
    bg_procs: int,
    bg_io_mix: float,
    bg_mem_mix: float,
    bg_intensity: float,
    duration: int,
    warmup: int,
    tmp_dir: str,
    worker_bin: str,
    bg_io_mode: str,
    probe_io_mode: str,
    samples: int,
    bg_queue_depth: int,
    probe_queue_depth: int,
    bg_cpu_mode: str,
    probe_cpu_mode: str,
    bg_mem_mode: str,
    probe_mem_mode: str,
    file_size_bytes: int,
    step: int = 1,
    max_probes: int = 64,
    start_n: int = 1,
) -> tuple[float, float]:
    """
    Method 1: absolute maximum throughput of probing processes, regardless of bg baseline changes.
    Sweeps n_probe_full until probe throughput plateaus/stagnates.
    Returns (bg_throughput_at_peak_probe, peak_probe_throughput) in Ops/s.
    """
    if probe_type == "io":
        probe_io_mix_val = 1.0
        probe_mem_mix_val = 0.0
        tput_key = "io_throughput"
    elif probe_type == "ram":
        probe_io_mix_val = 0.0
        probe_mem_mix_val = 1.0
        tput_key = "mem_throughput"
    else:  # cpu
        probe_io_mix_val = 0.0
        probe_mem_mix_val = 0.0
        tput_key = "cpu_throughput"

    kw = dict(
        bg_procs=bg_procs,
        bg_io_mix=bg_io_mix,
        bg_mem_mix=bg_mem_mix,
        bg_intensity=bg_intensity,
        probe_io_mix=probe_io_mix_val,
        probe_mem_mix=probe_mem_mix_val,
        duration=duration,
        warmup=warmup,
        tmp_dir=tmp_dir,
        worker_bin=worker_bin,
        tput_key=tput_key,
        bg_io_mode=bg_io_mode,
        probe_io_mode=probe_io_mode,
        samples=samples,
        bg_queue_depth=bg_queue_depth,
        probe_queue_depth=probe_queue_depth,
        bg_cpu_mode=bg_cpu_mode,
        probe_cpu_mode=probe_cpu_mode,
        bg_mem_mode=bg_mem_mode,
        probe_mem_mode=probe_mem_mode,
        file_size_bytes=file_size_bytes,
    )

    MAX_STAGNATION = 5
    n = start_n
    running_max_probe = 0.0
    peak_n = n
    steps_since_improvement = 0
    bg_at_peak_probe = 0.0

    print(f"\n--- Method 1: Saturating probe processes (regardless of background baseline drop) ---")
    print(f"  {'Probes':>7}  {'bg (T/s)':>12}  {probe_type.upper()+' (T/s)':>12}  {'status':}")
    print(f"  {'-------':>7}  {'---------':>12}  {'---------':>12}")

    while n <= max_probes:
        bg_tput, probe_tput = run_probe(n_probe_full=n, probe_frac=0.0, **kw)

        # Improvement threshold: 2% of the per-worker contribution at the current peak.
        min_gain = (running_max_probe / peak_n * 0.02) if running_max_probe > 0 else 0.0
        if probe_tput > running_max_probe + min_gain:
            running_max_probe = probe_tput
            bg_at_peak_probe = bg_tput
            peak_n = n
            steps_since_improvement = 0
            status = "NEW PEAK"
        else:
            steps_since_improvement += 1
            status = f"stagnant ({steps_since_improvement}/{MAX_STAGNATION})"

        print(f"  {n:>7d}  {bg_tput:>12.0f}  {probe_tput:>12.0f}  {status}")

        if steps_since_improvement >= MAX_STAGNATION:
            print(f"Throughput stagnated for {MAX_STAGNATION} steps. Stopping sweep.")
            break

        n += step

    return bg_at_peak_probe, running_max_probe


def main():
    parser = argparse.ArgumentParser(description="Compare Method 1 & 2 for measuring slack.")
    parser.add_argument("--duration", type=int, default=45, help="Duration per probe (secs)")
    parser.add_argument("--warmup", type=int, default=15, help="Warmup duration (secs)")
    parser.add_argument("--bg-procs", type=int, default=2, help="Number of background processes")
    parser.add_argument("--bg-intensity", type=float, default=1.0, help="Background load intensity")
    parser.add_argument("--drop-pct", type=float, default=0.10, help="Method 2 background drop threshold (e.g. 0.10 for 10%%)")
    parser.add_argument("--samples", type=int, default=1, help="Number of samples per level")
    parser.add_argument("--step", type=int, default=1, help="Sweep step size")
    parser.add_argument("--start-n", type=int, default=1, help="Sweep starting concurrency (default: 1)")
    parser.add_argument("--max-probes", type=int, default=300, help="Max probe processes")
    parser.add_argument("--queue-depth", type=int, default=1, help="Queue depth for io_uring")
    parser.add_argument("--file-size-mib", type=int, default=256, help="Scratch file size in MiB")
    parser.add_argument("--csv-out", default="results/slack_methods_comparison.csv", help="CSV output path")
    parser.add_argument("--skip-build", action="store_true", help="Skip cmake build step")
    parser.add_argument("--all-modes", action="store_true", help="Run all modes instead of one representative mode per resource type")
    parser.add_argument("--resource-type", choices=["cpu", "io", "ram"], default=None,
                        help="Filter to run only a specific resource type (cpu, io, or ram)")
    parser.add_argument("--method", choices=["both", "method1", "method2"], default="both",
                        help="Which slack measurement method to run (default: both)")
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent.resolve()
    worker_bin = str(repo_root / "build" / "worker")

    # Build check
    if not args.skip_build:
        print("Building slack-meter...")
        build_dir = repo_root / "build"
        try:
            subprocess.run(["cmake", "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release", "-S", str(repo_root)], check=True)
            subprocess.run(["cmake", "--build", str(build_dir), "--parallel"], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Build failed: {e}")
            sys.exit(1)

    if not os.path.exists(worker_bin):
        print(f"Error: worker binary not found at {worker_bin}")
        sys.exit(1)

    # Set up temp directory
    tmp_dir = "/holly/slack-meter-comparison" if os.path.isdir("/holly") and os.access("/holly", os.W_OK) else "/tmp/slack-meter-comparison"
    os.makedirs(tmp_dir, exist_ok=True)

    # Select configuration modes
    if args.all_modes:
        configs = [
            ("cpu", "cpu_int"),
            ("cpu", "cpu_fp"),
            ("cpu", "cpu_hash"),
            ("io", "rand_write"),
            ("io", "rand_read"),
            ("io", "seq_write"),
            ("io", "seq_read"),
            ("ram", "mem_copy"),
            ("ram", "mem_read"),
            ("ram", "mem_write"),
        ]
    else:
        # One representative mode per resource type
        configs = [
            ("cpu", "cpu_int"),
            ("io", "rand_write"),
            ("ram", "mem_copy"),
        ]

    if args.resource_type:
        configs = [c for c in configs if c[0] == args.resource_type]

    # Pre-flight clean
    clean_scratch_files(tmp_dir)

    results_to_append = []

    for resource_type, mode in configs:
        print("\n" + "=" * 80)
        print(f" Running Slack Methods Comparison: {resource_type.upper()} ({mode})")
        print("=" * 80)

        bg_settings = get_bg_settings(resource_type, mode)

        # Run Method 1
        bg_m1_kt = ""
        probe_m1_kt = ""
        if args.method in ["both", "method1"]:
            bg_m1_ops, probe_m1_ops = sweep_method_1(
                probe_type=resource_type,
                bg_procs=args.bg_procs,
                bg_io_mix=bg_settings["bg_io_mix"],
                bg_mem_mix=bg_settings["bg_mem_mix"],
                bg_intensity=args.bg_intensity,
                duration=args.duration,
                warmup=args.warmup,
                tmp_dir=tmp_dir,
                worker_bin=worker_bin,
                bg_io_mode=bg_settings["bg_io_mode"],
                probe_io_mode=bg_settings["probe_io_mode"],
                samples=args.samples,
                bg_queue_depth=args.queue_depth,
                probe_queue_depth=args.queue_depth,
                bg_cpu_mode=bg_settings["bg_cpu_mode"],
                probe_cpu_mode=bg_settings["probe_cpu_mode"],
                bg_mem_mode=bg_settings["bg_mem_mode"],
                probe_mem_mode=bg_settings["probe_mem_mode"],
                file_size_bytes=args.file_size_mib * 1024 * 1024,
                step=args.step,
                max_probes=args.max_probes,
                start_n=args.start_n,
            )
            # Convert Method 1 to kTokens/s
            bg_m1_kt = bg_m1_ops * 1e-3
            probe_m1_kt = probe_m1_ops * 1e-3

        # Run Method 2
        bg_m2_kt = ""
        probe_m2_kt = ""
        if args.method in ["both", "method2"]:
            print(f"\n--- Method 2: Sweep with drop_pct={args.drop_pct*100:.1f}% threshold ---")
            res_m2 = sweep(
                probe_type=resource_type,
                bg_procs=args.bg_procs,
                bg_io_mix=bg_settings["bg_io_mix"],
                bg_mem_mix=bg_settings["bg_mem_mix"],
                bg_intensity=args.bg_intensity,
                duration=args.duration,
                warmup=args.warmup,
                tmp_dir=tmp_dir,
                worker_bin=worker_bin,
                drop_pct=args.drop_pct,
                max_probes=args.max_probes,
                bg_io_mode=bg_settings["bg_io_mode"],
                probe_io_mode=bg_settings["probe_io_mode"],
                samples=args.samples,
                bg_queue_depth=args.queue_depth,
                probe_queue_depth=args.queue_depth,
                bg_cpu_mode=bg_settings["bg_cpu_mode"],
                probe_cpu_mode=bg_settings["probe_cpu_mode"],
                bg_mem_mode=bg_settings["bg_mem_mode"],
                probe_mem_mode=bg_settings["probe_mem_mode"],
                file_size_bytes=args.file_size_mib * 1024 * 1024,
                step=args.step,
                start_n=args.start_n,
            )
            bg_m2_kt = res_m2["baseline_r_ktokens"]
            probe_m2_kt = res_m2["slack_ktokens"]

        results_to_append.append({
            "resource_type": resource_type,
            "resource_type_mode": mode,
            "baseline_xput_in_method_1": bg_m1_kt,
            "unused_by_method_1": probe_m1_kt,
            "baseline_xput_in_method_2": bg_m2_kt,
            "unused_by_method_2": probe_m2_kt,
        })

        print(f"\nResults for {resource_type} ({mode}):")
        if isinstance(bg_m1_kt, float):
            print(f"  Method 1 (Absolute Peak Probe):")
            print(f"    Remaining Background Baseline: {bg_m1_kt:,.3f} kTokens/s")
            print(f"    Probe Slack / Unused Capacity: {probe_m1_kt:,.3f} kTokens/s")
        if isinstance(bg_m2_kt, float):
            print(f"  Method 2 (Drop Pct limit {args.drop_pct*100:.1f}%):")
            print(f"    Remaining Background Baseline: {bg_m2_kt:,.3f} kTokens/s")
            print(f"    Probe Slack / Unused Capacity: {probe_m2_kt:,.3f} kTokens/s")

    # Write/Append results to CSV
    csv_path = Path(args.csv_out).resolve()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    
    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    
    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "resource_type",
                "resource_type_mode",
                "baseline_xput_in_method_1",
                "unused_by_method_1",
                "baseline_xput_in_method_2",
                "unused_by_method_2"
            ])
        for row in results_to_append:
            m1_bg_str = f"{row['baseline_xput_in_method_1']:.3f}" if isinstance(row['baseline_xput_in_method_1'], float) else ""
            m1_pr_str = f"{row['unused_by_method_1']:.3f}" if isinstance(row['unused_by_method_1'], float) else ""
            m2_bg_str = f"{row['baseline_xput_in_method_2']:.3f}" if isinstance(row['baseline_xput_in_method_2'], float) else ""
            m2_pr_str = f"{row['unused_by_method_2']:.3f}" if isinstance(row['unused_by_method_2'], float) else ""
            writer.writerow([
                row["resource_type"],
                row["resource_type_mode"],
                m1_bg_str,
                m1_pr_str,
                m2_bg_str,
                m2_pr_str
            ])
            
    print(f"\n---> Results successfully appended to {csv_path}")

    # Post-flight clean
    clean_scratch_files(tmp_dir)


if __name__ == "__main__":
    main()
