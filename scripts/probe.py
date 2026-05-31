#!/usr/bin/env python3
"""
probe.py
========
Sweeps pure CPU, I/O, or RAM probe workers on top of a background workload,
stopping when the PROBE throughput reaches a stable plateau.

Methodology
-----------
Phase 0  Baseline: run BG_PROCS background workers alone. Record throughput B.

Phase 1  Linear sweep: add one probe worker per round; stop when probe
         throughput stagnates for MAX_STAGNATION consecutive steps.
         Record S = peak probe throughput, R = background throughput at
         the plateau step.

The decline in background throughput is neither monitored nor used as a
stopping criterion.  Probing continues until the probe throughput itself
saturates.

Resource usage formula (requires total capacity C from saturate.py):
    resource_usage_pct = (C - S) * B / (C * R) * 100

All throughputs are reported in kTokens (1 ops/s = 0.001 kTokens).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent.resolve()
WORKER_BIN = str(REPO_ROOT / "build" / "worker")

_BG_SEED_BASE    = 1000
_PROBE_SEED_BASE = 2000
_KT              = 1e-3   # ops/s → kTokens
MAX_STAGNATION   = 5


def _plot_slack_result(result: dict, out_path: Path) -> None:
    """Delegate to plot.py — keeps probe.py free of matplotlib."""
    try:
        from plot import plot_slack_result
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        from plot import plot_slack_result  # type: ignore[no-redef]
    plot_slack_result(result, out_path)


# ---------------------------------------------------------------------------
# Core probe: run bg + probe workers concurrently, return (bg_tput, probe_tput)
# ---------------------------------------------------------------------------

def run_probe(
    bg_procs:          int,
    bg_io_mix:         float,
    bg_mem_mix:        float,
    bg_intensity:      float,
    n_probe_full:      int,
    probe_io_mix:      float,
    probe_mem_mix:     float,
    duration:          int,
    warmup:            int,
    tmp_dir:           str,
    worker_bin:        str,
    tput_key:          str,
    bg_io_mode:        str = "rand_write",
    probe_io_mode:     str = "rand_write",
    samples:           int = 3,
    bg_queue_depth:    int = 1,
    probe_queue_depth: int = 1,
    bg_cpu_mode:       str = "cpu_int",
    probe_cpu_mode:    str = "cpu_int",
    bg_mem_mode:       str = "mem_copy",
    probe_mem_mode:    str = "mem_copy",
    file_size_bytes:   int = 0,
) -> tuple[float, float]:
    """Run bg + probe workers concurrently *samples* times; return average
    (bg_tput, probe_tput) in ops/s."""

    def make_cmd(
        io_mix: float, mem_mix: float, intensity: float, seed: int,
        mode: str, qd: int, cpu_mode: str, mem_mode: str,
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
            "--io-mode",     mode,
            "--queue-depth", str(qd),
            "--cpu-mode",    cpu_mode,
            "--mem-mode",    mem_mode,
        ]
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
                make_cmd(bg_io_mix, bg_mem_mix, bg_intensity,
                         _BG_SEED_BASE + i + run_idx * 100,
                         bg_io_mode, bg_queue_depth, actual_bg_cpu, actual_bg_mem),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))

        for probe_idx in range(n_probe_full):
            env = os.environ.copy()
            env["WORKER_ID"] = str(bg_procs + probe_idx)
            env["REUSE_FILE"] = "1"
            procs.append(subprocess.Popen(
                make_cmd(probe_io_mix, probe_mem_mix, 1.0,
                         _PROBE_SEED_BASE + probe_idx + run_idx * 100,
                         probe_io_mode, probe_queue_depth, probe_cpu_mode, probe_mem_mode),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))

        bg_tput = probe_tput = 0.0
        for idx, p in enumerate(procs):
            stdout, stderr = p.communicate()
            if stderr and stderr.strip():
                print(f"\n[worker warning]: {stderr.decode('utf-8', errors='replace').strip()}",
                      file=sys.stderr)
            if p.returncode != 0:
                print(f"\n[probe] Worker {idx} exited non-zero (run {run_idx}) — skipping sample")
                continue
            try:
                data = json.loads(stdout.strip())
                if idx < bg_procs:
                    bg_tput    += data.get("throughput", 0.0)
                else:
                    probe_tput += data.get(tput_key, 0.0)
            except (json.JSONDecodeError, KeyError):
                pass

        runs.append((bg_tput, probe_tput))

    avg_bg    = sum(r[0] for r in runs) / len(runs)
    avg_probe = sum(r[1] for r in runs) / len(runs)

    # Let OS writeback queues drain between rounds
    try:
        os.sync()
    except AttributeError:
        pass
    time.sleep(2.0)

    return avg_bg, avg_probe


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def sweep(
    probe_type:        str,
    bg_procs:          int,
    bg_io_mix:         float,
    bg_mem_mix:        float,
    bg_intensity:      float,
    duration:          int,
    warmup:            int,
    tmp_dir:           str,
    worker_bin:        str,
    capacity:          float | None = None,   # C in kTokens/s (from saturate.py)
    max_probes:        int   = 64,
    bg_io_mode:        str   = "rand_write",
    probe_io_mode:     str   = "rand_write",
    samples:           int   = 3,
    bg_queue_depth:    int   = 1,
    probe_queue_depth: int   = 1,
    bg_cpu_mode:       str   = "cpu_int",
    probe_cpu_mode:    str   = "cpu_int",
    bg_mem_mode:       str   = "mem_copy",
    probe_mem_mode:    str   = "mem_copy",
    file_size_bytes:   int   = 0,
    baseline_samples:  int   = 1,
) -> dict:
    os.makedirs(tmp_dir, exist_ok=True)

    # Configure probe resource type
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

    kw = dict(
        bg_procs=bg_procs, bg_io_mix=bg_io_mix, bg_mem_mix=bg_mem_mix,
        bg_intensity=bg_intensity,
        probe_io_mix=probe_io_mix, probe_mem_mix=probe_mem_mix,
        duration=duration, warmup=warmup, tmp_dir=tmp_dir,
        worker_bin=worker_bin, tput_key=tput_key,
        bg_io_mode=bg_io_mode, probe_io_mode=probe_io_mode, samples=samples,
        bg_queue_depth=bg_queue_depth, probe_queue_depth=probe_queue_depth,
        bg_cpu_mode=bg_cpu_mode, probe_cpu_mode=probe_cpu_mode,
        bg_mem_mode=bg_mem_mode, probe_mem_mode=probe_mem_mode,
        file_size_bytes=file_size_bytes,
    )

    # ------------------------------------------------------------------
    # Phase 0: baseline — bg workers only, record B
    # ------------------------------------------------------------------
    print("--- Phase 0: Baseline (background workers only) ---")

    baseline_runs: list[float] = []
    n_baseline = max(1, baseline_samples)
    for i in range(n_baseline):
        bt, _ = run_probe(n_probe_full=0, **kw)
        baseline_runs.append(bt)
        if n_baseline > 1:
            print(f"  baseline sample {i+1}/{n_baseline}: {bt*_KT:,.3f} kTokens/s")

    baseline_tput = statistics.mean(baseline_runs)   # B
    if n_baseline > 1:
        print(f"  Baseline B : {baseline_tput*_KT:,.3f} kTokens/s (mean of {n_baseline})")
    else:
        print(f"  Baseline B : {baseline_tput*_KT:,.3f} kTokens/s")

    # ------------------------------------------------------------------
    # Phase 1: sweep probe workers until probe throughput plateaus
    #
    # Stagnation criterion (mirrors saturate.py):
    #   improvement threshold = 2% of (running_max / peak_n)
    #   stop after MAX_STAGNATION consecutive steps without improvement
    # ------------------------------------------------------------------
    print(f"\n--- Phase 1: Linear {probe_type.upper()} sweep (stop on probe plateau) ---")
    print(f"  {'Probes':>7}  {'bg (kT/s)':>12}  {probe_type.upper()+' (kT/s)':>12}")
    print(f"  {'-------':>7}  {'---------':>12}  {'---------':>12}")

    phase1: list[dict] = []
    running_max          = 0.0
    peak_n               = 1
    steps_since_improve  = 0
    slack_ktokens        = 0.0   # S
    r_at_plateau         = 0.0   # R

    n_probe = 1
    while n_probe <= max_probes:
        bg_tput, probe_tput = run_probe(n_probe_full=n_probe, **kw)
        probe_kt = probe_tput * _KT
        bg_kt    = bg_tput    * _KT
        print(f"  {n_probe:>7d}  {bg_kt:>12.3f}  {probe_kt:>12.3f}")
        phase1.append(dict(n_probe=n_probe, bg_ktokens=bg_kt, probe_ktokens=probe_kt))

        min_gain = (running_max / peak_n * 0.02) if running_max > 0 else 0.0
        if probe_tput > running_max + min_gain:
            running_max         = probe_tput
            peak_n              = n_probe
            slack_ktokens       = probe_kt
            r_at_plateau        = bg_kt
            steps_since_improve = 0
        else:
            steps_since_improve += 1

        if steps_since_improve >= MAX_STAGNATION:
            print(f"\n  Probe throughput plateaued. S = {slack_ktokens:.3f} kTokens/s")
            break

        n_probe += 1
    else:
        print(f"\n  Reached max_probes={max_probes}. S = {slack_ktokens:.3f} kTokens/s")

    # ------------------------------------------------------------------
    # Resource usage (requires C)
    # ------------------------------------------------------------------
    resource_usage_pct: float | None = None
    if capacity is not None and capacity > 0 and r_at_plateau > 0:
        C = capacity              # kTokens/s
        S = slack_ktokens         # kTokens/s
        B = baseline_tput * _KT  # kTokens/s
        R = r_at_plateau          # kTokens/s
        resource_usage_pct = (C - S) * B / (C * R) * 100

    return dict(
        type                 = f"sweep_{probe_type}_loaded",
        probe_type           = probe_type,
        # Phase 0
        baseline_bg_ktokens  = baseline_tput * _KT,
        baseline_samples     = n_baseline,
        baseline_runs        = [r * _KT for r in baseline_runs],
        # Phase 1 results
        slack_ktokens        = slack_ktokens,        # S
        baseline_r_ktokens   = r_at_plateau,         # R at plateau
        # Optional capacity + derived metric
        capacity_ktokens     = capacity,
        resource_usage_pct   = resource_usage_pct,
        # Config
        bg_procs             = bg_procs,
        bg_io_mix            = bg_io_mix,
        bg_mem_mix           = bg_mem_mix,
        bg_intensity         = bg_intensity,
        # Probe data for plotting
        phase1_probes        = phase1,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep probe workers under background load; stop when probe throughput plateaus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  probe.py --probe-type cpu --bg-procs 4
  probe.py --probe-type io  --bg-procs 4 --capacity 12.5
""",
    )
    parser.add_argument("--probe-type",    choices=["cpu", "io", "ram"], required=True,
                        help="Resource type to probe")
    parser.add_argument("--bg-procs",      type=int,   required=True,   metavar="N")
    parser.add_argument("--bg-io-mix",     type=float, default=0.3,     metavar="F")
    parser.add_argument("--bg-mem-mix",    type=float, default=0.0,     metavar="F")
    parser.add_argument("--bg-intensity",  type=float, default=0.75,    metavar="F")
    parser.add_argument("--duration",      type=int,   default=30,      metavar="S")
    parser.add_argument("--warmup",        type=int,   default=15,      metavar="S")
    parser.add_argument("--samples",       type=int,   default=1,       metavar="N",
                        help="samples per probe level (default: 3)")
    parser.add_argument("--baseline-samples", type=int, default=1,      metavar="N",
                        help="baseline samples; uses the mean (default: 1)")
    parser.add_argument("--max-probes",    type=int,   default=64,      metavar="N")
    parser.add_argument("--capacity",      type=float, default=None,    metavar="KTOKENS/S",
                        help="total capacity C in kTokens/s from saturate.py; enables resource_usage_pct")
    parser.add_argument("--tmp-dir",       default="/tmp/slack-meter",  metavar="DIR")
    parser.add_argument("--worker-bin",    default=WORKER_BIN,          metavar="PATH")
    parser.add_argument("--bg-io-mode",    default="rand_write",        help="background IO mode")
    parser.add_argument("--probe-io-mode", default="rand_write",        help="probe IO mode")
    parser.add_argument("--cpu-mode",      default="cpu_int",           help="default CPU mode")
    parser.add_argument("--bg-cpu-mode",   default=None,                help="background CPU mode (defaults to --cpu-mode)")
    parser.add_argument("--probe-cpu-mode",default=None,                help="probe CPU mode (defaults to --cpu-mode)")
    parser.add_argument("--mem-mode",      default="mem_copy",          help="default memory mode")
    parser.add_argument("--bg-mem-mode",   default=None,                help="background memory mode (defaults to --mem-mode)")
    parser.add_argument("--probe-mem-mode",default=None,                help="probe memory mode (defaults to --mem-mode)")
    parser.add_argument("--queue-depth",   type=int,   default=1,       metavar="QD")
    parser.add_argument("--bg-queue-depth",   type=int, default=None,   metavar="QD",
                        help="background worker queue depth (defaults to --queue-depth)")
    parser.add_argument("--probe-queue-depth",type=int, default=None,   metavar="QD",
                        help="probe worker queue depth (defaults to --queue-depth)")
    parser.add_argument("--file-size-mib", type=int,   default=256,     metavar="MiB",
                        help="per-worker scratch file size in MiB (default: 256)")
    parser.add_argument("--output",        default=None,                metavar="FILE")
    parser.add_argument("--plot",          default=None,                metavar="FILE")
    args = parser.parse_args()

    if not os.path.exists(args.worker_bin):
        print(f"[probe] ERROR: worker binary not found at {args.worker_bin}")
        sys.exit(1)

    bg_qd      = args.bg_queue_depth    if args.bg_queue_depth    is not None else args.queue_depth
    probe_qd   = args.probe_queue_depth if args.probe_queue_depth is not None else args.queue_depth
    bg_cpu     = args.bg_cpu_mode       if args.bg_cpu_mode       is not None else args.cpu_mode
    probe_cpu  = args.probe_cpu_mode    if args.probe_cpu_mode    is not None else args.cpu_mode
    bg_mem     = args.bg_mem_mode       if args.bg_mem_mode       is not None else args.mem_mode
    probe_mem  = args.probe_mem_mode    if args.probe_mem_mode    is not None else args.mem_mode

    print("=" * 60)
    print(f"  {args.probe_type.upper()} Probe Sweep Under Background Load")
    print("=" * 60)
    print(f"  Background : {args.bg_procs} workers  "
          f"io={args.bg_io_mix}  mem={args.bg_mem_mix}  intensity={args.bg_intensity}")
    print(f"  Probe dur  : {args.duration}s   file_size={args.file_size_mib} MiB")
    if args.capacity:
        print(f"  Capacity C : {args.capacity:.3f} kTokens/s")
    print(f"  Tmp dir    : {args.tmp_dir}")
    print("=" * 60)

    result = sweep(
        probe_type        = args.probe_type,
        bg_procs          = args.bg_procs,
        bg_io_mix         = args.bg_io_mix,
        bg_mem_mix        = args.bg_mem_mix,
        bg_intensity      = args.bg_intensity,
        duration          = args.duration,
        warmup            = args.warmup,
        tmp_dir           = args.tmp_dir,
        worker_bin        = args.worker_bin,
        capacity          = args.capacity,
        max_probes        = args.max_probes,
        bg_io_mode        = args.bg_io_mode,
        probe_io_mode     = args.probe_io_mode,
        samples           = args.samples,
        bg_queue_depth    = bg_qd,
        probe_queue_depth = probe_qd,
        bg_cpu_mode       = bg_cpu,
        probe_cpu_mode    = probe_cpu,
        bg_mem_mode       = bg_mem,
        probe_mem_mode    = probe_mem,
        file_size_bytes   = args.file_size_mib * 1024 * 1024,
        baseline_samples  = args.baseline_samples,
    )

    print("\n" + "=" * 60)
    print("  Result")
    print("=" * 60)
    print(f"  Baseline B          : {result['baseline_bg_ktokens']:.3f} kTokens/s")
    print(f"  Slack S             : {result['slack_ktokens']:.3f} kTokens/s")
    print(f"  Baseline R (at S)   : {result['baseline_r_ktokens']:.3f} kTokens/s")
    if result.get("capacity_ktokens") is not None:
        print(f"  Capacity C          : {result['capacity_ktokens']:.3f} kTokens/s")
    if result.get("resource_usage_pct") is not None:
        print(f"  Resource usage      : {result['resource_usage_pct']:.1f}%")
    print("=" * 60)

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
