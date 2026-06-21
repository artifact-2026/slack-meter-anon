#!/usr/bin/env python3
"""
probe_rocksdb.py
================
Sweeps synthetic probe workers (CPU, I/O, or RAM) on top of a live RocksDB
workload, stopping when RocksDB throughput drops by DROP_PCT — the same
Phase 0 / 1 / 2 methodology as probe.py, but with ycsb_test as the background
instead of synthetic workers.

Architecture
------------
For each probe level, ycsb_test and N synthetic probe workers run concurrently
for a fixed duration (--runtime seconds).  When ycsb_test finishes it prints:
  throughput mean: X  stddev: Y
That is parsed as the RocksDB ops/s for this probe level.  Probe workers print
JSON to stdout; their resource-specific throughput field is also collected.

This design means every measurement is self-contained: ycsb_test starts fresh
(from the same DB state) for each probe level, so compaction debt does not
accumulate across measurements.

Modes
-----
Continuous mode (default)
  The RocksDB DB directory is NOT wiped between probe levels.  Faster, but
  later probe levels may see a slightly different LSM state than earlier ones.
  Good for exploratory runs.

Reset mode (--reset-db-per-point)
  The DB is wiped and reloaded before every single measurement (Phase 0
  baseline AND every Phase 1 / 2 probe level).  This guarantees identical LSM
  state across all comparisons.  Recommended for final paper results.

Phase flow
----------
Phase 0  Baseline: run ycsb_test alone (no probe workers). Record mean ops/s.
          threshold = baseline_ops_s * (1 - --drop-pct)

Phase 1  Linear sweep: start 1, 2, 3 … probe workers alongside ycsb_test.
          Stop when ycsb_test ops/s < threshold for
          --interference-threshold-count consecutive steps.
          If --step > 1, backtrack to step=1 on first interference.

Phase 2  Binary search: with (n_full-1) probe workers locked at intensity=1.0,
          find the highest fractional intensity on the last one that keeps
          ycsb_test ops/s ≥ threshold.

Output JSON schema matches probe.py for compatibility with existing plot.py.
Additional fields specific to this script are prefixed with 'rocksdb_'.

Usage
-----
Basic (supply saturation JSON from saturate_rocksdb.py):
  python3 probe_rocksdb.py \\
      --probe-type io \\
      --saturation-json results/saturation.json \\
      --worker-bin    build/worker \\
      --output        results/probe_rocksdb_io.json \\
      --plot          results/probe_rocksdb_io.png

Override DB parameters manually (without a saturation JSON):
  python3 probe_rocksdb.py \\
      --probe-type io \\
      --bg-binary     /path/to/ycsb_test \\
      --bg-dbpath     /holly/rocksdb_bench \\
      --bg-workload-spec /path/to/workloada.spec \\
      --bg-threads    16 \\
      --worker-bin    build/worker \\
      --output        results/probe_rocksdb_io.json

Reset-mode (fresh DB per measurement, page cache dropped):
  ... --reset-db-per-point --drop-caches
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent.resolve()
WORKER_BIN = str(REPO_ROOT / "build" / "worker")

_PROBE_SEED_BASE = 2000
_KT = 1e-3   # ops/s → kTokens


# ---------------------------------------------------------------------------
# Plotting (delegated to plot.py — same as probe.py)
# ---------------------------------------------------------------------------

def _plot_slack_result(result: dict, out_path: Path) -> None:
    try:
        from plot import plot_slack_result
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from plot import plot_slack_result  # type: ignore[no-redef]
    plot_slack_result(result, out_path)


# ---------------------------------------------------------------------------
# Spec / DB helpers  (mirrors saturate_rocksdb.py — kept self-contained)
# ---------------------------------------------------------------------------

def _write_temp_spec(base_spec_path: str, tmp_dir: str, **overrides) -> str:
    """Return path to a temp copy of base_spec with key=value overrides.

    We filter out pre-existing definitions of overridden keys to ensure
    our overrides take effect regardless of parser precedence.
    """
    os.makedirs(tmp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".spec", dir=tmp_dir, prefix="probe_rdb_")
    
    filtered_lines = []
    with open(base_spec_path) as f:
        for line in f:
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in overrides:
                    continue
            filtered_lines.append(line)

    with os.fdopen(fd, "w") as f:
        f.writelines(filtered_lines)
        f.write("\n# --- probe_rocksdb.py overrides ---\n")
        for k, v in overrides.items():
            f.write(f"{k}={v}\n")
    return path


def _load_db(
    binary: str,
    dbpath: str,
    base_spec: str,
    *,
    load_threads: int,
    rocksdb_parallelism: int,
    record_count: int,
    tmp_dir: str,
) -> None:
    """Wipe dbpath (if it exists) and load a fresh database."""
    if os.path.exists(dbpath):
        print(f"  [reset] Wiping {dbpath} …", flush=True)
        shutil.rmtree(dbpath)

    spec = _write_temp_spec(
        base_spec, tmp_dir,
        dbpath=dbpath,              # override any dbpath in the base spec
        recordcount=record_count,
        operationcount=record_count,
        rocksdb_parallelism=rocksdb_parallelism,
        xputwindow=0,   # no CSV during load
    )
    cmd = [
        binary,
        "-db",        "baseline",
        "-dbpath",    dbpath,
        "-P",         spec,
        "-bootstrap", "true",
        "-threads",   str(load_threads),
        "-load",      "true",
        "-run",       "false",
        "-throughput","false",
        "-runtime",   "0",
        "-levels",    "7",
        "-table",     "baseline",
    ]
    print(f"  [load] Loading {record_count:,} records …", flush=True)
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        m = re.search(r"loading records:\s*\d+\s+use time:\s*[\d.]+\s*s\s+IOPS:\s*([\d.]+)", out)
        print(f"  [load] Done — {float(m.group(1)):,.0f} load IOPS" if m else "  [load] Done.")
    except subprocess.CalledProcessError as e:
        print(f"  [load] ERROR: load phase failed (exit {e.returncode})")
        print(e.output[-2000:])
        raise
    finally:
        try:
            os.unlink(spec)
        except OSError:
            pass


def _drop_page_cache() -> None:
    """Drop OS page cache (requires passwordless sudo); non-fatal on failure."""
    print("  [cache] Dropping OS page cache …", end=" ", flush=True)
    try:
        subprocess.run(["sync"], check=True, capture_output=True)
        # Try writing directly first (e.g. if running as root in Docker container)
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3")
            print("done.")
            return
        except (PermissionError, FileNotFoundError):
            pass

        r = subprocess.run(
            ["sudo", "tee", "/proc/sys/vm/drop_caches"],
            input="3", text=True, capture_output=True,
        )
        print("done." if r.returncode == 0 else "WARNING: sudo tee failed — reads may hit RAM.")
    except Exception as e:
        print(f"WARNING: skipped ({e}).")


def _parse_throughput_mean(stdout: str) -> tuple[float, float]:
    """Parse 'throughput mean:X  stddev: Y' from ycsb_test stdout."""
    m = re.search(
        r"throughput mean:\s*([\d.eE+\-]+)\s+stddev:\s*([\d.eE+\-]+)", stdout
    )
    if not m:
        raise ValueError(
            "Could not find 'throughput mean:' in ycsb_test output.\n"
            f"Last 500 chars:\n{stdout[-500:]}"
        )
    return float(m.group(1)), float(m.group(2))


# ---------------------------------------------------------------------------
# Core measurement: ycsb_test + probe workers run concurrently
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
        "--io-mix",    str(io_mix),
        "--mem-mix",   str(mem_mix),
        "--intensity", str(intensity),
        "--duration",  str(duration),
        "--warmup",    str(warmup),
        "--tmp-dir",   tmp_dir,
        "--seed",      str(seed),
        "--io-mode",   io_mode,
        "--queue-depth", str(queue_depth),
        "--cpu-mode",  cpu_mode,
        "--mem-mode",  mem_mode,
    ]
    if file_size_bytes > 0:
        cmd += ["--file-size", str(file_size_bytes)]
    return cmd


def run_measurement(
    *,
    # RocksDB background
    bg_binary:          str,
    bg_dbpath:          str,
    bg_base_spec:       str,
    bg_threads:         int,
    bg_rocksdb_parallelism: int,
    bg_record_count:    int,
    bg_operation_count: int,
    bg_runtime_s:       int,
    bg_skip_s:          int,
    bg_xputfile:        str | None,
    tmp_dir:            str,
    # Probe workers
    n_probe_full:       int,
    probe_frac:         float,
    probe_io_mix:       float,
    probe_mem_mix:      float,
    probe_io_mode:      str,
    probe_cpu_mode:     str,
    probe_mem_mode:     str,
    probe_queue_depth:  int,
    tput_key:           str,
    worker_bin:         str,
    file_size_bytes:    int,
    samples:            int = 1,
    # Reset / cache
    reset_db_per_point: bool = False,
    drop_caches:        bool = False,
    load_threads:       int  = 8,
) -> tuple[float, float]:
    """Run ycsb_test + probe workers for bg_runtime_s seconds; return (rocksdb_ops_s, probe_tput).

    The probe workers run for the same duration as ycsb_test so both are active
    throughout the measurement window.  ycsb_test uses -skip bg_skip_s to
    compute its mean over [skip, runtime]; probe workers use --warmup bg_skip_s
    so both discard the same early window.

    When samples > 1, the returned values are the mean across all samples.
    """
    sample_rdb: list[float] = []
    sample_probe: list[float] = []

    for sample_idx in range(samples):
        if reset_db_per_point:
            _load_db(
                bg_binary, bg_dbpath, bg_base_spec,
                load_threads=load_threads,
                rocksdb_parallelism=bg_rocksdb_parallelism,
                record_count=bg_record_count,
                tmp_dir=tmp_dir,
            )
        if drop_caches:
            _drop_page_cache()

        # Build temp spec for this run.
        # dbpath is appended as an override so it wins over any dbpath in the
        # base spec (ycsb_test processes -P before later CLI flags).
        spec = _write_temp_spec(
            bg_base_spec, tmp_dir,
            dbpath=bg_dbpath,
            recordcount=bg_record_count,
            operationcount=bg_operation_count,
            rocksdb_parallelism=bg_rocksdb_parallelism,
            skip=bg_skip_s,
            xputwindow=10 if bg_xputfile else 0,
            **({"xputfile": bg_xputfile} if bg_xputfile else {}),
        )

        ycsb_cmd = [
            bg_binary,
            "-db",        "baseline",
            "-dbpath",    bg_dbpath,
            "-P",         spec,
            "-bootstrap", "false",
            "-threads",   str(bg_threads),
            "-load",      "false",
            "-run",       "false",
            "-throughput","true",
            "-runtime",   str(bg_runtime_s),
            "-skip",      str(bg_skip_s),
            "-levels",    "7",
            "-table",     "baseline",
        ]

        # Start ycsb_test
        ycsb_proc = subprocess.Popen(
            ycsb_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        # Start probe workers concurrently (same total duration as ycsb_test)
        probe_procs: list[subprocess.Popen] = []
        probe_idx = 0
        for i in range(n_probe_full):
            env = os.environ.copy()
            env["WORKER_ID"] = str(probe_idx)
            env["REUSE_FILE"] = "1"
            probe_procs.append(subprocess.Popen(
                _make_probe_cmd(
                    worker_bin, probe_io_mix, probe_mem_mix, 1.0,
                    _PROBE_SEED_BASE + probe_idx + sample_idx * 100,
                    bg_runtime_s, bg_skip_s, tmp_dir,
                    probe_io_mode, probe_queue_depth, probe_cpu_mode, probe_mem_mode,
                    file_size_bytes,
                ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            ))
            probe_idx += 1

        if probe_frac > 0.0:
            env = os.environ.copy()
            env["WORKER_ID"] = str(probe_idx)
            env["REUSE_FILE"] = "1"
            probe_procs.append(subprocess.Popen(
                _make_probe_cmd(
                    worker_bin, probe_io_mix, probe_mem_mix, probe_frac,
                    _PROBE_SEED_BASE + probe_idx + sample_idx * 100,
                    bg_runtime_s, bg_skip_s, tmp_dir,
                    probe_io_mode, probe_queue_depth, probe_cpu_mode, probe_mem_mode,
                    file_size_bytes,
                ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            ))

        # Collect ycsb result
        ycsb_stdout, ycsb_stderr = ycsb_proc.communicate()
        combined = ycsb_stdout + ycsb_stderr
        try:
            rdb_ops_s, _ = _parse_throughput_mean(combined)
        except ValueError as e:
            print(f"\n  [probe_rdb] WARNING: {e}")
            rdb_ops_s = float("nan")

        # Collect probe worker results
        probe_tput = 0.0
        for idx, p in enumerate(probe_procs):
            stdout, stderr = p.communicate()
            if stderr and stderr.strip():
                print(f"\n  [worker warning]: {stderr.strip()}", file=sys.stderr)
            if p.returncode != 0:
                print(f"\n  [probe_rdb] Worker {idx} exited non-zero (sample {sample_idx}) — skipping")
                continue
            try:
                data = json.loads(stdout.strip())
                probe_tput += data.get(tput_key, 0.0)
            except (json.JSONDecodeError, KeyError):
                pass

        sample_rdb.append(rdb_ops_s)
        sample_probe.append(probe_tput)

        try:
            os.unlink(spec)
        except OSError:
            pass

        # Cooldown between samples
        if sample_idx < samples - 1:
            try:
                os.sync()
            except AttributeError:
                pass
            time.sleep(2.0)

    # Filter NaN before averaging
    valid_rdb = [x for x in sample_rdb if not math.isnan(x)]
    avg_rdb   = statistics.mean(valid_rdb) if valid_rdb else float("nan")
    avg_probe = statistics.mean(sample_probe) if sample_probe else 0.0

    # Cooldown to let disk controller settle before next measurement
    try:
        os.sync()
    except AttributeError:
        pass
    time.sleep(2.0)

    return avg_rdb, avg_probe


# ---------------------------------------------------------------------------
# Phase 2: binary search (close mirror of probe.py's _run_phase2)
# ---------------------------------------------------------------------------

def _run_phase2(
    n_full: int,
    probe_type: str,
    baseline_ops_s: float,
    threshold: float,
    binary_steps: int,
    seed_probe_tput: float,
    seed_rdb_ops_s: float,
    measurement_kw: dict,
) -> tuple[list[dict], float, float, float]:
    """Binary-search for the highest fractional probe intensity that keeps
    RocksDB ops/s ≥ threshold.

    Returns (steps, best_intensity, best_probe_ktokens, best_rdb_ktokens).
    """
    print(f"\n--- Phase 2: Binary search  (locked: {n_full} × intensity=1.0) ---")
    print(f"  {'step':>4}  {'intensity':>9}  {'rdb (ops/s)':>12}  {probe_type.upper()+' (T/s)':>14}  {'status'}")
    print(f"  {'----':>4}  {'---------':>9}  {'----------':>12}  {'------------':>14}")

    low, high       = 0.0, 1.0
    best_intensity  = 0.0
    best_probe_kt   = seed_probe_tput * _KT
    best_rdb_kt     = seed_rdb_ops_s  * _KT
    steps: list[dict] = []

    for step in range(1, binary_steps + 1):
        mid = (low + high) / 2.0
        rdb_ops_s, probe_tput = run_measurement(
            n_probe_full=n_full, probe_frac=mid, **measurement_kw
        )
        interfered = (not math.isnan(rdb_ops_s)) and (rdb_ops_s < threshold)
        status = "interferes" if interfered else "ok"
        print(f"  {step:>4d}  {mid:>9.3f}  {rdb_ops_s:>12.0f}  {probe_tput:>14.0f}  {status}")
        steps.append(dict(
            step=step, intensity=mid,
            rdb_ops_s=rdb_ops_s, rdb_ktokens=rdb_ops_s * _KT,
            bg_ktokens=rdb_ops_s * _KT,
            probe_ktokens=probe_tput * _KT,
            interfered=interfered,
        ))
        if not interfered:
            best_intensity = mid
            best_probe_kt  = probe_tput * _KT
            best_rdb_kt    = rdb_ops_s  * _KT
            low  = mid
        else:
            high = mid

    return steps, best_intensity, best_probe_kt, best_rdb_kt


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def sweep(
    probe_type:      str,
    # RocksDB background params
    bg_binary:       str,
    bg_dbpath:       str,
    bg_base_spec:    str,
    bg_threads:      int,
    bg_rocksdb_parallelism: int,
    bg_record_count: int,
    bg_operation_count: int,
    bg_runtime_s:    int,
    bg_skip_s:       int,
    bg_xputfile_dir: str | None,
    # Probe workers
    worker_bin:      str,
    probe_io_mode:   str   = "rand_write",
    probe_cpu_mode:  str   = "cpu_int",
    probe_mem_mode:  str   = "mem_copy",
    probe_queue_depth: int = 1,
    file_size_bytes: int   = 0,
    # Sweep control
    drop_pct:        float = 0.05,
    max_probes:      int   = 64,
    binary_steps:    int   = 5,
    samples:         int   = 1,
    baseline_samples: int  = 1,
    interference_threshold_count: int = 3,
    step:            int   = 1,
    start_n:         int   = 1,
    # Reset / cache
    reset_db_per_point: bool = False,
    drop_caches:     bool  = False,
    load_threads:    int   = 8,
    # Scratch
    tmp_dir:         str   = "/tmp/slack-meter-probe-rdb",
) -> dict:
    os.makedirs(tmp_dir, exist_ok=True)

    # Configure probe resource mix
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

    # Shared kwargs for every run_measurement call
    meas_kw = dict(
        bg_binary=bg_binary, bg_dbpath=bg_dbpath,
        bg_base_spec=bg_base_spec, bg_threads=bg_threads,
        bg_rocksdb_parallelism=bg_rocksdb_parallelism,
        bg_record_count=bg_record_count, bg_operation_count=bg_operation_count,
        bg_runtime_s=bg_runtime_s, bg_skip_s=bg_skip_s,
        bg_xputfile=None,
        tmp_dir=tmp_dir,
        probe_io_mix=probe_io_mix, probe_mem_mix=probe_mem_mix,
        probe_io_mode=probe_io_mode, probe_cpu_mode=probe_cpu_mode,
        probe_mem_mode=probe_mem_mode, probe_queue_depth=probe_queue_depth,
        tput_key=tput_key, worker_bin=worker_bin,
        file_size_bytes=file_size_bytes,
        samples=samples,
        reset_db_per_point=reset_db_per_point, drop_caches=drop_caches,
        load_threads=load_threads,
    )

    # ------------------------------------------------------------------
    # Phase 0: Baseline (ycsb_test alone, no probe workers)
    # ------------------------------------------------------------------
    print("--- Phase 0: Baseline (RocksDB alone) ---")

    baseline_runs: list[float] = []
    n_baseline = max(1, baseline_samples)
    for i in range(n_baseline):
        if bg_xputfile_dir:
            meas_kw["bg_xputfile"] = os.path.join(bg_xputfile_dir, f"baseline_{i}.csv")
        rdb_ops_s, _ = run_measurement(n_probe_full=0, probe_frac=0.0, **meas_kw)
        baseline_runs.append(rdb_ops_s)
        if n_baseline > 1:
            print(f"  baseline sample {i+1}/{n_baseline}: {rdb_ops_s:,.0f} ops/s")

    valid_baseline = [x for x in baseline_runs if not math.isnan(x)]
    if not valid_baseline:
        raise RuntimeError("All baseline measurements failed. Check ycsb_test binary/DB path.")

    baseline_ops_s = statistics.mean(valid_baseline)
    threshold      = baseline_ops_s * (1.0 - drop_pct)

    if n_baseline > 1:
        print(f"  Baseline RocksDB : {baseline_ops_s:,.0f} ops/s (mean of {n_baseline})")
    else:
        print(f"  Baseline RocksDB : {baseline_ops_s:,.0f} ops/s")
    print(f"  Threshold        : {threshold:,.0f} ops/s  (drop >= {drop_pct*100:.1f}%)")
    meas_kw["bg_xputfile"] = None  # reset to no CSV for sweep phases

    # ------------------------------------------------------------------
    # Phase 1: Linear sweep + inline Phase 2 on interference
    # ------------------------------------------------------------------
    print(f"\n--- Phase 1: Linear {probe_type.upper()} sweep ---")
    print(f"  {'Probes':>7}  {'rdb (ops/s)':>12}  {probe_type.upper()+' (T/s)':>14}  {'status'}")
    print(f"  {'-------':>7}  {'----------':>12}  {'------------':>14}")

    phase1: list[dict] = []
    phase2: list[dict] = []
    consecutive_interference  = 0
    last_clean_n              = 0
    last_clean_probe_kt       = 0.0
    last_clean_rdb_kt         = baseline_ops_s * _KT
    best_intensity            = 0.0
    best_probe_ktokens        = 0.0
    best_rdb_ktokens          = baseline_ops_s * _KT

    n_probe    = start_n
    step_size  = step

    while n_probe <= max_probes:
        if bg_xputfile_dir:
            meas_kw["bg_xputfile"] = os.path.join(bg_xputfile_dir, f"phase1_n{n_probe}.csv")

        rdb_ops_s, probe_tput = run_measurement(
            n_probe_full=n_probe, probe_frac=0.0, **meas_kw
        )
        interfered = (not math.isnan(rdb_ops_s)) and (rdb_ops_s < threshold)
        status = "INTERFERENCE" if interfered else "ok"
        print(f"  {n_probe:>7d}  {rdb_ops_s:>12.0f}  {probe_tput:>14.0f}  {status}")
        phase1.append(dict(
            n_probe=n_probe,
            rdb_ops_s=rdb_ops_s, rdb_ktokens=rdb_ops_s * _KT,
            bg_ktokens=rdb_ops_s * _KT,
            probe_ktokens=probe_tput * _KT,
            interfered=interfered,
        ))

        if interfered:
            if step_size > 1:
                backtrack_start = last_clean_n + 1
                print(f"\n  [backtrack] Interference at {n_probe}. "
                      f"Backtracking to {backtrack_start} with step=1 …")
                n_probe = backtrack_start
                step_size = 1
                consecutive_interference = 0
                continue
            else:
                consecutive_interference += 1
                if consecutive_interference >= interference_threshold_count:
                    p2_steps, best_intensity, best_probe_ktokens, best_rdb_ktokens = _run_phase2(
                        last_clean_n, probe_type, baseline_ops_s, threshold,
                        binary_steps, last_clean_probe_kt / _KT, last_clean_rdb_kt / _KT,
                        meas_kw,
                    )
                    phase2.extend(p2_steps)

                    if not any(s["interfered"] for s in p2_steps):
                        # Phase 2 verified clean — resume Phase 1 from n_full+2
                        verified_n = last_clean_n + 1
                        print(f"\n  Phase 2 found no interference — probe {verified_n} verified "
                              f"clean; resuming Phase 1 from probe {verified_n + 1}")
                        phase1.append(dict(
                            n_probe=verified_n, rdb_ops_s=None,
                            rdb_ktokens=best_rdb_ktokens,
                            bg_ktokens=best_rdb_ktokens,
                            probe_ktokens=best_probe_ktokens,
                            interfered=False, verified_via_phase2=True,
                        ))
                        last_clean_n        = verified_n
                        last_clean_probe_kt = best_probe_ktokens
                        last_clean_rdb_kt   = best_rdb_ktokens
                        consecutive_interference = 0
                        n_probe    = verified_n + 1
                        step_size  = step
                        print(f"\n--- Phase 1 (resumed from probe {n_probe} step {step_size}): "
                              f"Linear {probe_type.upper()} sweep ---")
                        print(f"  {'Probes':>7}  {'rdb (ops/s)':>12}  "
                              f"{probe_type.upper()+' (T/s)':>14}  {'status'}")
                        print(f"  {'-------':>7}  {'----------':>12}  {'------------':>14}")
                        continue
                    else:
                        break   # Phase 2 confirmed interference — done
        else:
            last_clean_n        = n_probe
            last_clean_probe_kt = probe_tput * _KT
            last_clean_rdb_kt   = rdb_ops_s  * _KT
            consecutive_interference = 0

        n_probe += step_size
    else:
        # Exhausted max_probes without hitting the threshold count
        print(f"\n  Reached max_probes={max_probes} without sustained interference.")
        p2_steps, best_intensity, best_probe_ktokens, best_rdb_ktokens = _run_phase2(
            last_clean_n, probe_type, baseline_ops_s, threshold,
            binary_steps, last_clean_probe_kt / _KT, last_clean_rdb_kt / _KT,
            meas_kw,
        )
        phase2.extend(p2_steps)

    print(f"\n  {probe_type.upper()} slack: {last_clean_n} full worker(s) + 1 at intensity {best_intensity:.3f}")
    if last_clean_n == 0 and best_intensity == 0.0:
        print(f"  (RocksDB saturated — even a single low-intensity "
              f"{probe_type.upper()} worker causes interference)")

    return dict(
        # Schema matches probe.py for compatibility with plot.py
        type                  = f"sweep_{probe_type}_rocksdb",
        probe_type            = probe_type,
        baseline_bg_ktokens   = baseline_ops_s * _KT,
        slack_ktokens         = best_probe_ktokens,
        baseline_r_ktokens    = best_rdb_ktokens,
        baseline_tput         = baseline_ops_s,
        baseline_samples      = n_baseline,
        baseline_runs         = baseline_runs,
        interference_threshold= threshold,
        drop_pct              = drop_pct,
        slack_full            = last_clean_n,
        slack_partial         = best_intensity,
        phase1_probes         = phase1,
        phase2_probes         = phase2,
        # RocksDB-specific metadata
        rocksdb_bg_binary     = bg_binary,
        rocksdb_bg_dbpath     = bg_dbpath,
        rocksdb_workload_spec = bg_base_spec,
        rocksdb_bg_threads    = bg_threads,
        rocksdb_runtime_s     = bg_runtime_s,
        rocksdb_skip_s        = bg_skip_s,
        rocksdb_reset_per_pt  = reset_db_per_point,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    default_tmp = (
        "/holly/slack-meter-probe-rdb"
        if os.path.isdir("/holly") and os.access("/holly", os.W_OK)
        else "/tmp/slack-meter-probe-rdb"
    )

    parser = argparse.ArgumentParser(
        description="Probe CPU/IO/RAM slack with RocksDB (ycsb_test) as the background.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Probe type
    parser.add_argument("--probe-type", choices=["cpu", "io", "ram"], required=True,
                        help="Resource to probe.")

    # Saturation JSON shortcut
    parser.add_argument("--saturation-json", default=None, metavar="FILE",
                        help="JSON output from saturate_rocksdb.py. Populates --bg-binary, "
                             "--bg-dbpath, --bg-workload-spec, --bg-threads, --record-count, "
                             "--runtime, and --skip automatically (CLI flags override).")

    # RocksDB background (all can be overridden even when --saturation-json is given)
    parser.add_argument("--bg-binary",        default=None, metavar="PATH",
                        help="Path to ycsb_test binary.")
    parser.add_argument("--bg-dbpath",        default=None, metavar="PATH",
                        help="Path to the RocksDB database directory.")
    parser.add_argument("--bg-workload-spec", default=None, metavar="PATH",
                        help="YCSB .spec file describing the workload mix.")
    parser.add_argument("--bg-threads",       type=int, default=None, metavar="N",
                        help="Client thread count for RocksDB (saturation knee).")
    parser.add_argument("--rocksdb-parallelism", type=int, default=32, metavar="N",
                        help="RocksDB IncreaseParallelism value (default: 32).")
    parser.add_argument("--record-count",     type=int, default=None, metavar="N",
                        help="Record count (default: from saturation JSON or 10M).")
    parser.add_argument("--operation-count",  type=int, default=None, metavar="N",
                        help="Operations per run (default: same as --record-count).")
    parser.add_argument("--runtime",          type=int, default=None, metavar="S",
                        help="ycsb_test runtime per measurement in seconds "
                             "(default: from saturation JSON or 120).")
    parser.add_argument("--skip",             type=int, default=None, metavar="S",
                        help="Seconds to skip from ycsb_test mean (and probe worker warmup). "
                             "Default: from saturation JSON or 30.")
    parser.add_argument("--load-threads",     type=int, default=8, metavar="N",
                        help="Thread count for DB load phase (default: 8).")

    # Probe workers
    parser.add_argument("--worker-bin",         default=WORKER_BIN, metavar="PATH")
    parser.add_argument("--probe-io-mode",      default="rand_write")
    parser.add_argument("--probe-cpu-mode",     default="cpu_int")
    parser.add_argument("--probe-mem-mode",     default="mem_copy")
    parser.add_argument("--probe-queue-depth",  type=int, default=1, metavar="QD")
    parser.add_argument("--file-size-mib",      type=int, default=256, metavar="MiB",
                        help="Per-probe-worker scratch file size in MiB (default: 256; "
                             "try 4096 to exceed SSD DRAM cache for io probes).")

    # Sweep control
    parser.add_argument("--drop-pct",        type=float, default=0.05, metavar="F",
                        help="RocksDB ops/s drop fraction that triggers interference (default: 0.05).")
    parser.add_argument("--max-probes",      type=int,   default=64,   metavar="N")
    parser.add_argument("--binary-steps",    type=int,   default=5,    metavar="N")
    parser.add_argument("--samples",         type=int,   default=1,    metavar="N",
                        help="Samples per probe level; result is the mean (default: 1).")
    parser.add_argument("--baseline-samples",type=int,   default=1,    metavar="N",
                        help="Samples for Phase 0 baseline; uses the mean (default: 1).")
    parser.add_argument("--interference-threshold-count", type=int, default=3, metavar="N",
                        help="Consecutive interference events to terminate Phase 1 (default: 3).")
    parser.add_argument("--step",            type=int,   default=1,    metavar="N",
                        help="Phase 1 step size (default: 1).")
    parser.add_argument("--start-n",         type=int,   default=1,    metavar="N",
                        help="Phase 1 starting concurrency (default: 1).")

    # Reset / cache
    parser.add_argument("--reset-db-per-point", action="store_true",
                        help="Wipe and reload the DB before every measurement. "
                             "Guarantees identical LSM state; substantially slower.")
    parser.add_argument("--drop-caches", action="store_true",
                        help="Drop the OS page cache before each measurement (requires sudo).")

    # Output
    parser.add_argument("--tmp-dir",         default=default_tmp, metavar="DIR",
                        help="Scratch directory for temp spec files and probe worker I/O.")
    parser.add_argument("--xputfile-dir",    default=None,        metavar="DIR",
                        help="Directory to write per-measurement xput CSVs from ycsb_test.")
    parser.add_argument("--output",          default=None,        metavar="FILE")
    parser.add_argument("--plot",            default=None,        metavar="FILE")

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Resolve parameters: saturation JSON → CLI overrides
    # ------------------------------------------------------------------
    sat = {}
    if args.saturation_json:
        with open(args.saturation_json) as f:
            sat = json.load(f)

    bg_binary  = args.bg_binary  or sat.get("bg_binary")
    bg_dbpath  = args.bg_dbpath  or sat.get("bg_dbpath")
    bg_spec    = args.bg_workload_spec or sat.get("workload_spec")
    bg_threads = args.bg_threads or sat.get("knee_threads")
    record_count = args.record_count or sat.get("record_count", 10_000_000)
    runtime_s    = args.runtime  or sat.get("runtime_s", 120)
    skip_s       = args.skip     or sat.get("skip_s",    30)
    op_count     = args.operation_count or record_count

    # Validate required params
    missing = [name for name, val in [
        ("--bg-binary (or --saturation-json)", bg_binary),
        ("--bg-dbpath (or --saturation-json)", bg_dbpath),
        ("--bg-workload-spec (or --saturation-json)", bg_spec),
        ("--bg-threads (or --saturation-json)", bg_threads),
    ] if not val]
    if missing:
        parser.error("Missing required arguments:\n  " + "\n  ".join(missing))

    if not os.path.isfile(bg_binary):
        parser.error(f"ycsb_test binary not found: {bg_binary}")
    if not os.path.isdir(bg_dbpath) and not args.reset_db_per_point:
        parser.error(
            f"DB directory not found: {bg_dbpath}\n"
            "  Either run the load phase first, or use --reset-db-per-point to load automatically."
        )
    if not os.path.isfile(bg_spec):
        parser.error(f"workload spec not found: {bg_spec}")
    if not os.path.isfile(args.worker_bin):
        parser.error(f"worker binary not found: {args.worker_bin}")

    print("=" * 60)
    print(f"  RocksDB {args.probe_type.upper()} Slack Probe")
    print("=" * 60)
    print(f"  RocksDB binary  : {bg_binary}")
    print(f"  DB path         : {bg_dbpath}")
    print(f"  Workload spec   : {bg_spec}")
    print(f"  DB threads      : {bg_threads}  (saturation knee)")
    print(f"  Runtime/pt      : {runtime_s}s  (skip {skip_s}s)")
    print(f"  Drop pct        : {args.drop_pct*100:.1f}%")
    print(f"  Reset DB/pt     : {args.reset_db_per_point}")
    print(f"  Drop caches     : {args.drop_caches}")
    print(f"  Probe worker    : {args.worker_bin}")
    print("=" * 60)

    result = sweep(
        probe_type            = args.probe_type,
        bg_binary             = bg_binary,
        bg_dbpath             = bg_dbpath,
        bg_base_spec          = bg_spec,
        bg_threads            = bg_threads,
        bg_rocksdb_parallelism= args.rocksdb_parallelism,
        bg_record_count       = record_count,
        bg_operation_count    = op_count,
        bg_runtime_s          = runtime_s,
        bg_skip_s             = skip_s,
        bg_xputfile_dir       = args.xputfile_dir,
        worker_bin            = args.worker_bin,
        probe_io_mode         = args.probe_io_mode,
        probe_cpu_mode        = args.probe_cpu_mode,
        probe_mem_mode        = args.probe_mem_mode,
        probe_queue_depth     = args.probe_queue_depth,
        file_size_bytes       = args.file_size_mib * 1024 * 1024,
        drop_pct              = args.drop_pct,
        max_probes            = args.max_probes,
        binary_steps          = args.binary_steps,
        samples               = args.samples,
        baseline_samples      = args.baseline_samples,
        interference_threshold_count = args.interference_threshold_count,
        step                  = args.step,
        start_n               = args.start_n,
        reset_db_per_point    = args.reset_db_per_point,
        drop_caches           = args.drop_caches,
        load_threads          = args.load_threads,
        tmp_dir               = args.tmp_dir,
    )

    print("\n" + "=" * 60)
    print("  Result")
    print("=" * 60)
    print(f"  Baseline RocksDB ops/s : {result['baseline_tput']:,.0f}")
    print(f"  {args.probe_type.upper()} slack             : {result['slack_full']} full worker(s) "
          f"+ 1 at intensity {result['slack_partial']:.3f}")
    print(f"  {args.probe_type.upper()} slack throughput  : {result['slack_ktokens']*1000:,.0f} tokens/s")
    print("=" * 60)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        export = {k: v for k, v in result.items()
                  if k not in ("baseline_tput", "interference_threshold")}
        with open(out, "w") as f:
            json.dump(export, f, indent=2)
        print(f"\nResult written → {args.output}")

    if args.plot:
        _plot_slack_result(result, Path(args.plot))


if __name__ == "__main__":
    main()
