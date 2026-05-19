#!/usr/bin/env python3
"""
sweep_io_loaded.py
==================
Sweeps pure I/O workers on top of a background workload, stopping when the
BACKGROUND throughput drops by DROP_PCT — not when I/O throughput plateaus.

Methodology
-----------
Phase 0  Baseline: run BG_PROCS background workers alone. Record throughput.

Phase 1  Linear sweep: add one full-intensity (io_mix=1, intensity=1) I/O
         worker per round; stop when background throughput drops >= DROP_PCT.

Phase 2  Binary search: lock (n_full-1) I/O workers at intensity=1.0, find
         the highest fractional intensity on the last one that leaves
         background throughput undisturbed.

All throughputs are reported in kTokens (1 ops/s = 1 token = 0.001 kTokens).

Usage
-----
    python3 scripts/sweep_io_loaded.py \\
        --bg-procs 8 --bg-io-mix 0.5 --bg-intensity 0.9 \\
        --duration 30 --drop-pct 0.05 \\
        --tmp-dir /tmp/slack-meter \\
        --output results/loaded_sweep/sweep_io.json \\
        --plot   results/loaded_sweep/slack_result.png
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

_BG_SEED_BASE = 1000
_IO_SEED_BASE = 2000

_KT = 1e-3   # ops/s → kTokens


# ---------------------------------------------------------------------------
# Core probe
# ---------------------------------------------------------------------------

def run_probe(
    bg_procs:     int,
    bg_io_mix:    float,
    bg_intensity: float,
    n_io:         int,
    io_intensity: float,
    duration:     int,
    tmp_dir:      str,
    worker_bin:   str,
) -> tuple[float, float]:
    """Run bg + I/O workers concurrently; return (bg_tput, io_tput) in ops/s."""
    def make_cmd(io_mix: float, intensity: float, seed: int) -> list[str]:
        return [worker_bin,
                "--io-mix",    str(io_mix),
                "--intensity", str(intensity),
                "--duration",  str(duration),
                "--tmp-dir",   tmp_dir,
                "--seed",      str(seed)]

    procs: list[subprocess.Popen] = []
    for i in range(bg_procs):
        procs.append(subprocess.Popen(
            make_cmd(bg_io_mix, bg_intensity, _BG_SEED_BASE + i),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
    for i in range(n_io):
        procs.append(subprocess.Popen(
            make_cmd(1.0, io_intensity, _IO_SEED_BASE + i),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))

    bg_tput = io_tput = 0.0
    for idx, p in enumerate(procs):
        stdout, _ = p.communicate()
        if p.returncode != 0:
            print(f"\n[sweep-io] Worker {idx} exited non-zero — skipping sample")
            continue
        try:
            data = json.loads(stdout.strip())
            if idx < bg_procs:
                bg_tput += data.get("throughput", 0.0)
            else:
                io_tput += data.get("io_throughput", 0.0)
        except (json.JSONDecodeError, KeyError):
            pass

    return bg_tput, io_tput


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def sweep(
    bg_procs:     int,
    bg_io_mix:    float,
    bg_intensity: float,
    duration:     int,
    tmp_dir:      str,
    worker_bin:   str,
    drop_pct:     float = 0.05,
    max_io_procs: int   = 64,
    binary_steps: int   = 5,
) -> dict:
    os.makedirs(tmp_dir, exist_ok=True)
    kw = dict(bg_procs=bg_procs, bg_io_mix=bg_io_mix, bg_intensity=bg_intensity,
              duration=duration, tmp_dir=tmp_dir, worker_bin=worker_bin)

    # ------------------------------------------------------------------
    # Phase 0: baseline
    # ------------------------------------------------------------------
    print("--- Phase 0: Baseline (background workers only) ---")
    baseline_tput, _ = run_probe(n_io=0, io_intensity=0.0, **kw)
    threshold = baseline_tput * (1.0 - drop_pct)
    print(f"  Baseline bg : {baseline_tput*_KT:,.3f} kTokens/s")
    print(f"  Threshold   : {threshold*_KT:,.3f} kTokens/s  (drop >= {drop_pct*100:.0f}%)")

    # ------------------------------------------------------------------
    # Phase 1: linear sweep
    # ------------------------------------------------------------------
    print("\n--- Phase 1: Linear I/O sweep ---")
    print(f"  {'IO wkrs':>7}  {'bg (kT/s)':>12}  {'io (kT/s)':>12}  {'status':}")
    print(f"  {'-------':>7}  {'---------':>12}  {'---------':>12}")

    phase1: list[dict] = []
    n_full = 0

    for n_io in range(1, max_io_procs + 1):
        bg_tput, io_tput = run_probe(n_io=n_io, io_intensity=1.0, **kw)
        interfered = bg_tput < threshold
        status = "INTERFERENCE" if interfered else "ok"
        print(f"  {n_io:>7d}  {bg_tput*_KT:>12.3f}  {io_tput*_KT:>12.3f}  {status}")
        phase1.append(dict(n_io=n_io, bg_ktokens=bg_tput*_KT, io_ktokens=io_tput*_KT,
                           interfered=interfered))
        if interfered:
            n_full = n_io - 1
            break
        n_full = n_io
    else:
        print(f"\n  Reached max_io_procs={max_io_procs} without interference.")

    # ------------------------------------------------------------------
    # Phase 2: binary search on fractional last worker
    # ------------------------------------------------------------------
    print(f"\n--- Phase 2: Binary search  (locked: {n_full} × intensity=1.0) ---")
    print(f"  {'step':>4}  {'intensity':>9}  {'bg (kT/s)':>12}  {'io (kT/s)':>12}  {'status':}")
    print(f"  {'----':>4}  {'---------':>9}  {'---------':>12}  {'---------':>12}")

    low, high = 0.0, 1.0
    best_intensity  = 0.0
    best_io_ktokens = 0.0
    phase2: list[dict] = []

    for step in range(1, binary_steps + 1):
        mid = (low + high) / 2.0
        bg_tput, io_tput = run_probe(n_io=n_full + 1, io_intensity=mid, **kw)
        interfered = bg_tput < threshold
        status = "interferes" if interfered else "ok"
        print(f"  {step:>4d}  {mid:>9.3f}  {bg_tput*_KT:>12.3f}  {io_tput*_KT:>12.3f}  {status}")
        phase2.append(dict(step=step, intensity=mid,
                           bg_ktokens=bg_tput*_KT, io_ktokens=io_tput*_KT,
                           interfered=interfered))
        if not interfered:
            best_intensity  = mid
            best_io_ktokens = io_tput * _KT
            low  = mid
        else:
            high = mid

    # I/O slack in kTokens = io throughput at the best safe point
    # (n_full full workers + 1 at best_intensity)
    io_slack_ktokens = best_io_ktokens

    print(f"\n  I/O slack: {n_full} full worker(s) + 1 at intensity {best_intensity:.3f}")
    if n_full == 0 and best_intensity == 0.0:
        print("  (background is saturated — even a single low-intensity I/O worker interferes)")

    return dict(
        type                  = "sweep_io_loaded",
        # kTokens summary
        baseline_bg_ktokens   = baseline_tput * _KT,
        io_slack_ktokens      = io_slack_ktokens,
        # raw
        baseline_tput         = baseline_tput,
        interference_threshold= threshold,
        drop_pct              = drop_pct,
        bg_procs              = bg_procs,
        bg_io_mix             = bg_io_mix,
        bg_intensity          = bg_intensity,
        io_slack_full         = n_full,
        io_slack_partial      = best_intensity,
        # probe data for plotting
        phase1_probes         = phase1,
        phase2_probes         = phase2,
    )


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def plot_slack_result(result: dict, out_path: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
    except ImportError:
        print("[sweep-io] matplotlib not installed — skipping plot", file=sys.stderr)
        return

    baseline_kt = result["baseline_bg_ktokens"]
    threshold_kt = result["interference_threshold"] * _KT
    slack_kt     = result["io_slack_ktokens"]
    n_full       = result["io_slack_full"]
    partial      = result["io_slack_partial"]
    p1           = result["phase1_probes"]
    p2           = result["phase2_probes"]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle("I/O Sweep Under Background Load — Slack Result",
                 fontsize=12, fontweight="bold")

    # ---- Panel 1: Phase 1 linear sweep ------------------------------------
    ax = axes[0]
    if p1:
        x  = [0]          + [d["n_io"]        for d in p1]
        bg = [baseline_kt] + [d["bg_ktokens"]  for d in p1]
        io = [0.0]         + [d["io_ktokens"]  for d in p1]

        ax.plot(x, bg, "o-", color="#4c72b0", label="bg throughput",  linewidth=2, markersize=5)
        ax.plot(x, io, "s-", color="#dd8452", label="io throughput",  linewidth=2, markersize=5)
        ax.axhline(baseline_kt,  color="#2ca02c", linestyle="--", linewidth=1.4,
                   label=f"baseline ({baseline_kt:.2f} kT/s)")
        ax.axhline(threshold_kt, color="#c44e52", linestyle=":",  linewidth=1.4,
                   label=f"threshold ({threshold_kt:.2f} kT/s, −{result['drop_pct']*100:.0f}%)")

        # Mark interference point
        interf = [d for d in p1 if d["interfered"]]
        if interf:
            xi = interf[0]["n_io"]
            ax.axvline(xi, color="#c44e52", linestyle="--", alpha=0.4)
            ax.annotate(f"interference\nat {xi} IO worker(s)",
                        xy=(xi, threshold_kt), xytext=(xi + 0.3, threshold_kt * 1.08),
                        fontsize=8, color="#c44e52",
                        arrowprops=dict(arrowstyle="->", color="#c44e52", lw=1))

        ax.set_xlabel("Number of I/O sweep workers")
        ax.set_ylabel("Throughput (kTokens/s)")
        ax.set_title("Phase 1 — Linear sweep", fontsize=10, loc="left")
        ax.legend(fontsize=8)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)

    # ---- Panel 2: Phase 2 binary search -----------------------------------
    ax = axes[1]
    if p2:
        x2  = [d["intensity"]   for d in p2]
        bg2 = [d["bg_ktokens"]  for d in p2]
        io2 = [d["io_ktokens"]  for d in p2]
        ok  = [not d["interfered"] for d in p2]

        # Plot points with colour by ok/interferes
        for xi, bgi, ioi, is_ok in zip(x2, bg2, io2, ok):
            c = "#4c72b0" if is_ok else "#c44e52"
            ax.scatter(xi, bgi, color=c, zorder=5, s=60)
            ax.scatter(xi, ioi, color="#dd8452", marker="s", zorder=5, s=60)

        # Connect dots in step order
        ax.plot(x2, bg2, "-",  color="#4c72b0", alpha=0.4, linewidth=1)
        ax.plot(x2, io2, "-",  color="#dd8452", alpha=0.4, linewidth=1)

        ax.axhline(baseline_kt,  color="#2ca02c", linestyle="--", linewidth=1.4,
                   label=f"baseline ({baseline_kt:.2f} kT/s)")
        ax.axhline(threshold_kt, color="#c44e52", linestyle=":",  linewidth=1.4,
                   label=f"threshold ({threshold_kt:.2f} kT/s)")

        if partial > 0:
            ax.axvline(partial, color="#9467bd", linestyle="--", linewidth=1.4,
                       label=f"best intensity = {partial:.3f}")
            ax.annotate(f"io slack\n{slack_kt:.3f} kT/s",
                        xy=(partial, slack_kt),
                        xytext=(partial + 0.05, slack_kt * 1.1),
                        fontsize=8, color="#9467bd",
                        arrowprops=dict(arrowstyle="->", color="#9467bd", lw=1))

        # Legend entries for scatter colours
        from matplotlib.lines import Line2D
        handles, labels = ax.get_legend_handles_labels()
        handles += [
            Line2D([0], [0], marker="o", color="w", markerfacecolor="#4c72b0",
                   markersize=8, label="bg (ok)"),
            Line2D([0], [0], marker="o", color="w", markerfacecolor="#c44e52",
                   markersize=8, label="bg (interferes)"),
            Line2D([0], [0], marker="s", color="w", markerfacecolor="#dd8452",
                   markersize=8, label="io throughput"),
        ]
        ax.legend(handles=handles, fontsize=8)

        ax.set_xlabel(f"Partial intensity of last I/O worker\n({n_full} full worker(s) locked at 1.0)")
        ax.set_ylabel("Throughput (kTokens/s)")
        ax.set_title("Phase 2 — Binary search", fontsize=10, loc="left")
        ax.set_xlim(0, 1)
        ax.set_ylim(bottom=0)

    # ---- Summary text box -------------------------------------------------
    summary = (
        f"Background load:  {result['bg_procs']} workers  "
        f"io_mix={result['bg_io_mix']}  intensity={result['bg_intensity']}\n"
        f"Baseline bg:      {baseline_kt:.3f} kTokens/s\n"
        f"I/O slack:        {n_full} full worker(s) + 1 × {partial:.3f}  "
        f"→  {slack_kt:.3f} kTokens/s of additional I/O"
    )
    fig.text(0.5, 0.01, summary, ha="center", va="bottom", fontsize=8.5,
             bbox=dict(boxstyle="round,pad=0.4", facecolor="#f0f0f0", alpha=0.8))

    for ax in axes:
        ax.grid(axis="y", linestyle=":", linewidth=0.6, alpha=0.7)
        ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=[0, 0.10, 1, 0.95])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[sweep-io] Plot saved: {out_path}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep I/O workers under background load; stop on background interference.")
    parser.add_argument("--bg-procs",     type=int,   required=True, metavar="N")
    parser.add_argument("--bg-io-mix",    type=float, default=0.3,   metavar="F")
    parser.add_argument("--bg-intensity", type=float, default=0.75,  metavar="F")
    parser.add_argument("--duration",     type=int,   default=30,    metavar="S")
    parser.add_argument("--drop-pct",     type=float, default=0.05,  metavar="F")
    parser.add_argument("--max-io-procs", type=int,   default=64,    metavar="N")
    parser.add_argument("--tmp-dir",      default="/tmp/slack-meter", metavar="DIR")
    parser.add_argument("--worker-bin",   default=WORKER_BIN,        metavar="PATH")
    parser.add_argument("--output",       default=None,              metavar="FILE",
                        help="write JSON result to this file")
    parser.add_argument("--plot",         default=None,              metavar="FILE",
                        help="write slack result figure to this PNG file")
    args = parser.parse_args()

    if not os.path.exists(args.worker_bin):
        print(f"[sweep-io] ERROR: worker binary not found at {args.worker_bin}")
        sys.exit(1)

    print("=" * 60)
    print("  I/O Sweep Under Background Load")
    print("=" * 60)
    print(f"  Background : {args.bg_procs} workers  "
          f"io_mix={args.bg_io_mix}  intensity={args.bg_intensity}")
    print(f"  Probe dur  : {args.duration}s   drop_pct={args.drop_pct*100:.0f}%")
    print(f"  Tmp dir    : {args.tmp_dir}")
    print("=" * 60)

    result = sweep(
        bg_procs     = args.bg_procs,
        bg_io_mix    = args.bg_io_mix,
        bg_intensity = args.bg_intensity,
        duration     = args.duration,
        tmp_dir      = args.tmp_dir,
        worker_bin   = args.worker_bin,
        drop_pct     = args.drop_pct,
        max_io_procs = args.max_io_procs,
    )

    print("\n" + "=" * 60)
    print("  Result")
    print("=" * 60)
    print(f"  Baseline bg throughput : {result['baseline_bg_ktokens']:.3f} kTokens/s")
    print(f"  I/O slack              : {result['io_slack_full']} full worker(s) "
          f"+ 1 at intensity {result['io_slack_partial']:.3f}")
    print(f"  I/O slack throughput   : {result['io_slack_ktokens']:.3f} kTokens/s")
    print("=" * 60)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Exclude raw ops/s fields from JSON to keep it tidy; kTokens suffice
        export = {k: v for k, v in result.items()
                  if k not in ("baseline_tput", "interference_threshold")}
        with open(out, "w") as f:
            json.dump(export, f, indent=2)
        print(f"\nResult written to {args.output}")

    if args.plot:
        plot_slack_result(result, Path(args.plot))


if __name__ == "__main__":
    main()
