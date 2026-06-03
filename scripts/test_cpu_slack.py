#!/usr/bin/env python3
"""
test_cpu_slack.py
=================
Automated test to show CPU measurements and verify the conservation of slack:
1. Iterate through the 3 CPU modes (cpu_int, cpu_fp, cpu_hash).
2. For each CPU mode:
   - Measure the hardware CPU capacity until saturation (C), and get the
     corresponding saturation process count (P0).
   - Run P1 background processes (P1 < P0) and measure the total throughput (R).
   - Probe the slack (S) using probe.py while the P1 background processes run.
3. Report C, R, S, and verify the conservation ratio (R + S) / C.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
WORKER_BIN = REPO_ROOT / "build" / "worker"
SATURATE_SCRIPT = REPO_ROOT / "scripts" / "saturate.py"
TMP_DIR = Path("/holly/slack-meter-test-cpu-slack")


def build_worker():
    """Build the worker binary if needed."""
    print("Building slack-meter...")
    build_dir = REPO_ROOT / "build"
    subprocess.run(["cmake", "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release", "-S", str(REPO_ROOT)], check=True)
    subprocess.run(["cmake", "--build", str(build_dir), "--parallel"], check=True)
    if not WORKER_BIN.exists():
        print(f"Error: Build succeeded but worker binary not found at {WORKER_BIN}")
        sys.exit(1)


def get_saturation_capacity(cpu_mode, duration, warmup, step, start_n=None):
    """Run saturate.py to measure hardware CPU capacity C and process count P0."""
    print(f"\n[Step 1/3] Measuring CPU saturation capacity (C) for mode: {cpu_mode}...")
    output_file = TMP_DIR / f"sat_{cpu_mode}.json"
    cmd = [
        sys.executable,
        str(SATURATE_SCRIPT),
        "--resource-type", "cpu",
        "--cpu-mode", cpu_mode,
        "--duration", str(duration),
        "--warmup", str(warmup),
        "--step", str(step),
        "--output", str(output_file),
        "--tmp-dir", str(TMP_DIR)
    ]
    if start_n is not None:
        cmd += ["--start-n", str(start_n)]
    subprocess.run(cmd, check=True)
    
    with open(output_file) as f:
        data = json.load(f)
    
    C = data["peak_throughput"]
    P0 = data["optimal_workers"]
    print(f"  => Saturation Capacity (C) = {C:,.2f} ops/s at P0 = {P0} workers")
    return C, P0


def run_concurrent_bg_probe(cpu_mode, p_bg, p_probe, duration, warmup):
    """Run p_bg background workers and p_probe probing workers concurrently.
    Return (total_bg_throughput, total_probe_throughput).
    """
    print(f"  Running concurrently: P_bg = {p_bg} workers, P_probe = {p_probe} workers...")
    processes = []
    
    # Launch background processes
    for i in range(p_bg):
        cmd = [
            str(WORKER_BIN),
            "--io-mix", "0.0",
            "--mem-mix", "0.0",
            "--intensity", "1.0",
            "--duration", str(duration),
            "--warmup", str(warmup),
            "--tmp-dir", str(TMP_DIR),
            "--seed", str(1337 + i),
            "--cpu-mode", cpu_mode,
            "--mem-mode", "mem_copy"
        ]
        processes.append((subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), "bg"))
        
    # Launch probing processes
    for i in range(p_probe):
        cmd = [
            str(WORKER_BIN),
            "--io-mix", "0.0",
            "--mem-mix", "0.0",
            "--intensity", "1.0",
            "--duration", str(duration),
            "--warmup", str(warmup),
            "--tmp-dir", str(TMP_DIR),
            "--seed", str(2337 + i),
            "--cpu-mode", cpu_mode,
            "--mem-mode", "mem_copy"
        ]
        processes.append((subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), "probe"))

    total_r = 0.0
    total_s = 0.0
    
    for i, (p, p_type) in enumerate(processes):
        stdout, stderr = p.communicate()
        if p.returncode != 0:
            print(f"Error: Process {i} ({p_type}) failed: {stderr}")
            sys.exit(1)
        try:
            data = json.loads(stdout.strip())
            tput = data.get("cpu_throughput", data.get("throughput", 0.0))
            if p_type == "bg":
                total_r += tput
            else:
                total_s += tput
        except json.JSONDecodeError:
            print(f"Error: Failed to parse worker output: {stdout}")
            sys.exit(1)
            
    print(f"    => Background Throughput (R) = {total_r:,.2f} ops/s")
    print(f"    => Probing Throughput (S) = {total_s:,.2f} ops/s")
    return total_r, total_s


def clean_temp_files(keep_sat=False):
    """Clean up JSON files and directories in TMP_DIR."""
    if TMP_DIR.exists():
        for p in TMP_DIR.glob("*"):
            try:
                if p.is_file():
                    if keep_sat and p.name.startswith("sat_") and p.name.endswith(".json"):
                        continue
                    p.unlink()
            except Exception as e:
                print(f"[Warning] Failed to delete {p}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Test CPU measurement flow and slack conservation.")
    parser.add_argument("--duration", type=int, default=30, help="Duration for each worker run in seconds")
    parser.add_argument("--warmup", type=int, default=15, help="Warmup duration for each worker run in seconds")
    parser.add_argument("--step", type=int, default=1, help="Saturate sweep step size")
    parser.add_argument("--start-n", type=int, default=None, help="Skip straight to this concurrency level for saturation sweep")
    parser.add_argument("--modes", nargs="+", choices=["cpu_int", "cpu_fp", "cpu_hash"], 
                        default=["cpu_int", "cpu_fp", "cpu_hash"], 
                        help="CPU modes to run (choices: cpu_int, cpu_fp, cpu_hash)")
    parser.add_argument("--phase", choices=["sat", "slack", "full"], default="full",
                        help="Which phase(s) of the test to run: sat (only saturation sweep), slack (only slack sweep), or full (both)")
    parser.add_argument("--p0", type=int, default=None, help="Explicit P0 for the slack phase (if running slack alone)")
    parser.add_argument("--c-cap", type=float, default=None, help="Explicit Capacity C (ops/s) for the slack phase (if running slack alone)")
    parser.add_argument("--skip-build", action="store_true", help="Skip building the worker binary")
    args = parser.parse_args()

    TMP_DIR.mkdir(parents=True, exist_ok=True)

    if not args.skip_build:
        build_worker()

    modes = args.modes
    phase = args.phase

    # Step 1: Saturation Capacity C
    sat_results = {}
    if phase in ("sat", "full"):
        for mode in modes:
            print("\n" + "=" * 70)
            print(f" SATURATION SWEEP: CPU Mode = {mode}")
            print("=" * 70)
            C, P0 = get_saturation_capacity(mode, args.duration, args.warmup, args.step, args.start_n)
            sat_results[mode] = {"C": C, "P0": P0}

    # Step 2: Slack Phase (Direct concurrent execution)
    results = {}
    if phase in ("slack", "full"):
        for mode in modes:
            print("\n" + "=" * 70)
            print(f" SLACK EXPERIMENT: CPU Mode = {mode}")
            print("=" * 70)
            
            # Determine C and P0
            if args.p0 is not None and args.c_cap is not None:
                P0 = args.p0
                C = args.c_cap
                print(f"Using CLI supplied P0 = {P0}, Capacity C = {C:,.2f} ops/s")
            elif mode in sat_results:
                P0 = sat_results[mode]["P0"]
                C = sat_results[mode]["C"]
            else:
                # Attempt to load from JSON
                sat_file = TMP_DIR / f"sat_{mode}.json"
                if sat_file.exists():
                    print(f"Loading saturation data from {sat_file}...")
                    with open(sat_file) as f:
                        data = json.load(f)
                    C = data["peak_throughput"]
                    P0 = data["optimal_workers"]
                    print(f"  => Loaded Saturation Capacity (C) = {C:,.2f} ops/s at P0 = {P0} workers")
                else:
                    print(f"Error: Saturation file {sat_file} not found and no CLI values supplied for mode '{mode}'.")
                    print("Please run with '--phase sat' or '--phase full' first, or supply '--p0' and '--c-cap'.")
                    sys.exit(1)
            
            if P0 <= 0:
                print(f"Warning: P0 is {P0}. Cannot run slack phase for mode '{mode}'.")
                continue
                
            runs = []
            fractions = [0.25, 0.50, 0.75, 1.00]
            for frac in fractions:
                p_bg = min(P0, max(0, int(frac * P0 + 0.5)))
                p_probe = P0 - p_bg
                print(f"\n[Running Fraction {frac:.2f}] P_bg = {p_bg}, P_probe = {p_probe}")
                R, S = run_concurrent_bg_probe(mode, p_bg, p_probe, args.duration, args.warmup)
                comb = R + S
                ratio = (comb / C) * 100.0 if C > 0 else 0.0
                runs.append({
                    "fraction": frac,
                    "p_bg": p_bg,
                    "p_probe": p_probe,
                    "R": R,
                    "S": S,
                    "R+S": comb,
                    "ratio": ratio
                })
            
            results[mode] = {
                "C": C,
                "P0": P0,
                "runs": runs
            }

    # Print Report for Slack Phase
    if phase in ("slack", "full") and results:
        print("\n" + "=" * 115)
        print("                               FINAL REPORT: CPU Slack & Capacity Conservation")
        print("=" * 115)
        print(f"{'CPU Mode':<12} | {'P0':<4} | {'BG (P_bg)':<10} | {'Probe (P_prb)':<14} | {'Capacity C (ops/s)':<20} | {'BG Load R (ops/s)':<20} | {'Probing S (ops/s)':<20} | {'(R+S)/C Ratio':<13}")
        print("-" * 115)
        for mode, data in results.items():
            C = data["C"]
            P0 = data["P0"]
            for idx, r in enumerate(data["runs"]):
                mode_str = mode if idx == 0 else ""
                p0_str = str(P0) if idx == 0 else ""
                bg_desc = f"{r['p_bg']} ({int(r['fraction']*100)}%)"
                print(f"{mode_str:<12} | {p0_str:<4} | {bg_desc:<10} | {r['p_probe']:<14} | {C:>20,.1f} | {r['R']:>20,.1f} | {r['S']:>20,.1f} | {r['ratio']:>11.1f}%")
            print("-" * 115)
        print("=" * 115)
    elif phase == "sat" and sat_results:
        print("\n" + "=" * 60)
        print("                   SATURATION RESULTS")
        print("=" * 60)
        print(f"{'CPU Mode':<12} | {'Optimal P0':<10} | {'Peak Capacity C (ops/s)':<25}")
        print("-" * 60)
        for mode, data in sat_results.items():
            print(f"{mode:<12} | {data['P0']:<10} | {data['C']:>25,.1f}")
        print("=" * 60)

    clean_temp_files(keep_sat=True)


if __name__ == "__main__":
    main()
