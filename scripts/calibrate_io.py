#!/usr/bin/env python3
import subprocess
import json
import os
import sys

# Configuration
WORKER_BIN = "./build/worker"
DURATION = 30
TMP_DIR = "/holly/slack-meter-calibrate"

def run_workers(num_full_workers, fractional_intensity=0.0):
    """Spawns N full workers + 1 optional fractional worker and returns total I/O throughput."""
    msg = f"Running {num_full_workers} worker(s)"
    if fractional_intensity > 0:
        msg += f" + 1 fractional ({fractional_intensity:.2f})"
    print(f"{msg}... ", end="", flush=True)
    
    processes = []
    # Full workers
    for i in range(num_full_workers):
        cmd = [
            WORKER_BIN,
            "--io-mix", "1.0",
            "--intensity", "1.0",
            "--duration", str(DURATION),
            "--tmp-dir", TMP_DIR,
            "--seed", str(1337 + i)
        ]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(p)
        
    # Fractional worker
    if fractional_intensity > 0:
        cmd = [
            WORKER_BIN,
            "--io-mix", "1.0",
            "--intensity", str(fractional_intensity),
            "--duration", str(DURATION),
            "--tmp-dir", TMP_DIR,
            "--seed", str(1337 + num_full_workers)
        ]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

def main():
    if not os.path.exists(WORKER_BIN):
        print(f"Error: Could not find worker binary at {WORKER_BIN}")
        print("Please build the project first.")
        sys.exit(1)
        
    print("==================================================")
    print(" Calibrating Maximum I/O Capacity (T_io) ")
    print("==================================================")
    print(f"Configuration: pure I/O (4KiB sync random writes), {DURATION}s duration")
    print(f"Tmp dir: {TMP_DIR}")
    print("--------------------------------------------------")

    os.makedirs(TMP_DIR, exist_ok=True)

    peak_throughput = 0.0
    optimal_workers = 0
    plateau_strikes = 0
    
    # 1. Sweep linearly starting at 1
    n = 1
    while True:
        throughput = run_workers(n)
        
        if throughput > peak_throughput * 1.02:
            peak_throughput = throughput
            optimal_workers = n
            plateau_strikes = 0
        else:
            plateau_strikes += 1
            
        if plateau_strikes >= 3:
            print("\nThroughput has plateaued. Stopping integer sweep.")
            break
        
        # Safety limit to avoid infinite loops
        if n >= 128:
            print("\nReached 128 processes. Stopping integer sweep.")
            break
            
        n += 1

    # 3. Optional binary search on (n+1)th process
    print("\n--- Phase 2: Binary Search on Fractional Worker ---")
    print(f"Searching for hidden capacity with {optimal_workers} full workers + 1 fractional worker")
    
    low = 0.0
    high = 1.0
    best_throughput = peak_throughput
    best_intensity = 0.0
    
    # 5 steps gives roughly 3% precision in intensity
    for _ in range(5): 
        mid = (low + high) / 2.0
        t = run_workers(optimal_workers, mid)
        
        if t > best_throughput:
            # It improved! Try pushing the fractional intensity higher.
            best_throughput = t
            best_intensity = mid
            low = mid
        else:
            # It degraded or plateaued. Back off the fractional intensity.
            high = mid

    print("==================================================")
    print(" Calibration Complete ")
    print("==================================================")
    print(f"Peak I/O Throughput (T_io): {best_throughput:,.0f} ops/s")
    print(f"Achieved at concurrency:    {optimal_workers} full + 1 fractional ({best_intensity:.2f})")
    print("--------------------------------------------------")
    
    k_tokens = best_throughput / 1000.0
    print(f"System I/O Capacity:        {k_tokens:,.2f} kTokens")
    print("==================================================")

if __name__ == "__main__":
    main()
