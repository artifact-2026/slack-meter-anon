#!/usr/bin/env python3
"""
test_fungibility_matrix.py
==========================
Runs a matrix experiment to prove the fungibility of the I/O "unit of measure".
It tests combinations of Background I/O workload intensity and Probe I/O Mode under a
rw_mixed background workload, proving that the Normalized App Footprint calculation
((Capacity - Slack) / Capacity) yields a stable measurement of the application's size
regardless of the probe used.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from getpass import getuser

# The 4 fungible I/O probe modes
PROBE_MODES = ["rand_write", "rand_read", "seq_write", "seq_read"]

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

    # Parse CSV: probe_mode -> {bg_intensity: footprint}
    data = {}
    intensities = set()
    footprints = []
    
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pr = row["probe_mode"]
            intensity = float(row["bg_intensity"])
            footprint = float(row["footprint_pct"])
            intensities.add(intensity)
            footprints.append(footprint)
            if pr not in data:
                data[pr] = {}
            data[pr][intensity] = footprint

    sorted_intensities = sorted(list(intensities))
    
    fig, ax = plt.subplots(figsize=(11, 6))
    
    x = np.arange(len(sorted_intensities))
    num_probes = len(PROBE_MODES)
    width = 0.7 / max(num_probes, 1)

    colors = ['#1565C0', '#E64A19', '#2E7D32', '#6A1B9A']
    
    for i, probe_mode in enumerate(PROBE_MODES):
        if probe_mode not in data:
            continue
        y_vals = [data[probe_mode].get(intensity, 0.0) for intensity in sorted_intensities]
        offset = (i - (num_probes - 1) / 2.0) * width
        ax.bar(x + offset, y_vals, width, label=f"Probe: {probe_mode}", 
               color=colors[i % len(colors)], edgecolor="white", alpha=0.88, zorder=3)
        
    ax.set_ylabel("Normalized App Footprint (%)\n(Capacity - Slack) / Capacity", fontsize=11, fontweight="bold")
    ax.set_title("I/O Unit of Measure Fungibility:\nFootprint Measurement Invariance Across Different Probes and Intensities", 
                 fontsize=12, fontweight="bold", pad=15)
    ax.set_xticks(x)
    ax.set_xticklabels([f"BG Intensity:\n{int(intensity * 100)}%" for intensity in sorted_intensities], fontsize=10)
    
    import matplotlib.ticker as ticker
    ax.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=100.0))
    
    ax.legend(title="Unit of Measure (Probe)", loc="upper left", bbox_to_anchor=(1, 1))
    
    min_pct = min(footprints) if footprints else 0.0
    max_pct = max(footprints) if footprints else 100.0
    min_y = min(0.0, min_pct - 5.0)
    max_y = max(105.0, max_pct + 5.0)
    ax.set_ylim(min_y, max_y)
    
    ax.grid(axis='y', linestyle=':', alpha=0.7, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    
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
    parser = argparse.ArgumentParser(description="Test I/O Fungibility Matrix with mixed background at varying intensities.")
    parser.add_argument("--bg-procs", type=int, default=8)
    parser.add_argument("--bg-io-mix", type=float, default=0.3)
    parser.add_argument("--bg-mem-mix", type=float, default=0.3)
    parser.add_argument("--bg-intensities", type=str, default="0.2,0.4,0.6,0.8",
                        help="comma-separated list of background I/O intensities to sweep (default: 0.2,0.4,0.6,0.8)")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--out-dir", type=str, default="results/fungibility_matrix")
    parser.add_argument("--capacities", type=str, default=None,
                        help="JSON string or comma-separated key-value list of capacities, e.g., 'rand_write:100,rand_read:95,seq_write:80,seq_read:90'")
    parser.add_argument("--capacity-file", type=str, default=None,
                        help="JSON file containing the capacity mapping")
    parser.add_argument("--queue-depth", type=int, default=1,
                        help="default queue depth/concurrency per worker (default: 1)")
    parser.add_argument("--bg-queue-depth", type=int, default=None,
                        help="queue depth/concurrency per background worker")
    parser.add_argument("--probe-queue-depth", type=int, default=None,
                        help="queue depth/concurrency per probe worker")
    parser.add_argument("--only-plot", action="store_true",
                        help="Skip running sweeps and only generate the plot from existing CSV")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "fungibility_results.csv"

    # Clean any stale scratch files from previous runs
    clean_scratch_files()

    # Pre-flight: ensure no entries in out_dir are owned by a different user
    current_uid = os.getuid()
    if current_uid != 0:
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

    # Parse intensities to sweep
    intensities = [float(x.strip()) for x in args.bg_intensities.split(",") if x.strip()]

    # Load capacities (either from CLI, file, or preexisting calibration artifacts)
    if not args.only_plot:
        capacities = load_capacities(PROBE_MODES, args.capacity_file, args.capacities, out_dir)

        print("\n" + "="*60)
        print(" Phase 2: Probe Slack for I/O Fungibility Matrix")
        print("="*60)
        
        # Initialize CSV
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["bg_mode", "probe_mode", "bg_intensity", "capacity_kt", "slack_kt", "app_usage_kt", "footprint_pct"])

        # Loop over intensities
        for intensity in intensities:
            print("\n" + "-"*50)
            print(f" Evaluating Background Workload (rw_mixed) at Intensity: {intensity:.2f}")
            print("-" * 50)
            
            for probe_mode in PROBE_MODES:
                print(f"\n  ---> Probing with {probe_mode} ...")
                
                sweep_dir = out_dir / f"bg_intensity_{intensity:.2f}" / f"probe_{probe_mode}"
                sweep_dir.mkdir(parents=True, exist_ok=True)
                
                env = os.environ.copy()
                env["SWEEP"] = "io"
                env["BG_PROCS"] = str(args.bg_procs)
                env["BG_IO_MIX"] = str(args.bg_io_mix)
                env["BG_MEM_MIX"] = str(args.bg_mem_mix)
                env["BG_INTENSITY"] = str(intensity)
                env["BG_IO_MODE"] = "rw_mixed"
                env["PROBE_IO_MODE"] = probe_mode
                env["DURATION"] = str(args.duration)
                env["OUTPUT_DIR"] = str(sweep_dir)
                env["DISABLE_COLLECTORS"] = "1"
                
                # Forward configurations
                env["QUEUE_DEPTH"] = str(args.queue_depth)
                env["BG_QUEUE_DEPTH"] = str(args.bg_queue_depth if args.bg_queue_depth is not None else args.queue_depth)
                env["PROBE_QUEUE_DEPTH"] = str(args.probe_queue_depth if args.probe_queue_depth is not None else args.queue_depth)
                cmd = ["bash", "scripts/run_loaded_sweep.sh"]
                try:
                    # Run and let stdout/stderr stream directly to the terminal in real-time
                    subprocess.run(cmd, env=env, check=True)
                except subprocess.CalledProcessError as e:
                    sys.stderr.write(f"\n============================================================\n")
                    sys.stderr.write(f"ERROR: run_loaded_sweep.sh failed with exit code {e.returncode}\n")
                    sys.stderr.write(f"Command: {e.cmd}\n")
                    sys.stderr.write(f"============================================================\n")
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
                
                print(f"       Capacity:  {cap*1000:.0f} Tokens/s")
                print(f"       Slack:     {slack*1000:.0f} Tokens/s")
                print(f"       Footprint: {footprint_pct:.2f}%")
                
                with open(csv_path, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["rw_mixed", probe_mode, intensity, cap, slack, app_usage, footprint_pct])

    print("\n" + "="*60)
    print(" Phase 3: Generate Plot")
    print("="*60)
    plot_matrix(csv_path, out_dir / "fungibility_plot.png")

    # Final cleanup of scratch files
    clean_scratch_files()

if __name__ == "__main__":
    main()
