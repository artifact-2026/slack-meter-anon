#!/usr/bin/env python3
"""
auto_scatter.py
===============
Automates running loaded sweeps to generate the data for the Utilization vs Slack scatter plot.

It runs `run_loaded_sweep.sh` in a loop, randomizing the background workload mix
and intensity. After each run, it extracts the baseline %util and the measured 
IO slack, appends them to a CSV, and then plots the results against the naive expectation.
"""

import argparse
import csv
import json
import math
import os
import random
import subprocess
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Automate sweeps and plot util vs slack.")
    parser.add_argument("--capacity", type=float, required=True, 
                        help="Total calibrated IO capacity in kT/s (e.g. 355.0)")
    parser.add_argument("--iterations", type=int, default=10, 
                        help="Number of sweep iterations to run (default: 10)")
    parser.add_argument("--bg-procs", type=int, default=8, 
                        help="Number of background workers (default: 8)")
    parser.add_argument("--out-csv", type=str, default="results/scatter_data.csv")
    parser.add_argument("--out-plot", type=str, default="results/scatter_plot.png")
    args = parser.parse_args()

    out_csv = Path(args.out_csv)
    out_plot = Path(args.out_plot)

    # Ensure results directory exists
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    # Initialize CSV if it doesn't exist
    if not out_csv.exists():
        with open(out_csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["iteration", "bg_procs", "bg_io_mix", "bg_mem_mix", "bg_intensity", "util_pct", "slack_kt", "slack_pct"])

    # -----------------------------------------------------------------------
    # 1. Experiment Loop
    # -----------------------------------------------------------------------
    for i in range(1, args.iterations + 1):
        print(f"\n{'='*70}")
        print(f"Iteration {i} / {args.iterations}")
        print(f"{'='*70}")

        # Randomize parameters to generate a good scatter
        # We want to explore different bottlenecks: pure IO, pure Memory, mixed, etc.
        bg_procs_val = random.randint(1, args.bg_procs)
        bg_io_mix = round(random.uniform(0.1, 1.0), 2)
        # Ensure mem_mix + io_mix doesn't exceed 1.0 to be clean
        bg_mem_mix = round(random.uniform(0.0, 1.0 - bg_io_mix) if bg_io_mix < 1.0 else 0.0, 2)
        bg_intensity = round(random.uniform(0.1, 1.0), 2)

        print(f"Running: BG_PROCS={bg_procs_val} | BG_IO_MIX={bg_io_mix} | BG_MEM_MIX={bg_mem_mix} | BG_INTENSITY={bg_intensity}")

        env = os.environ.copy()
        env["SWEEP"] = "io"
        env["BG_PROCS"] = str(bg_procs_val)
        env["BG_IO_MIX"] = str(bg_io_mix)
        env["BG_MEM_MIX"] = str(bg_mem_mix)
        env["BG_INTENSITY"] = str(bg_intensity)
        # Reduce sweep duration slightly if you want to speed up tests, otherwise use defaults
        # env["DURATION"] = "20" 

        cmd = ["bash", "scripts/run_loaded_sweep.sh"]
        
        try:
            subprocess.run(cmd, env=env, check=True)
        except subprocess.CalledProcessError:
            print("[auto_scatter] ERROR: Sweep script failed. Skipping iteration.")
            continue

        # -------------------------------------------------------------------
        # 2. Parse Results
        # -------------------------------------------------------------------
        json_path = Path("results/loaded_sweep/sweep_io.json")
        iostat_path = Path("results/loaded_sweep/iostat.csv")

        if not json_path.exists() or not iostat_path.exists():
            print("[auto_scatter] ERROR: Output files not found. Skipping iteration.")
            continue

        # Extract measured Slack
        with open(json_path, "r") as f:
            data = json.load(f)
            slack_kt = data.get("slack_ktokens", 0.0)

        # Extract Average Baseline Utilization
        # The sweep's baseline phase runs first (typically 30s). 
        # We average the first 25 samples from iostat.csv to get the pure background utilization.
        util_vals = []
        with open(iostat_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    util_vals.append(float(row["%util"]))
                except (ValueError, KeyError):
                    pass

        if not util_vals:
            print("[auto_scatter] ERROR: No %util data in iostat.csv")
            continue

        # Average the first 25 seconds
        samples_to_average = min(25, len(util_vals))
        avg_util = sum(util_vals[:samples_to_average]) / samples_to_average
        
        # Calculate Slack as a percentage of total calibrated capacity
        slack_pct = (slack_kt / args.capacity) * 100.0

        # -------------------------------------------------------------------
        # 3. Save Data Point
        # -------------------------------------------------------------------
        with open(out_csv, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([i, bg_procs_val, bg_io_mix, bg_mem_mix, bg_intensity, avg_util, slack_kt, slack_pct])

        print(f"\n---> RESULT: Util = {avg_util:.1f}%, Slack = {slack_kt:.1f} kT/s ({slack_pct:.1f}% of {args.capacity})")

    # -----------------------------------------------------------------------
    # 4. Generate the Plot
    # -----------------------------------------------------------------------
    print("\nGenerating Scatter Plot...")
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("[auto_scatter] matplotlib not installed. Data saved to CSV, but plot skipped.")
        return

    utils = []
    slacks = []
    with open(out_csv, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            utils.append(float(row["util_pct"]))
            slacks.append(float(row["slack_pct"]))

    if not utils:
        print("[auto_scatter] No valid data in CSV to plot.")
        return

    fig, ax = plt.subplots(figsize=(9, 7))

    # Naive Expectation Line
    x_line = np.linspace(0, 100, 100)
    y_line = 100 - x_line
    ax.plot(x_line, y_line, linestyle="--", color="gray", linewidth=2.5, 
            label="Naive Expectation\n(Slack = 100% - %util)")

    # Actual Measured Data Points
    ax.scatter(utils, slacks, color="#1f77b4", s=100, alpha=0.8, edgecolors="white", 
               label="Measured IO Slack")

    ax.set_xlim(0, 105)
    ax.set_ylim(-5, 105)
    ax.set_xlabel("Average Baseline I/O Utilization (%util)", fontsize=12, fontweight="bold")
    ax.set_ylabel(f"Extractable IO Slack\n(% of Calibrated {args.capacity} kT/s Capacity)", fontsize=12, fontweight="bold")
    ax.set_title("The Utilization Fallacy: OS Metrics vs. True Hardware Capacity", fontsize=14, fontweight="bold")
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="upper right", fontsize=10, framealpha=0.9)
    
    fig.tight_layout()
    fig.savefig(out_plot, dpi=150)
    print(f"[auto_scatter] Success! Plot saved to {out_plot}")

if __name__ == "__main__":
    main()
