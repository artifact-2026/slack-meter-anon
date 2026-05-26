#!/usr/bin/env python3
"""
test_fungibility_matrix.py
==========================
Runs a 3x4 matrix experiment to prove the fungibility of the "unit of measure".
It tests combinations of Background IO_MODE and Probe IO_MODE, proving that 
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
from getpass import getuser

# The 3 background modes requested (write only, read only, read and write mixed)
BG_MODES = ["rand_write", "rand_read", "rand_rw"]

# The 4 valid probe modes supported by the C++ worker
PROBE_MODES = ["rand_write", "rand_read", "rand_read_64k", "seq_read"]

def load_capacities(modes, capacity_file=None, capacities_arg=None, out_dir=None):
    caps = {}
    if capacities_arg:
        try:
            caps = json.loads(capacities_arg)
        except json.JSONDecodeError:
            for part in capacities_arg.split(","):
                if ":" in part:
                    k, v = part.split(":", 1)
                    caps[k.strip()] = float(v.strip())
    elif capacity_file:
        with open(capacity_file) as f:
            data = json.load(f)
            if isinstance(data, dict):
                caps = data

    # For any missing modes, try to find cap_<mode>.json in out_dir or results/calibration
    for mode in modes:
        if mode not in caps or caps[mode] is None:
            found = False
            for parent in [out_dir, Path("results/calibration"), Path("results/loaded_sweep")]:
                if parent:
                    p = Path(parent) / f"cap_{mode}.json"
                    if p.exists():
                        try:
                            with open(p) as f:
                                d = json.load(f)
                                caps[mode] = d["peak_throughput"] / 1000.0
                                print(f"Loaded capacity for {mode} from {p}: {caps[mode]:.2f} kOps/s")
                                found = True
                                break
                        except Exception as e:
                            print(f"[Warning] Failed to read {p}: {e}")
            if not found:
                print(f"ERROR: No capacity provided or found for probe mode: {mode}")
                print("Please run calibration first, or pass capacities via --capacities or --capacity-file.")
                sys.exit(1)
    return caps

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
    data = {bg: {pr: 0.0 for pr in PROBE_MODES} for bg in BG_MODES}
    
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bg = row["bg_mode"]
            pr = row["probe_mode"]
            if bg in data and pr in data[bg]:
                data[bg][pr] = float(row["footprint_pct"])

    fig, ax = plt.subplots(figsize=(11, 6))
    
    x = np.arange(len(BG_MODES))
    width = 0.15
    colors = ['#4c72b0', '#dd8452', '#55a868', '#c44e52']
    
    for i, probe_mode in enumerate(PROBE_MODES):
        y_vals = [data[bg_mode][probe_mode] for bg_mode in BG_MODES]
        offset = (i - 1.5) * width
        ax.bar(x + offset, y_vals, width, label=f"Probe: {probe_mode}", 
               color=colors[i], edgecolor="white", zorder=3)
        
    ax.set_ylabel("Normalized App Footprint (%)\n(Capacity - Slack) / Capacity", fontsize=11, fontweight="bold")
    ax.set_title("Unit of Measure Fungibility:\nFootprint Measurement Invariance Across Different Probes", 
                 fontsize=13, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([f"BG Workload:\n{m}" for m in BG_MODES], fontsize=10)
    ax.legend(title="Unit of Measure (Probe)", loc="upper left", bbox_to_anchor=(1, 1))
    ax.set_ylim(0, 105)
    ax.grid(axis='y', linestyle=':', alpha=0.7, zorder=0)
    
    fig.tight_layout()
    fig.savefig(out_plot, dpi=150)
    print(f"\n---> Success! Matrix plot saved to {out_plot}")


def clean_scratch_files():
    tmp_dir = Path("/holly/slack-meter-loaded-sweep")
    if not (tmp_dir.exists() and os.access(tmp_dir, os.W_OK)):
        tmp_dir = Path("/tmp/slack-meter-loaded-sweep")
    
    if tmp_dir.exists():
        print(f"\nCleaning up scratch files in {tmp_dir}...")
        for p in tmp_dir.glob("sm_io_*.dat"):
            try:
                p.unlink()
            except Exception as e:
                print(f"[Warning] Failed to delete {p}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Test 3x4 Fungibility Matrix.")
    parser.add_argument("--bg-procs", type=int, default=8)
    parser.add_argument("--bg-io-mix", type=float, default=0.3)
    parser.add_argument("--bg-mem-mix", type=float, default=0.3)
    parser.add_argument("--bg-intensity", type=float, default=0.75)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--out-dir", type=str, default="results/fungibility_matrix")
    parser.add_argument("--capacities", type=str, default=None,
                        help="JSON string or comma-separated key-value list of capacities, e.g., 'rand_read:180,rand_write:25'")
    parser.add_argument("--capacity-file", type=str, default=None,
                        help="JSON file containing the capacity mapping")
    parser.add_argument("--queue-depth", type=int, default=1,
                        help="default queue depth/concurrency per worker (default: 1)")
    parser.add_argument("--bg-queue-depth", type=int, default=None,
                        help="queue depth/concurrency per background worker")
    parser.add_argument("--probe-queue-depth", type=int, default=None,
                        help="queue depth/concurrency per probe worker")
    parser.add_argument("--drop-pct", type=float, default=0.10,
                        help="interference drop threshold percentage (default: 0.10)")
    parser.add_argument("--interference-count", type=int, default=3,
                        help="interference count to terminate Phase 1 (default: 3)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "fungibility_results.csv"

    # Clean any stale scratch files from previous runs
    clean_scratch_files()

    # Pre-flight: ensure no entries in out_dir are owned by a different user
    current_uid = os.getuid()
    bad_paths = [
        p for p in out_dir.rglob("*")
        if p.stat().st_uid != current_uid
    ]
    if bad_paths:
        print("\nERROR: The following paths in the output directory are owned by a different user")
        print("       (likely created by a previous 'sudo' run). Fix with:\n")
        print(f"  sudo chown -R {getuser()} {out_dir}\n")
        for p in bad_paths:
            print(f"  {p}")
        sys.exit(1)

    # Load capacities (either from CLI, file, or preexisting calibration artifacts)
    capacities = load_capacities(PROBE_MODES, args.capacity_file, args.capacities, out_dir)

    print("\n" + "="*60)
    print(" Phase 2: Probe Slack for 3x4 Matrix")
    print("="*60)
    
    # Initialize CSV
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["bg_mode", "probe_mode", "capacity_kt", "slack_kt", "app_usage_kt", "footprint_pct"])

    # Nested loop for the 12 combinations
    for bg_mode in BG_MODES:
        print("\n" + "-"*50)
        print(f" Evaluating Background Workload: {bg_mode.upper()}")
        print("-" * 50)
        
        for probe_mode in PROBE_MODES:
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
            
            # Forward the queue depth, drop threshold, and early stop count
            env["QUEUE_DEPTH"] = str(args.queue_depth)
            env["BG_QUEUE_DEPTH"] = str(args.bg_queue_depth if args.bg_queue_depth is not None else args.queue_depth)
            if probe_mode == "rand_read":
                env["PROBE_QUEUE_DEPTH"] = "32"
            else:
                env["PROBE_QUEUE_DEPTH"] = str(args.probe_queue_depth if args.probe_queue_depth is not None else args.queue_depth)
            env["DROP_PCT"] = str(args.drop_pct)
            env["INTERFERENCE_COUNT"] = str(args.interference_count)
            
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
                
                # Also write to diagnostic log file in output directory
                try:
                    with open(out_dir / "sweep_error.log", "w") as ef:
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

    # Final cleanup of scratch files
    clean_scratch_files()

if __name__ == "__main__":
    main()
