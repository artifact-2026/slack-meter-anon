#!/usr/bin/env python3
"""
auto_scatter.py
===============
Automates running loaded sweeps to generate the data for the Utilization vs Slack scatter plot.

It runs `run_loaded_sweep.sh` in a loop, randomizing the background workload mix
and intensity. After each run, it extracts the baseline %util and the measured 
IO slack, appends them to a CSV, and then plots the results against the naive expectation.

To run:
  $ python3 scripts/auto_scatter.py --capacity <value> --iterations <value>

Example:
  $ python3 scripts/auto_scatter.py --capacity 355.0 --iterations 20
  $ ./scripts/auto_scatter.py --capacity 353.0 --iteration 10
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
    parser.add_argument("--capacity", type=float, default=None, 
                        help="Total calibrated IO capacity in kT/s (e.g. 355.0)")
    parser.add_argument("--only-plot", action="store_true",
                        help="Skip running the sweep loop and only generate the plot from the CSV data")
    parser.add_argument("--iterations", type=int, default=10, 
                        help="Number of sweep iterations to run (default: 10)")
    parser.add_argument("--bg-procs", type=int, default=8, 
                        help="Number of background workers (default: 8)")
    parser.add_argument("--out-csv", type=str, default="results/scatter_data.csv")
    parser.add_argument("--out-plot", type=str, default="results/scatter_plot.png")
    args = parser.parse_args()

    if not args.only_plot and args.capacity is None:
        parser.error("--capacity is required unless --only-plot is specified")

    out_csv = Path(args.out_csv)
    out_plot = Path(args.out_plot)

    if not args.only_plot:
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
            bg_procs_val = random.randint(1, args.bg_procs)
            bg_io_mix = round(random.uniform(0.1, 1.0), 2)
            bg_mem_mix = round(random.uniform(0.0, 1.0 - bg_io_mix) if bg_io_mix < 1.0 else 0.0, 2)
            bg_intensity = round(random.uniform(0.1, 1.0), 2)
            bg_io_mode = random.choice(["rand_write", "rand_read", "seq_write", "seq_read"])
            probe_io_mode = random.choice(["rand_write", "rand_read", "seq_write", "seq_read"])

            print(f"Running: BG_PROCS={bg_procs_val} | BG_IO_MIX={bg_io_mix} | BG_MEM_MIX={bg_mem_mix} | BG_INTENSITY={bg_intensity} | BG_IO_MODE={bg_io_mode} | PROBE_IO_MODE={probe_io_mode}")

            env = os.environ.copy()
            env["SWEEP"] = "io"
            env["BG_PROCS"] = str(bg_procs_val)
            env["BG_IO_MIX"] = str(bg_io_mix)
            env["BG_MEM_MIX"] = str(bg_mem_mix)
            env["BG_INTENSITY"] = str(bg_intensity)
            env["BG_IO_MODE"] = bg_io_mode
            env["PROBE_IO_MODE"] = probe_io_mode

            cmd = ["bash", "scripts/run_loaded_sweep.sh"]
            
            try:
                subprocess.run(cmd, env=env, check=True)
            except subprocess.CalledProcessError:
                print("[auto_scatter] ERROR: Sweep script failed. Skipping iteration.")
                continue

            # Parse Results
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

            # Save Data Point
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

    near_x, near_y = [], []
    above_x, above_y = [], []
    below_x, below_y = [], []

    for u, s in zip(utils, slacks):
        diff = u + s - 100.0
        if abs(diff) <= 15.0:
            near_x.append(u)
            near_y.append(s)
        elif diff > 15.0:
            above_x.append(u)
            above_y.append(s)
        else:
            below_x.append(u)
            below_y.append(s)

    # Actual Measured Data Points
    if near_x:
        ax.scatter(near_x, near_y, color="#7f7f7f", s=100, alpha=0.8, edgecolors="white", 
                   label="Near Naive Expectation (±5%)")
    if above_x:
        ax.scatter(above_x, above_y, color="#d62728", s=100, alpha=0.8, edgecolors="white", 
                   label="Above Line (Higher Slack)")
    if below_x:
        ax.scatter(below_x, below_y, color="#2ca02c", s=100, alpha=0.8, edgecolors="white", 
                   label="Below Line (Lower Slack)")

    ax.set_xlim(0, 105)
    ax.set_ylim(-5, 105)
    ax.set_xlabel("Average Baseline I/O Utilization (%util)", fontsize=12, fontweight="bold")
    cap_desc = f" (% of Calibrated {args.capacity} kT/s Capacity)" if args.capacity is not None else ""
    ax.set_ylabel(f"Extractable IO Slack\n{cap_desc}", fontsize=12, fontweight="bold")
    ax.set_title("The Utilization Fallacy: OS Metrics vs. True Hardware Capacity", fontsize=14, fontweight="bold")
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.legend(loc="upper right", fontsize=10, framealpha=0.9)
    
    fig.tight_layout()
    fig.savefig(out_plot, dpi=150)
    print(f"[auto_scatter] Success! Plot saved to {out_plot}")

if __name__ == "__main__":
    main()
