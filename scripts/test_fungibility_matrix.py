#!/usr/bin/env python3
"""
test_fungibility_matrix.py
==========================
Runs a 4x4 matrix experiment to prove the fungibility of the "unit of measure".
It tests every combination of Background IO_MODE and Probe IO_MODE, proving that 
the Normalized App Footprint calculation ((Capacity - Slack) / Capacity) yields 
a stable measurement of the application's size regardless of the probe used.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

IO_MODES = ["rand_write", "rand_read", "seq_write", "seq_read"]

def run_calibration(out_dir, duration, skip_calibrate):
    capacities = {}
    print("\n" + "="*60)
    print(" Phase 1: Calibrate Capacity for all I/O Flavors")
    print("="*60)
    
    for mode in IO_MODES:
        cap_file = out_dir / f"cap_{mode}.json"
        if skip_calibrate and cap_file.exists():
            with open(cap_file) as f:
                capacities[mode] = json.load(f)["peak_throughput"] / 1000.0
            print(f"[SKIP] {mode} capacity: {capacities[mode]:.2f} kOps/s")
            continue
            
        print(f"\n---> Calibrating {mode} ...")
        env = os.environ.copy()
        env["IO_MODE"] = mode
        env["RESOURCE_TYPE"] = "io"
        # Ensure cmake is not invoked in runtime container
        env["SKIP_BUILD"] = "1"
        
        cmd = ["bash", "scripts/run_calibrate.sh", "--duration", str(duration), "--output", str(cap_file)]
        subprocess.run(cmd, env=env, check=True)
        
        with open(cap_file) as f:
            capacities[mode] = json.load(f)["peak_throughput"] / 1000.0
        print(f"     Capacity for {mode}: {capacities[mode]:.2f} kOps/s")
        
    return capacities

def plot_matrix(csv_path, out_plot):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("\n[Warning] matplotlib not installed. Data saved to CSV, but skipping plot.")
        return

    # Parse CSV: bg_mode -> {probe_mode: footprint}
    data = {bg: {pr: 0.0 for pr in IO_MODES} for bg in IO_MODES}
    
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bg = row["bg_mode"]
            pr = row["probe_mode"]
            if bg in data and pr in data[bg]:
                data[bg][pr] = float(row["footprint_pct"])

    fig, ax = plt.subplots(figsize=(11, 6))
    
    x = np.arange(len(IO_MODES))
    width = 0.2
    colors = ['#4c72b0', '#dd8452', '#55a868', '#c44e52']
    
    for i, probe_mode in enumerate(IO_MODES):
        y_vals = [data[bg_mode][probe_mode] for bg_mode in IO_MODES]
        offset = (i - 1.5) * width
        ax.bar(x + offset, y_vals, width, label=f"Probe: {probe_mode}", 
               color=colors[i], edgecolor="white", zorder=3)
        
    ax.set_ylabel("Normalized App Footprint (%)\n(Capacity - Slack) / Capacity", fontsize=11, fontweight="bold")
    ax.set_title("Unit of Measure Fungibility:\nFootprint Measurement Invariance Across Different Probes", 
                 fontsize=13, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([f"BG Workload:\n{m}" for m in IO_MODES], fontsize=10)
    ax.legend(title="Unit of Measure (Probe)", loc="upper left", bbox_to_anchor=(1, 1))
    ax.set_ylim(0, 105)
    ax.grid(axis='y', linestyle=':', alpha=0.7, zorder=0)
    
    fig.tight_layout()
    fig.savefig(out_plot, dpi=150)
    print(f"\n---> Success! Matrix plot saved to {out_plot}")


def main():
    parser = argparse.ArgumentParser(description="Test 4x4 Fungibility Matrix.")
    parser.add_argument("--bg-procs", type=int, default=8)
    parser.add_argument("--bg-io-mix", type=float, default=0.3)
    parser.add_argument("--bg-mem-mix", type=float, default=0.3)
    parser.add_argument("--bg-intensity", type=float, default=0.75)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--out-dir", type=str, default="results/fungibility_matrix")
    parser.add_argument("--skip-calibrate", action="store_true", help="Skip calibration if results exist")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "fungibility_results.csv"

    capacities = run_calibration(out_dir, args.duration, args.skip_calibrate)

    print("\n" + "="*60)
    print(" Phase 2: Probe Slack for 4x4 Matrix")
    print("="*60)
    
    # Initialize CSV
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["bg_mode", "probe_mode", "capacity_kt", "slack_kt", "app_usage_kt", "footprint_pct"])

    # Nested loop for the 16 combinations
    for bg_mode in IO_MODES:
        print("\n" + "-"*50)
        print(f" Evaluating Background Workload: {bg_mode.upper()}")
        print("-" * 50)
        
        for probe_mode in IO_MODES:
            print(f"\n  ---> Probing with {probe_mode} ...")
            
            sweep_dir = out_dir / f"bg_{bg_mode}" / f"probe_{probe_mode}"
            sweep_dir.mkdir(parents=True, exist_ok=True)
            
            env = os.environ.copy()
            env["SWEEP"] = "io"
            env["BG_PROCS"] = str(args.bg_procs)
            env["BG_IO_MIX"] = str(args.bg_io_mix)
            env["BG_MEM_MIX"] = str(args.bg_mem_mix)
            env["BG_INTENSITY"] = str(args.bg_intensity)
            env["BG_IO_MODE"] = bg_mode
            env["PROBE_IO_MODE"] = probe_mode
            env["DURATION"] = str(args.duration)
            env["OUTPUT_DIR"] = str(sweep_dir)
            env["DISABLE_COLLECTORS"] = "1"
            
            cmd = ["bash", "scripts/run_loaded_sweep.sh"]
            try:
                subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                sys.stderr.write(f"\n============================================================\n")
                sys.stderr.write(f"ERROR: run_loaded_sweep.sh failed with exit code {e.returncode}\n")
                sys.stderr.write(f"Command: {e.cmd}\n")
                sys.stderr.write(f"--- STDOUT ---\n{e.stdout}\n")
                sys.stderr.write(f"--- STDERR ---\n{e.stderr}\n")
                sys.stderr.write(f"============================================================\n")
                sys.stderr.flush()
                
                # Also write to absolute volume mount path to ensure sync to host
                try:
                    with open("/app/results/sweep_error.log", "w") as ef:
                        ef.write(f"Command: {e.cmd}\n")
                        ef.write(f"Exit code: {e.returncode}\n")
                        ef.write(f"STDOUT:\n{e.stdout}\n")
                        ef.write(f"STDERR:\n{e.stderr}\n")
                except Exception as write_err:
                    sys.stderr.write(f"Failed to write log file: {write_err}\n")
                    sys.stderr.flush()
                
                raise e
            
            sweep_file = sweep_dir / "sweep_io.json"
            if not sweep_file.exists():
                print(f"       ERROR: sweep failed to produce {sweep_file}")
                continue
                
            with open(sweep_file) as f:
                slack = json.load(f).get("slack_ktokens", 0.0)
                
            cap = capacities[probe_mode]
            app_usage = cap - slack
            footprint_pct = (app_usage / cap) * 100 if cap > 0 else 0
            
            print(f"       Capacity:  {cap:.2f} kT/s")
            print(f"       Slack:     {slack:.3f} kT/s")
            print(f"       Footprint: {footprint_pct:.2f}%")
            
            with open(csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([bg_mode, probe_mode, cap, slack, app_usage, footprint_pct])

    print("\n" + "="*60)
    print(" Phase 3: Generate Plot")
    print("="*60)
    plot_matrix(csv_path, out_dir / "fungibility_plot.png")

if __name__ == "__main__":
    main()
