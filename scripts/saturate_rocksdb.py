#!/usr/bin/env python3
"""
saturate_rocksdb.py
===================
Sweeps RocksDB client thread counts to find the saturation knee (peak ops/s),
using the ycsb_test binary from the htap project as the workload driver.

This replaces reading a pre-computed summary.csv from htap's saturation_sweep.sh.
The output JSON is consumed directly by probe_rocksdb.py.

Algorithm
---------
For each thread count N in THREAD_COUNTS:
  1. (optional) Wipe and reload a fresh DB so every point starts from clean LSM state.
  2. (optional) Drop the OS page cache so reads hit disk, not RAM.
  3. Run ycsb_test for RUNTIME_S seconds; skip first SKIP_S in the mean.
  4. Parse `throughput mean: X` from stdout → ops/s for this thread count.
  5. Stop when ops/s stagnates for MAX_STAGNATION consecutive steps.

Output JSON
-----------
{
  "knee_threads":   16,
  "peak_ops_s":     45231.2,
  "peak_stddev":    812.4,
  "workload_spec":  "/path/to/workloada.spec",
  "bg_binary":      "/path/to/ycsb_test",
  "bg_dbpath":      "/path/to/rocksdb",
  "sweep":          [{"threads": 1, "ops_s": 12000.0, "stddev": 300.0}, ...]
}

Usage
-----
  python3 saturate_rocksdb.py \\
      --bg-binary    /path/to/htap/build/src/test/ycsb/ycsb_test \\
      --bg-dbpath    /holly/rocksdb_bench \\
      --bg-workload-spec /path/to/htap/src/test/ycsb/workloads/workloada.spec \\
      --record-count 10000000 \\
      --runtime      120 \\
      --skip         30 \\
      --thread-counts "1 2 4 8 16 32 64" \\
      --output       results/saturation.json

To start with a fresh DB (wipe + reload between every thread count):
  ... --reset-db-per-point --load-threads 8 --rocksdb-parallelism 32
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()

_STAGNATION_GAIN_FRACTION = 0.02   # must improve by 2% of per-thread contribution to count
_MAX_STAGNATION           = 5      # consecutive non-improving steps before stopping


# ---------------------------------------------------------------------------
# Spec helpers
# ---------------------------------------------------------------------------

def write_temp_spec(base_spec_path: str, tmp_dir: str, **overrides) -> str:
    """Return path to a temp copy of base_spec with key=value overrides.

    We filter out pre-existing definitions of overridden keys to ensure
    our overrides take effect regardless of parser precedence.
    """
    os.makedirs(tmp_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".spec", dir=tmp_dir, prefix="sat_")
    
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
        f.write("\n# --- saturate_rocksdb.py overrides ---\n")
        for k, v in overrides.items():
            f.write(f"{k}={v}\n")
    return path


# ---------------------------------------------------------------------------
# DB load / page-cache helpers
# ---------------------------------------------------------------------------

def load_db(
    binary: str,
    dbpath: str,
    spec_path: str,
    *,
    load_threads: int = 8,
    rocksdb_parallelism: int = 32,
    record_count: int,
    log_path: str | None = None,
) -> None:
    """Wipe dbpath (if it exists) and load a fresh RocksDB database via ycsb_test.

    The wipe is done here rather than in the caller so every code path that
    loads the DB is safe: bootstrap opens with only the default column family,
    which fails if an existing DB already has the 'baseline' column family.
    """
    if os.path.exists(dbpath):
        print(f"  [load] Wiping existing DB at {dbpath} …", flush=True)
        shutil.rmtree(dbpath)
    tmp_dir = os.path.dirname(spec_path)
    load_spec = write_temp_spec(
        spec_path, tmp_dir,
        dbpath=dbpath,              # override any dbpath in the base spec
        recordcount=record_count,
        operationcount=record_count,
        rocksdb_parallelism=rocksdb_parallelism,
        xputwindow=0,   # suppress CSV writing during load
    )
    cmd = [
        binary,
        "-db",        "baseline",
        "-dbpath",    dbpath,
        "-P",         load_spec,
        "-bootstrap", "true",
        "-threads",   str(load_threads),
        "-load",      "true",
        "-run",       "false",
        "-throughput","false",
        "-runtime",   "0",
        "-levels",    "7",
        "-table",     "baseline",
    ]
    print(f"  [load] Loading {record_count:,} records into {dbpath} …")
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text(out)
        # Extract load IOPS from output for informational purposes
        m = re.search(r"loading records:\s*\d+\s+use time:\s*[\d.]+\s*s\s+IOPS:\s*([\d.]+)", out)
        if m:
            print(f"  [load] Done — {float(m.group(1)):,.0f} load IOPS")
        else:
            print("  [load] Done.")
    except subprocess.CalledProcessError as e:
        print(f"  [load] ERROR: ycsb_test load phase failed (exit {e.returncode})")
        print(e.output[-2000:])
        raise
    finally:
        os.unlink(load_spec)


def drop_page_cache() -> None:
    """Drop the OS page cache so RocksDB reads hit disk rather than RAM.

    Requires passwordless sudo access to /proc/sys/vm/drop_caches, or running
    as root.  A failure here is non-fatal — a warning is printed instead.
    """
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

        result = subprocess.run(
            ["sudo", "tee", "/proc/sys/vm/drop_caches"],
            input="3", text=True, capture_output=True,
        )
        if result.returncode == 0:
            print("done.")
        else:
            print(f"WARNING: sudo tee failed (need passwordless sudo). "
                  f"Reads may be served from RAM.")
    except Exception as e:
        print(f"WARNING: drop_caches skipped ({e}).")


# ---------------------------------------------------------------------------
# Single-point measurement
# ---------------------------------------------------------------------------

def _parse_throughput_mean(stdout: str) -> tuple[float, float]:
    """Parse 'throughput mean:X  stddev: Y' from ycsb_test stdout.

    Returns (ops_s_mean, ops_s_stddev).  Raises ValueError if not found.
    """
    m = re.search(
        r"throughput mean:\s*([\d.eE+\-]+)\s+stddev:\s*([\d.eE+\-]+)", stdout
    )
    if not m:
        raise ValueError(
            "Could not find 'throughput mean:' in ycsb_test output.\n"
            f"Last 500 chars of stdout:\n{stdout[-500:]}"
        )
    return float(m.group(1)), float(m.group(2))


def run_one_thread_count(
    binary: str,
    dbpath: str,
    base_spec: str,
    *,
    threads: int,
    runtime_s: int,
    skip_s: int,
    record_count: int,
    operation_count: int,
    rocksdb_parallelism: int,
    tmp_dir: str,
    xputfile: str | None = None,
    xputwindow: int = 10,
) -> tuple[float, float]:
    """Run ycsb_test at a fixed thread count; return (mean_ops_s, stddev_ops_s)."""
    spec = write_temp_spec(
        base_spec, tmp_dir,
        dbpath=dbpath,              # override any dbpath in the base spec
        recordcount=record_count,
        operationcount=operation_count,
        rocksdb_parallelism=rocksdb_parallelism,
        skip=skip_s,
        xputwindow=xputwindow if xputfile else 0,
        **({"xputfile": xputfile} if xputfile else {}),
    )
    cmd = [
        binary,
        "-db",        "baseline",
        "-dbpath",    dbpath,
        "-P",         spec,
        "-bootstrap", "false",
        "-threads",   str(threads),
        "-load",      "false",
        "-run",       "false",
        "-throughput","true",
        "-runtime",   str(runtime_s),
        "-skip",      str(skip_s),
        "-levels",    "7",
        "-table",     "baseline",
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        stdout = result.stdout + result.stderr   # ycsb_test sometimes mixes channels
        if result.returncode != 0:
            print(f"\n  [sat] ycsb_test exited {result.returncode}. Stderr:\n{result.stderr[-500:]}")
            return float("nan"), float("nan")
        return _parse_throughput_mean(stdout)
    except Exception as e:
        print(f"\n  [sat] Error running ycsb_test: {e}")
        return float("nan"), float("nan")
    finally:
        try:
            os.unlink(spec)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Saturation sweep
# ---------------------------------------------------------------------------

def find_saturation_knee(
    binary: str,
    dbpath: str,
    base_spec: str,
    *,
    thread_counts: list[int],
    runtime_s: int,
    skip_s: int,
    record_count: int,
    operation_count: int,
    rocksdb_parallelism: int,
    load_threads: int,
    tmp_dir: str,
    reset_db_per_point: bool = False,
    drop_caches: bool = False,
    output_dir: str | None = None,
) -> dict:
    """Sweep thread counts and return knee info dict."""
    os.makedirs(tmp_dir, exist_ok=True)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    sweep: list[dict] = []
    running_max = 0.0
    peak_n = thread_counts[0]
    steps_since_improvement = 0

    print(f"\n{'='*60}")
    print(f"  RocksDB Saturation Sweep")
    print(f"{'='*60}")
    print(f"  Thread counts : {thread_counts}")
    print(f"  Runtime/point : {runtime_s}s  (skip {skip_s}s warmup)")
    print(f"  Reset DB/pt   : {reset_db_per_point}")
    print(f"  Drop caches   : {drop_caches}")
    print(f"{'='*60}\n")
    print(f"  {'threads':>8}  {'ops/s':>12}  {'stddev':>10}  {'status'}")
    print(f"  {'--------':>8}  {'------------':>12}  {'----------':>10}")

    for threads in thread_counts:
        if reset_db_per_point:
            print(f"\n  Wiping and reloading DB for threads={threads} …")
            if os.path.exists(dbpath):
                shutil.rmtree(dbpath)
            xputfile_load = None
            if output_dir:
                xputfile_load = os.path.join(output_dir, f"load_t{threads}.log")
            load_db(
                binary, dbpath, base_spec,
                load_threads=load_threads,
                rocksdb_parallelism=rocksdb_parallelism,
                record_count=record_count,
                log_path=xputfile_load,
            )

        if drop_caches:
            drop_page_cache()

        xputfile = None
        if output_dir:
            xputfile = os.path.join(output_dir, f"xput_t{threads}.csv")

        ops_s, stddev = run_one_thread_count(
            binary, dbpath, base_spec,
            threads=threads,
            runtime_s=runtime_s,
            skip_s=skip_s,
            record_count=record_count,
            operation_count=operation_count,
            rocksdb_parallelism=rocksdb_parallelism,
            tmp_dir=tmp_dir,
            xputfile=xputfile,
        )

        import math
        is_nan = math.isnan(ops_s)
        sweep.append({"threads": threads, "ops_s": ops_s, "stddev": stddev})

        # Stagnation check (skip NaN points)
        if not is_nan:
            min_gain = (running_max / peak_n * _STAGNATION_GAIN_FRACTION) if running_max > 0 else 0.0
            if ops_s > running_max + min_gain:
                running_max = ops_s
                peak_n = threads
                steps_since_improvement = 0
                status = "new max"
            else:
                steps_since_improvement += 1
                status = f"no gain ({steps_since_improvement}/{_MAX_STAGNATION})"
        else:
            status = "error"

        print(f"  {threads:>8d}  {ops_s:>12.0f}  {stddev:>10.0f}  {status}")

        if steps_since_improvement >= _MAX_STAGNATION:
            print(f"\n  Throughput stagnated for {_MAX_STAGNATION} steps — stopping sweep.")
            break

    # Find the best point
    valid = [(s["threads"], s["ops_s"], s["stddev"]) for s in sweep
             if not (s["ops_s"] != s["ops_s"])]   # filter NaN
    if not valid:
        raise RuntimeError("All thread counts produced errors. Check ycsb_test binary and DB path.")

    best = max(valid, key=lambda x: x[1])
    knee_threads, peak_ops_s, peak_stddev = best

    print(f"\n{'='*60}")
    print(f"  Saturation knee: {knee_threads} threads → {peak_ops_s:,.0f} ops/s")
    print(f"{'='*60}\n")

    return dict(
        knee_threads  = knee_threads,
        peak_ops_s    = peak_ops_s,
        peak_stddev   = peak_stddev,
        workload_spec = base_spec,
        bg_binary     = binary,
        bg_dbpath     = dbpath,
        runtime_s     = runtime_s,
        skip_s        = skip_s,
        record_count  = record_count,
        sweep         = sweep,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    default_tmp = (
        "/holly/slack-meter-sat"
        if os.path.isdir("/holly") and os.access("/holly", os.W_OK)
        else "/tmp/slack-meter-sat"
    )

    parser = argparse.ArgumentParser(
        description="Sweep RocksDB thread counts to find the saturation knee.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Required
    parser.add_argument("--bg-binary",        required=True, metavar="PATH",
                        help="Path to ycsb_test binary (htap project build).")
    parser.add_argument("--bg-dbpath",        required=True, metavar="PATH",
                        help="Path to the RocksDB database directory (created if --reset-db-per-point).")
    parser.add_argument("--bg-workload-spec", required=True, metavar="PATH",
                        help="YCSB .spec file describing the workload mix.")

    # DB sizing
    parser.add_argument("--record-count",     type=int, default=10_000_000, metavar="N",
                        help="Number of records to load (default: 10M ≈ 20 GB with 2KB records).")
    parser.add_argument("--operation-count",  type=int, default=None,       metavar="N",
                        help="Number of operations per run (default: same as --record-count).")

    # Sweep knobs
    parser.add_argument("--thread-counts",    default="1 2 4 8 16 32 64",  metavar="COUNTS",
                        help="Space-separated thread counts to sweep (default: '1 2 4 8 16 32 64').")
    parser.add_argument("--runtime",          type=int, default=120,        metavar="S",
                        help="ycsb_test runtime per thread count in seconds (default: 120).")
    parser.add_argument("--skip",             type=int, default=30,         metavar="S",
                        help="Seconds to skip from throughput mean (warmup, default: 30).")

    # RocksDB config
    parser.add_argument("--rocksdb-parallelism", type=int, default=32,     metavar="N",
                        help="IncreaseParallelism thread count for RocksDB compaction (default: 32).")
    parser.add_argument("--load-threads",     type=int, default=8,          metavar="N",
                        help="Thread count for the DB load phase (default: 8).")

    # Reset / cache
    parser.add_argument("--reset-db-per-point", action="store_true",
                        help="Wipe and reload the DB before each thread count. "
                             "Guarantees identical LSM state per point; slower.")
    parser.add_argument("--drop-caches",      action="store_true",
                        help="Drop the OS page cache before each run (requires sudo).")

    # Paths
    parser.add_argument("--tmp-dir",          default=default_tmp,          metavar="DIR",
                        help="Scratch directory for temp spec files.")
    parser.add_argument("--output-dir",       default=None,                 metavar="DIR",
                        help="Directory to write per-thread xput CSVs.")
    parser.add_argument("--output",           default=None,                 metavar="FILE",
                        help="JSON file for the saturation result (consumed by probe_rocksdb.py).")

    args = parser.parse_args()

    # Validate
    if not os.path.isfile(args.bg_binary):
        print(f"ERROR: ycsb_test binary not found: {args.bg_binary}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(args.bg_workload_spec):
        print(f"ERROR: workload spec not found: {args.bg_workload_spec}", file=sys.stderr)
        sys.exit(1)

    thread_counts = [int(t) for t in args.thread_counts.split()]
    if not thread_counts:
        print("ERROR: --thread-counts produced an empty list.", file=sys.stderr)
        sys.exit(1)

    op_count = args.operation_count if args.operation_count is not None else args.record_count

    result = find_saturation_knee(
        binary             = args.bg_binary,
        dbpath             = args.bg_dbpath,
        base_spec          = args.bg_workload_spec,
        thread_counts      = thread_counts,
        runtime_s          = args.runtime,
        skip_s             = args.skip,
        record_count       = args.record_count,
        operation_count    = op_count,
        rocksdb_parallelism= args.rocksdb_parallelism,
        load_threads       = args.load_threads,
        tmp_dir            = args.tmp_dir,
        reset_db_per_point = args.reset_db_per_point,
        drop_caches        = args.drop_caches,
        output_dir         = args.output_dir,
    )

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Result written → {args.output}")
        print(f"  Pass to probe_rocksdb.py via: --saturation-json {args.output}")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
