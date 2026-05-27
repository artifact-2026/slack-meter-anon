#!/usr/bin/env python3
"""
probe.py
========
Sweeps pure CPU, I/O, or RAM workers on top of a background workload, stopping when the
BACKGROUND throughput drops by DROP_PCT — not when the probe throughput plateaus.

Methodology
-----------
Phase 0  Baseline: run BG_PROCS background workers alone. Record throughput.

Phase 1  Linear sweep: add one full-intensity probe worker per round; stop when
         background throughput drops >= DROP_PCT.

Phase 2  Binary search: lock (n_full-1) probe workers at intensity=1.0, find
         the highest fractional intensity on the last one that leaves
         background throughput undisturbed.

All throughputs are reported in kTokens (1 ops/s = 1 token = 0.001 kTokens).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT  = Path(__file__).parent.parent.resolve()
WORKER_BIN = str(REPO_ROOT / "build" / "worker")


def _plot_slack_result(result: dict, out_path: Path) -> None:
    """Delegate to plot.py — keeps probe.py free of matplotlib."""
    try:
        from plot import plot_slack_result
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from plot import plot_slack_result  # type: ignore[no-redef]
    plot_slack_result(result, out_path)

_BG_SEED_BASE = 1000
_PROBE_SEED_BASE = 2000

_KT = 1e-3   # ops/s → kTokens


# ---------------------------------------------------------------------------
# Core probe
# ---------------------------------------------------------------------------

def run_probe(
    bg_procs:     int,
    bg_io_mix:    float,
    bg_mem_mix:   float,
    bg_intensity: float,
    n_probe_full: int,
    probe_frac:   float,
    probe_io_mix: float,
    probe_mem_mix: float,
    duration:     int,
    warmup:       int,
    tmp_dir:      str,
    worker_bin:   str,
    tput_key:     str,
    bg_io_mode:   str = "rand_write",
    probe_io_mode: str = "rand_write",
    samples:      int = 3,
    bg_queue_depth: int = 1,
    probe_queue_depth: int = 1,
    bg_cpu_mode:  str = "cpu_int",
    probe_cpu_mode: str = "cpu_int",
    bg_mem_mode:  str = "mem_copy",
    probe_mem_mode: str = "mem_copy",
    file_size_bytes: int = 0,
) -> tuple[float, float]:
    """Run bg + probe workers concurrently samples times; return median (bg_tput, probe_tput) in ops/s."""
    def make_cmd(io_mix: float, mem_mix: float, intensity: float, seed: int, mode: str, qd: int, cpu_mode: str, mem_mode: str) -> list[str]:
        cmd = [worker_bin,
               "--io-mix",    str(io_mix),
               "--mem-mix",   str(mem_mix),
               "--intensity", str(intensity),
               "--duration",  str(duration),
               "--warmup",    str(warmup),
               "--tmp-dir",   tmp_dir,
               "--seed",      str(seed),
               "--io-mode",   mode,
               "--queue-depth", str(qd),
               "--cpu-mode",  cpu_mode,
               "--mem-mode",  mem_mode]
        if file_size_bytes > 0:
            cmd += ["--file-size", str(file_size_bytes)]
        return cmd

    runs: list[tuple[float, float]] = []

    for run_idx in range(samples):
        procs: list[subprocess.Popen] = []
        for i in range(bg_procs):
            env = os.environ.copy()
            env["WORKER_ID"] = str(i)
            env["REUSE_FILE"] = "1"
            actual_bg_cpu = ["cpu_int", "cpu_fp", "cpu_hash"][i % 3] if bg_cpu_mode == "mixed" else bg_cpu_mode
            actual_bg_mem = ["mem_copy", "mem_read", "mem_write"][i % 3] if bg_mem_mode == "mixed" else bg_mem_mode
            procs.append(subprocess.Popen(
                make_cmd(bg_io_mix, bg_mem_mix, bg_intensity, _BG_SEED_BASE + i + run_idx * 100, bg_io_mode, bg_queue_depth, actual_bg_cpu, actual_bg_mem),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))
        probe_idx = 0
        for i in range(n_probe_full):
            env = os.environ.copy()
            env["WORKER_ID"] = str(bg_procs + probe_idx)
            env["REUSE_FILE"] = "1"
            procs.append(subprocess.Popen(
                make_cmd(probe_io_mix, probe_mem_mix, 1.0, _PROBE_SEED_BASE + probe_idx + run_idx * 100, probe_io_mode, probe_queue_depth, probe_cpu_mode, probe_mem_mode),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))
            probe_idx += 1
        
        if probe_frac > 0.0:
            env = os.environ.copy()
            env["WORKER_ID"] = str(bg_procs + probe_idx)
            env["REUSE_FILE"] = "1"
            procs.append(subprocess.Popen(
                make_cmd(probe_io_mix, probe_mem_mix, probe_frac, _PROBE_SEED_BASE + probe_idx + run_idx * 100, probe_io_mode, probe_queue_depth, probe_cpu_mode, probe_mem_mode),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))

        bg_tput = probe_tput = 0.0
        for idx, p in enumerate(procs):
            stdout, stderr = p.communicate()
            if stderr and stderr.strip():
                print(f"\n[worker warning]: {stderr.decode('utf-8', errors='replace').strip()}", file=sys.stderr)
            if p.returncode != 0:
                print(f"\n[probe] Worker {idx} exited non-zero (run {run_idx}) — skipping sample")
                continue
            try:
                data = json.loads(stdout.strip())
                if idx < bg_procs:
                    bg_tput += data.get("throughput", 0.0)
                else:
                    probe_tput += data.get(tput_key, 0.0)
            except (json.JSONDecodeError, KeyError):
                pass
        runs.append((bg_tput, probe_tput))

    # Sort runs by bg_tput to pick the median run
    runs.sort(key=lambda x: x[0])
    median_run = runs[len(runs) // 2]

    # Cooldown sleep to let the OS writeback queues drain and disk controller stabilize
    import time
    try:
        os.sync()
    except AttributeError:
        pass
    time.sleep(2.0)

    return median_run[0], median_run[1]


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def sweep(
    probe_type:   str,
    bg_procs:     int,
    bg_io_mix:    float,
    bg_mem_mix:   float,
    bg_intensity: float,
    duration:     int,
    warmup:       int,
    tmp_dir:      str,
    worker_bin:   str,
    drop_pct:     float = 0.05,
    max_probes:   int   = 64,
    binary_steps: int   = 5,
    bg_io_mode:   str   = "rand_write",
    probe_io_mode: str   = "rand_write",
    samples:      int   = 3,
    bg_queue_depth: int = 1,
    probe_queue_depth: int = 1,
    interference_threshold_count: int = 3,
    bg_cpu_mode:  str = "cpu_int",
    probe_cpu_mode: str = "cpu_int",
    bg_mem_mode:  str = "mem_copy",
    probe_mem_mode: str = "mem_copy",
    file_size_bytes: int = 0,
) -> dict:
    os.makedirs(tmp_dir, exist_ok=True)
    
    # Configure probe parameters
    if probe_type == "io":
        probe_io_mix = 1.0
        probe_mem_mix = 0.0
        tput_key = "io_throughput"
    elif probe_type == "ram":
        probe_io_mix = 0.0
        probe_mem_mix = 1.0
        tput_key = "mem_throughput"
    else: # cpu
        probe_io_mix = 0.0
        probe_mem_mix = 0.0
        tput_key = "cpu_throughput"
 
    kw = dict(bg_procs=bg_procs, bg_io_mix=bg_io_mix, bg_mem_mix=bg_mem_mix, bg_intensity=bg_intensity,
              probe_io_mix=probe_io_mix, probe_mem_mix=probe_mem_mix,
              duration=duration, warmup=warmup, tmp_dir=tmp_dir, worker_bin=worker_bin, tput_key=tput_key,
              bg_io_mode=bg_io_mode, probe_io_mode=probe_io_mode, samples=samples,
              bg_queue_depth=bg_queue_depth, probe_queue_depth=probe_queue_depth,
              bg_cpu_mode=bg_cpu_mode, probe_cpu_mode=probe_cpu_mode,
              bg_mem_mode=bg_mem_mode, probe_mem_mode=probe_mem_mode,
              file_size_bytes=file_size_bytes)

    # ------------------------------------------------------------------
    # Phase 0: baseline
    # ------------------------------------------------------------------
    print("--- Phase 0: Baseline (background workers only) ---")
    baseline_tput, _ = run_probe(n_probe_full=0, probe_frac=0.0, **kw)
    threshold = baseline_tput * (1.0 - drop_pct)
    print(f"  Baseline bg : {baseline_tput*_KT:,.3f} kTokens/s")
    print(f"  Threshold   : {threshold*_KT:,.3f} kTokens/s  (drop >= {drop_pct*100:.1f}%)")

    # ------------------------------------------------------------------
    # Phase 1: linear sweep
    # ------------------------------------------------------------------
    print(f"\n--- Phase 1: Linear {probe_type.upper()} sweep ---")
    print(f"  {'Probes':>7}  {'bg (kT/s)':>12}  {probe_type.upper()+' (kT/s)':>12}  {'status':}")
    print(f"  {'-------':>7}  {'---------':>12}  {'---------':>12}")

    phase1: list[dict] = []
    consecutive_interference = 0
    last_clean_n = 0            # last n_probe that did NOT cause interference

    for n_probe in range(1, max_probes + 1):
        bg_tput, probe_tput = run_probe(n_probe_full=n_probe, probe_frac=0.0, **kw)
        interfered = bg_tput < threshold
        status = "INTERFERENCE" if interfered else "ok"
        print(f"  {n_probe:>7d}  {bg_tput*_KT:>12.3f}  {probe_tput*_KT:>12.3f}  {status}")
        phase1.append(dict(n_probe=n_probe, bg_ktokens=bg_tput*_KT, probe_ktokens=probe_tput*_KT,
                           interfered=interfered))
        if interfered:
            consecutive_interference += 1
            if consecutive_interference >= interference_threshold_count:
                break
        else:
            last_clean_n = n_probe
            consecutive_interference = 0
    else:
        print(f"\n  Reached max_probes={max_probes} without reaching interference threshold.")

    # n_full = last probe count that left background throughput undisturbed.
    # 0 means even a single probe worker at full intensity causes interference.
    n_full = last_clean_n

    # ------------------------------------------------------------------
    # Phase 2: binary search on fractional last worker
    # ------------------------------------------------------------------
    print(f"\n--- Phase 2: Binary search  (locked: {n_full} × intensity=1.0) ---")
    print(f"  {'step':>4}  {'intensity':>9}  {'bg (kT/s)':>12}  {probe_type.upper()+' (kT/s)':>12}  {'status':}")
    print(f"  {'----':>4}  {'---------':>9}  {'---------':>12}  {'---------':>12}")

    low, high = 0.0, 1.0
    best_intensity  = 0.0
    best_probe_ktokens = 0.0
    phase2: list[dict] = []

    for step in range(1, binary_steps + 1):
        mid = (low + high) / 2.0
        bg_tput, probe_tput = run_probe(n_probe_full=n_full, probe_frac=mid, **kw)
        interfered = bg_tput < threshold
        status = "interferes" if interfered else "ok"
        print(f"  {step:>4d}  {mid:>9.3f}  {bg_tput*_KT:>12.3f}  {probe_tput*_KT:>12.3f}  {status}")
        phase2.append(dict(step=step, intensity=mid,
                           bg_ktokens=bg_tput*_KT, probe_ktokens=probe_tput*_KT,
                           interfered=interfered))
        if not interfered:
            best_intensity  = mid
            best_probe_ktokens = probe_tput * _KT
            low  = mid
        else:
            high = mid

    slack_ktokens = best_probe_ktokens

    print(f"\n  {probe_type.upper()} slack: {n_full} full worker(s) + 1 at intensity {best_intensity:.3f}")
    if n_full == 0 and best_intensity == 0.0:
        print(f"  (background is saturated — even a single low-intensity {probe_type.upper()} worker interferes)")

    return dict(
        type                  = f"sweep_{probe_type}_loaded",
        probe_type            = probe_type,
        # kTokens summary
        baseline_bg_ktokens   = baseline_tput * _KT,
        slack_ktokens         = slack_ktokens,
        # raw
        baseline_tput         = baseline_tput,
        interference_threshold= threshold,
        drop_pct              = drop_pct,
        bg_procs              = bg_procs,
        bg_io_mix             = bg_io_mix,
        bg_mem_mix            = bg_mem_mix,
        bg_intensity          = bg_intensity,
        slack_full            = n_full,
        slack_partial         = best_intensity,
        # probe data for plotting
        phase1_probes         = phase1,
        phase2_probes         = phase2,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep probe workers under background load.")
    parser.add_argument("--probe-type",   choices=["cpu", "io", "ram"], required=True,
                        help="Resource to probe")
    parser.add_argument("--bg-procs",     type=int,   required=True, metavar="N")
    parser.add_argument("--bg-io-mix",    type=float, default=0.3,   metavar="F")
    parser.add_argument("--bg-mem-mix",   type=float, default=0.0,   metavar="F")
    parser.add_argument("--bg-intensity", type=float, default=0.75,  metavar="F")
    parser.add_argument("--duration",     type=int,   default=60,    metavar="S")
    parser.add_argument("--warmup",       type=int,   default=15,    metavar="S",
                        help="warmup duration (seconds)")
    parser.add_argument("--drop-pct",     type=float, default=0.05,  metavar="F")
    parser.add_argument("--interference-threshold-count", type=int, default=3, metavar="N",
                        help="number of interference events required to terminate Phase 1 (default: 3)")
    parser.add_argument("--samples",      type=int,   default=3,     metavar="N",
                        help="number of samples per probe level (default: 3)")
    parser.add_argument("--max-probes",   type=int,   default=64,    metavar="N")
    parser.add_argument("--tmp-dir",      default="/tmp/slack-meter", metavar="DIR")
    parser.add_argument("--worker-bin",   default=WORKER_BIN,        metavar="PATH")
    parser.add_argument("--bg-io-mode",   default="rand_write",      help="Background IO Mode")
    parser.add_argument("--probe-io-mode",default="rand_write",      help="Probe IO Mode")
    parser.add_argument("--cpu-mode",     default="cpu_int",         help="Default CPU Mode")
    parser.add_argument("--bg-cpu-mode",  default=None,              help="Background CPU Mode (defaults to --cpu-mode)")
    parser.add_argument("--probe-cpu-mode", default=None,            help="Probe CPU Mode (defaults to --cpu-mode)")
    parser.add_argument("--mem-mode",     default="mem_copy",        help="Default Memory Mode")
    parser.add_argument("--bg-mem-mode",  default=None,              help="Background Memory Mode (defaults to --mem-mode)")
    parser.add_argument("--probe-mem-mode", default=None,            help="Probe Memory Mode (defaults to --mem-mode)")
    parser.add_argument("--output",       default=None,              metavar="FILE")
    parser.add_argument("--plot",         default=None,              metavar="FILE")
    parser.add_argument("--queue-depth",  type=int,   default=1,     metavar="QD",
                        help="default queue depth/concurrency per worker for io_uring (default: 1)")
    parser.add_argument("--bg-queue-depth", type=int, default=None,  metavar="QD",
                        help="queue depth/concurrency per background worker (defaults to --queue-depth)")
    parser.add_argument("--probe-queue-depth", type=int, default=None, metavar="QD",
                        help="queue depth/concurrency per probe worker (defaults to --queue-depth)")
    parser.add_argument("--file-size-mib", type=int, default=256, metavar="MiB",
                        help="Per-worker scratch file size in MiB (default: 256; try 4096 to exceed SSD DRAM cache)")
    args = parser.parse_args()

    if not os.path.exists(args.worker_bin):
        print(f"[probe] ERROR: worker binary not found at {args.worker_bin}")
        sys.exit(1)

    print("=" * 60)
    print(f"  {args.probe_type.upper()} Sweep Under Background Load")
    print("=" * 60)
    print(f"  Background : {args.bg_procs} workers  "
          f"io={args.bg_io_mix}  mem={args.bg_mem_mix}  intensity={args.bg_intensity}")
    print(f"  Probe dur  : {args.duration}s   drop_pct={args.drop_pct*100:.0f}%   file_size={args.file_size_mib} MiB")
    print(f"  Tmp dir    : {args.tmp_dir}")
    print("=" * 60)

    bg_qd = args.bg_queue_depth if args.bg_queue_depth is not None else args.queue_depth
    probe_qd = args.probe_queue_depth if args.probe_queue_depth is not None else args.queue_depth
    bg_cpu = args.bg_cpu_mode if args.bg_cpu_mode is not None else args.cpu_mode
    probe_cpu = args.probe_cpu_mode if args.probe_cpu_mode is not None else args.cpu_mode
    bg_mem = args.bg_mem_mode if args.bg_mem_mode is not None else args.mem_mode
    probe_mem = args.probe_mem_mode if args.probe_mem_mode is not None else args.mem_mode

    result = sweep(
        probe_type   = args.probe_type,
        bg_procs     = args.bg_procs,
        bg_io_mix    = args.bg_io_mix,
        bg_mem_mix   = args.bg_mem_mix,
        bg_intensity = args.bg_intensity,
        duration     = args.duration,
        warmup       = args.warmup,
        tmp_dir      = args.tmp_dir,
        worker_bin   = args.worker_bin,
        drop_pct     = args.drop_pct,
        max_probes   = args.max_probes,
        bg_io_mode   = args.bg_io_mode,
        probe_io_mode= args.probe_io_mode,
        samples      = args.samples,
        bg_queue_depth= bg_qd,
        probe_queue_depth= probe_qd,
        interference_threshold_count= args.interference_threshold_count,
        bg_cpu_mode  = bg_cpu,
        probe_cpu_mode = probe_cpu,
        bg_mem_mode  = bg_mem,
        probe_mem_mode = probe_mem,
        file_size_bytes = args.file_size_mib * 1024 * 1024,
    )

    print("\n" + "=" * 60)
    print("  Result")
    print("=" * 60)
    print(f"  Baseline bg throughput : {result['baseline_bg_ktokens']:.3f} kTokens/s")
    print(f"  {args.probe_type.upper()} slack              : {result['slack_full']} full worker(s) "
          f"+ 1 at intensity {result['slack_partial']:.3f}")
    print(f"  {args.probe_type.upper()} slack throughput   : {result['slack_ktokens']:.3f} kTokens/s")
    print("=" * 60)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        export = {k: v for k, v in result.items()
                  if k not in ("baseline_tput", "interference_threshold")}
        with open(out, "w") as f:
            json.dump(export, f, indent=2)
        print(f"\nResult written to {args.output}")

    if args.plot:
        _plot_slack_result(result, Path(args.plot))


if __name__ == "__main__":
    main()
