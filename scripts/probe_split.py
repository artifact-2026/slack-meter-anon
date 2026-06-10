#!/usr/bin/env python3
"""
probe_split.py
==============
Sweeps synthetic probe workers (CPU, I/O, or RAM) on top of a running
split_job --phase split workload, stopping when split_job throughput drops by
DROP_PCT -- the same Phase 0 / 1 / 2 methodology as probe_rocksdb.py, but with
split_job as the background instead of ycsb_test.

This measures the resource *slack* available while the Mycelium "split rows
into column groups" transformation runs: a high slack means the transform is
cheap in that resource; a low slack means it is expensive.

Prerequisites
-------------
  1. Build split_job from the htap project:
       cmake -B build && cmake --build build --target split_job
  2. Pre-generate data files with split_job --phase generate:
       ./split_job --phase generate -P workloads/test_basic.spec \
           --data_dir /tmp/split_data
     Generate enough data so that the split phase takes a meaningful amount
     of time (tens of seconds at minimum).
  3. Build the slack-meter worker binary:
       cmake -B build && cmake --build build   (from slack-meter repo root)

Usage
-----
  python3 scripts/probe_split.py \\
      --probe-type     io \\
      --split-binary   /path/to/split_job \\
      --data-dir       /tmp/split_data \\
      --worker-bin     build/worker \\
      --output         results/probe_split_io.json \\
      --plot           results/probe_split_io.png

Output JSON schema matches probe_rocksdb.py for compatibility with plot.py.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent.resolve()
WORKER_BIN = str(REPO_ROOT / "build" / "worker")

_PROBE_SEED_BASE = 2000
_KT = 1e-3   # rows/s → kRows (same scale as kTokens for plot compatibility)


# ---------------------------------------------------------------------------
# Plotting (delegated to plot.py — same as probe_rocksdb.py)
# ---------------------------------------------------------------------------

def _plot_slack_result(result: dict, out_path: Path) -> None:
    try:
        from plot import plot_slack_result
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from plot import plot_slack_result  # type: ignore[no-redef]
    plot_slack_result(result, out_path)


# ---------------------------------------------------------------------------
# Parse split_job throughput output
#
# split_job prints (when --runtime > 0):
#   throughput mean: X  stddev: Y
# which is the same format ycsb_test uses, so we reuse the same regex.
# ---------------------------------------------------------------------------

def _parse_throughput_mean(stdout: str) -> float:
    """Return the mean rows/s from split_job stdout, or raise ValueError."""
    m = re.search(
        r"throughput mean:\s*([\d.eE+\-]+)\s+stddev:\s*([\d.eE+\-]+)", stdout
    )
    if not m:
        raise ValueError(
            "Could not find 'throughput mean:' in split_job output.\n"
            f"Last 500 chars:\n{stdout[-500:]}"
        )
    return float(m.group(1))


# ---------------------------------------------------------------------------
# Probe-worker command builder (mirrors probe_rocksdb.py)
# ---------------------------------------------------------------------------

def _make_probe_cmd(
    worker_bin: str,
    io_mix: float,
    mem_mix: float,
    intensity: float,
    seed: int,
    duration: int,
    warmup: int,
    tmp_dir: str,
    io_mode: str,
    queue_depth: int,
    cpu_mode: str,
    mem_mode: str,
    file_size_bytes: int,
) -> list[str]:
    cmd = [
        worker_bin,
        "--io-mix",      str(io_mix),
        "--mem-mix",     str(mem_mix),
        "--intensity",   str(intensity),
        "--duration",    str(duration),
        "--warmup",      str(warmup),
        "--tmp-dir",     tmp_dir,
        "--seed",        str(seed),
        "--io-mode",     io_mode,
        "--queue-depth", str(queue_depth),
        "--cpu-mode",    cpu_mode,
        "--mem-mode",    mem_mode,
    ]
    if file_size_bytes > 0:
        cmd += ["--file-size", str(file_size_bytes)]
    return cmd


# ---------------------------------------------------------------------------
# Core measurement: split_job + probe workers run concurrently
# ---------------------------------------------------------------------------

def run_measurement(
    *,
    # split_job background
    split_binary:    str,
    data_dir:        str,
    out_dir:         str,
    batch:           int,
    groups:          int,
    tmp_dir:         str,
    # Probe workers
    n_probe_full:    int,
    probe_frac:      float,
    probe_io_mix:    float,
    probe_mem_mix:   float,
    probe_io_mode:   str,
    probe_cpu_mode:  str,
    probe_mem_mode:  str,
    probe_queue_depth: int,
    tput_key:        str,
    worker_bin:      str,
    file_size_bytes: int,
    duration_s:      int = 600,
    samples:         int = 1,
    n_split_procs:   int = 1,
) -> tuple[float, float]:
    """Run split_job + probe workers concurrently; return (split_rows_s, probe_tput).

    n_split_procs concurrent split_job processes are launched (each with its own
    --out_dir subdirectory); their throughputs are summed so the aggregate matches
    the target write rate.

    Both split_job (via --duration) and probe workers run for duration_s seconds
    and exit naturally — no SIGTERM.

    When samples > 1 the returned values are the mean across all samples.
    """

    sample_split: list[float] = []
    sample_probe: list[float] = []

    for sample_idx in range(samples):
        # Build one command per split_job process, each with its own out_dir.
        split_cmds = []
        for proc_idx in range(n_split_procs):
            proc_out_dir = out_dir if n_split_procs == 1 else f"{out_dir}/proc{proc_idx}"
            split_cmds.append([
                split_binary,
                "--phase",    "split",
                "--data_dir", data_dir,
                "--out_dir",  proc_out_dir,
                "--batch",    str(batch),
                "--groups",   str(groups),
                "--duration", str(duration_s),
            ])

        # Start probe workers first so they are already running and consuming
        # resources when split_job begins.
        probe_procs: list[subprocess.Popen] = []
        for probe_idx in range(n_probe_full):
            env = os.environ.copy()
            env["WORKER_ID"] = str(probe_idx)
            env["REUSE_FILE"] = "1"
            probe_procs.append(subprocess.Popen(
                _make_probe_cmd(
                    worker_bin, probe_io_mix, probe_mem_mix, 1.0,
                    _PROBE_SEED_BASE + probe_idx + sample_idx * 100,
                    duration_s, 0, tmp_dir,
                    probe_io_mode, probe_queue_depth, probe_cpu_mode, probe_mem_mode,
                    file_size_bytes,
                ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            ))

        if probe_frac > 0.0:
            env = os.environ.copy()
            env["WORKER_ID"] = str(n_probe_full)
            env["REUSE_FILE"] = "1"
            probe_procs.append(subprocess.Popen(
                _make_probe_cmd(
                    worker_bin, probe_io_mix, probe_mem_mix, probe_frac,
                    _PROBE_SEED_BASE + n_probe_full + sample_idx * 100,
                    duration_s, 0, tmp_dir,
                    probe_io_mode, probe_queue_depth, probe_cpu_mode, probe_mem_mode,
                    file_size_bytes,
                ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            ))

        # Now start all split_job processes concurrently.
        split_procs = [
            subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for cmd in split_cmds
        ]

        # Wait for all split_job processes and sum their throughputs.
        split_rows_s_total = 0.0
        any_nan = False
        for proc in split_procs:
            stdout, stderr = proc.communicate()
            combined = stdout + stderr
            try:
                split_rows_s_total += _parse_throughput_mean(combined)
            except ValueError as e:
                print(f"\n  [probe_split] WARNING: {e}")
                any_nan = True

        split_rows_s = float("nan") if any_nan and split_rows_s_total == 0.0 else split_rows_s_total

        # Wait for probe workers to finish (they also run for duration_s seconds
        # and exit naturally — no SIGTERM needed).

        # Collect probe worker output (best-effort: workers may not print JSON
        # when terminated mid-run, but probe_tput is informational only —
        # interference detection uses split_rows_s exclusively).
        probe_tput = 0.0
        for idx, p in enumerate(probe_procs):
            try:
                stdout, stderr = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                stdout, stderr = p.communicate()
            if stderr and stderr.strip():
                print(f"\n  [worker warning]: {stderr.strip()}", file=sys.stderr)
            try:
                data = json.loads(stdout.strip())
                probe_tput += data.get(tput_key, 0.0)
            except (json.JSONDecodeError, KeyError):
                pass

        sample_split.append(split_rows_s)
        sample_probe.append(probe_tput)

        # Cooldown between samples.
        if sample_idx < samples - 1:
            try:
                os.sync()
            except AttributeError:
                pass
            time.sleep(2.0)

    # Filter NaN before averaging.
    valid_split = [x for x in sample_split if not math.isnan(x)]
    avg_split   = statistics.mean(valid_split) if valid_split else float("nan")
    avg_probe   = statistics.mean(sample_probe) if sample_probe else 0.0

    try:
        os.sync()
    except AttributeError:
        pass
    time.sleep(2.0)

    return avg_split, avg_probe


# ---------------------------------------------------------------------------
# Phase 2: binary search
# ---------------------------------------------------------------------------

def _run_phase2(
    n_full: int,
    probe_type: str,
    threshold: float,
    binary_steps: int,
    seed_probe_tput: float,
    measurement_kw: dict,
) -> tuple[list[dict], float, float]:
    print(f"\n--- Phase 2: Binary search  (locked: {n_full} × intensity=1.0) ---")
    print(f"  {'step':>4}  {'intensity':>9}  {'split (rows/s)':>16}  {probe_type.upper()+' (T/s)':>14}  {'status'}")
    print(f"  {'----':>4}  {'---------':>9}  {'---------------':>16}  {'------------':>14}")

    low, high      = 0.0, 1.0
    best_intensity = 0.0
    best_probe_kt  = seed_probe_tput * _KT
    steps: list[dict] = []

    for step in range(1, binary_steps + 1):
        mid = (low + high) / 2.0
        split_rows_s, probe_tput = run_measurement(
            n_probe_full=n_full, probe_frac=mid, **measurement_kw
        )
        interfered = (not math.isnan(split_rows_s)) and (split_rows_s < threshold)
        status = "interferes" if interfered else "ok"
        print(f"  {step:>4d}  {mid:>9.3f}  {split_rows_s:>16.0f}  {probe_tput:>14.0f}  {status}")
        steps.append(dict(
            step=step, intensity=mid,
            split_rows_s=split_rows_s,
            probe_ktokens=probe_tput * _KT,
            interfered=interfered,
        ))
        if not interfered:
            best_intensity = mid
            best_probe_kt  = probe_tput * _KT
            low  = mid
        else:
            high = mid

    return steps, best_intensity, best_probe_kt


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def sweep(
    probe_type:      str,
    # split_job background params
    split_binary:    str,
    data_dir:        str,
    out_dir:         str,
    batch:           int,
    groups:          int,
    # Probe workers
    worker_bin:      str,
    probe_io_mode:   str   = "rand_write",
    probe_cpu_mode:  str   = "cpu_int",
    probe_mem_mode:  str   = "mem_copy",
    probe_queue_depth: int = 1,
    file_size_bytes: int   = 0,
    # Duration (applied to both split_job and probe workers)
    duration_s:      int   = 600,
    # Sweep control
    drop_pct:        float = 0.05,
    max_probes:      int   = 64,
    binary_steps:    int   = 5,
    samples:         int   = 1,
    baseline_samples: int  = 1,
    interference_threshold_count: int = 3,
    # Concurrent split_job processes
    n_split_procs:   int   = 1,
    # Scratch
    tmp_dir:         str   = "/holly/slack-meter-probe-split",
) -> dict:
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    if probe_type == "io":
        probe_io_mix  = 1.0
        probe_mem_mix = 0.0
        tput_key      = "io_throughput"
    elif probe_type == "ram":
        probe_io_mix  = 0.0
        probe_mem_mix = 1.0
        tput_key      = "mem_throughput"
    else:  # cpu
        probe_io_mix  = 0.0
        probe_mem_mix = 0.0
        tput_key      = "cpu_throughput"

    meas_kw = dict(
        split_binary=split_binary, data_dir=data_dir, out_dir=out_dir,
        batch=batch, groups=groups,
        tmp_dir=tmp_dir,
        probe_io_mix=probe_io_mix, probe_mem_mix=probe_mem_mix,
        probe_io_mode=probe_io_mode, probe_cpu_mode=probe_cpu_mode,
        probe_mem_mode=probe_mem_mode, probe_queue_depth=probe_queue_depth,
        tput_key=tput_key, worker_bin=worker_bin,
        file_size_bytes=file_size_bytes,
        duration_s=duration_s,
        samples=samples,
        n_split_procs=n_split_procs,
    )

    # ------------------------------------------------------------------
    # Phase 0: baseline (split_job alone, no probe workers)
    # ------------------------------------------------------------------
    print("--- Phase 0: Baseline (split_job alone) ---")

    baseline_runs: list[float] = []
    n_baseline = max(1, baseline_samples)
    for i in range(n_baseline):
        bt, _ = run_measurement(n_probe_full=0, probe_frac=0.0, **meas_kw)
        baseline_runs.append(bt)
        if n_baseline > 1:
            print(f"  baseline sample {i+1}/{n_baseline}: {bt:,.0f} rows/s")

    baseline_tput = statistics.mean([x for x in baseline_runs if not math.isnan(x)]
                                    or [float("nan")])
    threshold     = baseline_tput * (1.0 - drop_pct)

    if n_baseline > 1:
        print(f"  Baseline split : {baseline_tput:,.0f} rows/s (mean of {n_baseline})")
    else:
        print(f"  Baseline split : {baseline_tput:,.0f} rows/s")
    print(f"  Threshold      : {threshold:,.0f} rows/s  (drop >= {drop_pct*100:.1f}%)")

    # ------------------------------------------------------------------
    # Phase 1: linear sweep
    # ------------------------------------------------------------------
    print(f"\n--- Phase 1: Linear {probe_type.upper()} sweep ---")
    print(f"  {'Probes':>7}  {'split (rows/s)':>16}  {probe_type.upper()+' (T/s)':>14}  {'status'}")
    print(f"  {'-------':>7}  {'---------------':>16}  {'------------':>14}")

    phase1: list[dict] = []
    phase2: list[dict] = []
    consecutive_interference = 0
    last_clean_n             = 0
    last_clean_probe_kt      = 0.0
    best_intensity           = 0.0
    best_probe_kt            = 0.0

    n_probe = 1
    while n_probe <= max_probes:
        split_rows_s, probe_tput = run_measurement(
            n_probe_full=n_probe, probe_frac=0.0, **meas_kw
        )
        interfered = (not math.isnan(split_rows_s)) and (split_rows_s < threshold)
        status = "INTERFERENCE" if interfered else "ok"
        print(f"  {n_probe:>7d}  {split_rows_s:>16.0f}  {probe_tput:>14.0f}  {status}")
        phase1.append(dict(
            n_probe=n_probe,
            split_rows_s=split_rows_s,
            probe_ktokens=probe_tput * _KT,
            interfered=interfered,
        ))

        if interfered:
            consecutive_interference += 1
            if consecutive_interference >= interference_threshold_count:
                n_full = last_clean_n
                steps, best_intensity, best_probe_kt = _run_phase2(
                    n_full, probe_type, threshold, binary_steps,
                    last_clean_probe_kt, meas_kw,
                )
                phase2.extend(steps)

                if not any(s["interfered"] for s in steps):
                    verified_n = n_full + 1
                    print(f"\n  Phase 2 found no interference — probe {verified_n} verified "
                          f"clean; resuming Phase 1 from probe {verified_n + 1}")
                    phase1.append(dict(n_probe=verified_n, split_rows_s=None,
                                       probe_ktokens=best_probe_kt,
                                       interfered=False, verified_via_phase2=True))
                    last_clean_n        = verified_n
                    last_clean_probe_kt = best_probe_kt
                    consecutive_interference = 0
                    n_probe = verified_n + 1
                    print(f"\n--- Phase 1 (resumed from probe {n_probe}): "
                          f"Linear {probe_type.upper()} sweep ---")
                    print(f"  {'Probes':>7}  {'split (rows/s)':>16}  {probe_type.upper()+' (T/s)':>14}  {'status'}")
                    print(f"  {'-------':>7}  {'---------------':>16}  {'------------':>14}")
                    continue
                else:
                    break
        else:
            last_clean_n        = n_probe
            last_clean_probe_kt = probe_tput * _KT
            consecutive_interference = 0

        n_probe += 1
    else:
        print(f"\n  Reached max_probes={max_probes} without hitting interference threshold.")
        n_full = last_clean_n
        steps, best_intensity, best_probe_kt = _run_phase2(
            n_full, probe_type, threshold, binary_steps, last_clean_probe_kt, meas_kw,
        )
        phase2.extend(steps)

    print(f"\n  {probe_type.upper()} slack: {last_clean_n} full worker(s) + 1 at intensity {best_intensity:.3f}")
    if last_clean_n == 0 and best_intensity == 0.0:
        print(f"  (split_job is saturated — even a single low-intensity {probe_type.upper()} worker interferes)")

    return dict(
        type                  = "sweep_split_job",
        probe_type            = probe_type,
        baseline_split_rows_s = baseline_tput,
        baseline_ktokens      = baseline_tput * _KT,
        slack_ktokens         = best_probe_kt,
        baseline_samples      = n_baseline,
        baseline_runs         = baseline_runs,
        interference_threshold= threshold,
        drop_pct              = drop_pct,
        slack_full            = last_clean_n,
        slack_partial         = best_intensity,
        split_binary          = split_binary,
        data_dir              = data_dir,
        split_batch           = batch,
        split_groups          = groups,
        n_split_procs         = n_split_procs,
        phase1_probes         = phase1,
        phase2_probes         = phase2,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep probe workers under split_job --phase split workload.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--probe-type",   choices=["cpu", "io", "ram"], required=True)
    # split_job params
    parser.add_argument("--split-binary", required=True, metavar="PATH",
                        help="Path to split_job binary")
    parser.add_argument("--data-dir",     required=True, metavar="DIR",
                        help="Directory containing pre-generated data files "
                             "(from split_job --phase generate)")
    parser.add_argument("--out-dir",      default="/holly/split_job_out", metavar="DIR",
                        help="Directory split_job writes transformed output to")
    parser.add_argument("--batch",        type=int, default=4,  metavar="N",
                        help="split_job --batch")
    parser.add_argument("--groups",       type=int, default=2,  metavar="N",
                        help="split_job --groups")
    # Probe worker params
    parser.add_argument("--worker-bin",   default=WORKER_BIN, metavar="PATH")
    parser.add_argument("--probe-io-mode",  default="rand_write")
    parser.add_argument("--probe-cpu-mode", default="cpu_int")
    parser.add_argument("--probe-mem-mode", default="mem_copy")
    parser.add_argument("--queue-depth",  type=int, default=1, metavar="QD")
    parser.add_argument("--file-size-mib",type=int, default=256, metavar="MiB")
    # Duration
    parser.add_argument("--duration",     type=int,   default=600,  metavar="S",
                        help="Seconds both split_job and probe workers run (default: 600)")
    # Sweep control
    parser.add_argument("--drop-pct",     type=float, default=0.05, metavar="F")
    parser.add_argument("--max-probes",   type=int,   default=64,   metavar="N")
    parser.add_argument("--binary-steps", type=int,   default=5,    metavar="N")
    parser.add_argument("--samples",      type=int,   default=1,    metavar="N")
    parser.add_argument("--baseline-samples", type=int, default=1,  metavar="N")
    parser.add_argument("--interference-threshold-count", type=int, default=3, metavar="N")
    parser.add_argument("--split-processes", type=int, default=4, metavar="N",
                        help="Number of concurrent split_job processes (summed throughput). "
                             "Use 4 to match ~34k write ops/s from RocksDB. (default: 4)")
    # Output
    parser.add_argument("--tmp-dir",      default="/holly/slack-meter-probe-split", metavar="DIR")
    parser.add_argument("--output",       default=None, metavar="FILE")
    parser.add_argument("--plot",         default=None, metavar="FILE")
    args = parser.parse_args()

    if not os.path.exists(args.split_binary):
        print(f"[probe_split] ERROR: split_job binary not found at {args.split_binary}")
        sys.exit(1)
    if not os.path.isdir(args.data_dir):
        print(f"[probe_split] ERROR: data-dir not found: {args.data_dir}")
        sys.exit(1)
    if not os.path.exists(args.worker_bin):
        print(f"[probe_split] ERROR: worker binary not found at {args.worker_bin}")
        sys.exit(1)

    print("=" * 65)
    print(f"  {args.probe_type.upper()} Slack Probe — split_job background")
    print("=" * 65)
    print(f"  split_job    : {args.split_binary}")
    print(f"  data-dir     : {args.data_dir}")
    print(f"  batch/groups : {args.batch} / {args.groups}")
    print(f"  split procs  : {args.split_processes} (aggregate throughput target)")
    print(f"  duration     : {args.duration}s")
    print(f"  worker       : {args.worker_bin}")
    print(f"  drop_pct     : {args.drop_pct*100:.0f}%")
    print("=" * 65)

    result = sweep(
        probe_type        = args.probe_type,
        split_binary      = args.split_binary,
        data_dir          = args.data_dir,
        out_dir           = args.out_dir,
        batch             = args.batch,
        groups            = args.groups,
        worker_bin        = args.worker_bin,
        probe_io_mode     = args.probe_io_mode,
        probe_cpu_mode    = args.probe_cpu_mode,
        probe_mem_mode    = args.probe_mem_mode,
        probe_queue_depth = args.queue_depth,
        file_size_bytes   = args.file_size_mib * 1024 * 1024,
        duration_s        = args.duration,
        drop_pct          = args.drop_pct,
        max_probes        = args.max_probes,
        binary_steps      = args.binary_steps,
        samples           = args.samples,
        baseline_samples  = args.baseline_samples,
        interference_threshold_count = args.interference_threshold_count,
        n_split_procs     = args.split_processes,
        tmp_dir           = args.tmp_dir,
    )

    print("\n" + "=" * 65)
    print("  Result")
    print("=" * 65)
    print(f"  Baseline split throughput : {result['baseline_split_rows_s']:,.0f} rows/s")
    print(f"  {args.probe_type.upper()} slack              : {result['slack_full']} full worker(s) "
          f"+ 1 at intensity {result['slack_partial']:.3f}")
    print(f"  {args.probe_type.upper()} slack throughput   : {result['slack_ktokens']:.3f} kT/s")
    print("=" * 65)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nResult written to {args.output}")

    if args.plot:
        _plot_slack_result(result, Path(args.plot))


if __name__ == "__main__":
    main()
