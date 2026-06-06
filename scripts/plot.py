#!/usr/bin/env python3
"""
Slack Meter – Plot Building Block
===================================
Two plot families, each callable as a function or from the CLI.

1. Slack sweep plots  (plot_slack_result)
   Visualises probe.py output: probe throughput (S) and bg throughput (R) over sweep.
   Used by probe.py (imported) and run_loaded_sweep.sh (via --plot flag on probe.py).

2. Experiment CSV bar charts  (plot_case / main)
   Reads results/experiment.csv and produces one 4-panel bar-chart figure per
   case group (case1.*, case2.*, …).

Usage — bar charts
------------------
    python3 scripts/plot.py                                  # all case groups
    python3 scripts/plot.py --row 1                          # only case1.*
    python3 scripts/plot.py --csv results/experiment.csv \\
                            --out-dir results/plots

Panels (bar chart, per figure)
-------------------------------
  1. duty_cycle  →  saturation_n
  2. duty_cycle  →  io_slack_added_pct
  3. duty_cycle  →  cpu_slack_added_pct
  4. duty_cycle  →  ram_slack_added_pct

Dependencies
------------
    pip install matplotlib pandas numpy
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Slack sweep plot  (used by probe.py)
# ---------------------------------------------------------------------------

_KT = 1e-3   # ops/s → kTokens/s


def plot_slack_result(result: dict, out_path: Path) -> None:
    """
    Two-panel figure for a probe.py sweep result.
      Left  – probe throughput vs. # probes (with S plateau annotated)
      Right – bg throughput (R) vs. # probes (with B baseline and R-at-plateau annotated)
    """
    ptype       = result["probe_type"].upper()
    baseline_kt = result["baseline_bg_ktokens"]   # B
    slack_kt    = result["slack_ktokens"]          # S
    r_kt        = result["baseline_r_ktokens"]     # R at plateau
    p1          = result["phase1_probes"]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle(f"{ptype} Sweep Under Background Load — Slack Result",
                 fontsize=12, fontweight="bold")

    # Find the probe count at which S was achieved (last non-decreasing peak)
    peak_n = None
    if p1:
        peak_val = -1.0
        for d in p1:
            if d["probe_ktokens"] >= peak_val:
                peak_val = d["probe_ktokens"]
                peak_n   = d["n_probe"]

    # ---- Panel 1: probe throughput (S) ------------------------------------
    ax = axes[0]
    if p1:
        x  = [0]   + [d["n_probe"]       for d in p1]
        pb = [0.0] + [d["probe_ktokens"] for d in p1]

        ax.plot(x, pb, "s-", color="#dd8452", label=f"{ptype.lower()} throughput",
                linewidth=2, markersize=5)
        ax.axhline(slack_kt, color="#9467bd", linestyle="--", linewidth=1.4,
                   label=f"S = {slack_kt:.3f} kT/s (plateau)")

        if peak_n is not None:
            ax.axvline(peak_n, color="#9467bd", linestyle=":", alpha=0.5)
            ax.annotate(f"plateau\nn={peak_n}",
                        xy=(peak_n, slack_kt),
                        xytext=(peak_n + 0.4, slack_kt * 1.08),
                        fontsize=8, color="#9467bd",
                        arrowprops=dict(arrowstyle="->", color="#9467bd", lw=1))

        ax.set_xlabel(f"Number of {ptype} probe workers")
        ax.set_ylabel("Throughput (kTokens/s)")
        ax.set_title(f"Probe throughput → S", fontsize=10, loc="left")
        ax.legend(fontsize=8)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)

    # ---- Panel 2: bg throughput (R) over sweep ----------------------------
    ax = axes[1]
    if p1:
        x  = [0]           + [d["n_probe"]    for d in p1]
        bg = [baseline_kt] + [d["rdb_ktokens"] for d in p1]

        ax.plot(x, bg, "o-", color="#4c72b0", label="bg throughput (R)",
                linewidth=2, markersize=5)
        ax.axhline(baseline_kt, color="#2ca02c", linestyle="--", linewidth=1.4,
                   label=f"B = {baseline_kt:.3f} kT/s (baseline)")

        if peak_n is not None and r_kt > 0:
            ax.axhline(r_kt, color="#c44e52", linestyle=":", linewidth=1.4,
                       label=f"R = {r_kt:.3f} kT/s (at plateau)")
            ax.axvline(peak_n, color="#9467bd", linestyle=":", alpha=0.5)
            ax.annotate(f"R at S\n{r_kt:.3f} kT/s",
                        xy=(peak_n, r_kt),
                        xytext=(peak_n + 0.4, r_kt * 0.92),
                        fontsize=8, color="#c44e52",
                        arrowprops=dict(arrowstyle="->", color="#c44e52", lw=1))

        ax.set_xlabel(f"Number of {ptype} probe workers")
        ax.set_ylabel("Throughput (kTokens/s)")
        ax.set_title("Background throughput → R", fontsize=10, loc="left")
        ax.legend(fontsize=8)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)

    # ---- Summary text box -------------------------------------------------
    cap_kt = result.get("capacity_ktokens")
    ru_pct = result.get("resource_usage_pct")
    summary_lines = []
    # Background load info (RocksDB)
    bg_threads = result.get('rocksdb_bg_threads')
    if bg_threads is not None:
        summary_lines.append(f"Background load:  {bg_threads} RocksDB threads")
    # No explicit io/mem mix for RocksDB background; include spec if available
    bg_spec = result.get('rocksdb_workload_spec')
    if bg_spec:
        summary_lines.append(f"Workload spec: {bg_spec}")
    summary_lines.append(f"Baseline B = {baseline_kt:.3f} kT/s")
    summary_lines.append(f"Slack S = {slack_kt:.3f} kT/s")
    summary_lines.append(f"R at plateau = {r_kt:.3f} kT/s")
    if cap_kt is not None:
        summary_lines.append(f"Capacity C = {cap_kt:.3f} kT/s")
    if ru_pct is not None:
        summary_lines.append(f"Resource usage = (C−S)·B / (C·R) = {ru_pct:.1f}%")
    summary = "\n".join(summary_lines)

    fig.text(0.5, 0.01, summary, ha="center", va="bottom", fontsize=8.5,
             bbox=dict(boxstyle="round,pad=0.4", facecolor="#f0f0f0", alpha=0.8))

    for ax in axes:
        ax.grid(axis="y", linestyle=":", linewidth=0.6, alpha=0.7)
        ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=[0, 0.12, 1, 0.95])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[plot] Slack plot saved: {out_path}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Constants  (experiment CSV bar charts)
# ---------------------------------------------------------------------------

IO_MIX_LABELS: dict[float, str] = {
    0.0: "CPU-Only Workload",
    0.3: "CPU-Heavy Workload with Moderate I/O",
    0.5: "Evenly-Balanced CPU-I/O Workload",
    0.7: "I/O-Heavy Workload with Moderate CPU",
    1.0: "I/O-Focused Workload",
}

# Colour palette – one colour per duty_cycle value (sorted descending: 1.0 → 0.25)
DUTY_COLOURS = {
    1.00: "#1565C0",   # deep blue
    0.75: "#2E7D32",   # deep green
    0.50: "#F57F17",   # amber
    0.25: "#6A1B9A",   # purple
}

FIGURE_DPI  = 150
BAR_WIDTH   = 0.35   # narrower than the 1-unit x spacing so bars have clear gaps
BAR_ALPHA   = 0.88


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _duty_label(v: float) -> str:
    return f"{v:.2f}"


def _io_mix_title(io_mix: float) -> str:
    return IO_MIX_LABELS.get(round(io_mix, 1), f"io_mix={io_mix}")


# ---------------------------------------------------------------------------
# Core plot routine – one 3-panel figure for a single case group
# ---------------------------------------------------------------------------

def plot_case(
    df_case: pd.DataFrame,
    case_prefix: str,
    out_dir: Path,
) -> None:
    """Produce and save a 3-panel figure for *df_case* (rows of one case group).

    X-axis: integer positions 0, 1, 2, … with duty_cycle values as tick labels.
    One coloured bar per duty_cycle; no legend (axis labels are self-describing).
    """

    io_mix      = df_case["io_mix"].iloc[0]
    df_sorted   = df_case.sort_values("duty_cycle", ascending=False)
    duty_cycles = df_sorted["duty_cycle"].tolist()
    tick_labels = [_duty_label(dc) for dc in duty_cycles]
    x_pos       = np.arange(len(duty_cycles))        # integer bar centres

    fig, axes = plt.subplots(1, 4, figsize=(17, 4.5))
    fig.suptitle(
        f"{case_prefix}  ·  {_io_mix_title(io_mix)}  (io_mix={io_mix})",
        fontsize=12, fontweight="bold", y=1.02,
    )

    # -----------------------------------------------------------------------
    # Collect per-bar values
    # -----------------------------------------------------------------------
    sat_vals:   list[float] = []
    io_pct:     list[float] = []
    cpu_cores:  list[float] = []
    cpu_pct:    list[float] = []
    ram_pct:    list[float] = []
    colours:    list[str]   = []

    for dc in duty_cycles:
        row = df_sorted[df_sorted["duty_cycle"] == dc].iloc[0]
        colours.append(DUTY_COLOURS.get(dc, "#888888"))
        sat_vals.append(float(row["saturation_n"]))
        io_pct.append(float(row["io_slack_added_pct"]))
        cpu_cores.append(float(row["cpu_slack_cores"]))
        cpu_pct.append(float(row["cpu_slack_added_pct"]))
        ram_pct.append(float(row.get("ram_slack_added_pct", 0.0)))

    # -----------------------------------------------------------------------
    # Shared x-axis helper
    # -----------------------------------------------------------------------
    def _fmt_xax(ax: plt.Axes) -> None:
        ax.set_xticks(x_pos)
        ax.set_xticklabels(tick_labels, fontsize=9)
        ax.set_xlabel("duty_cycle", fontsize=10)
        ax.set_xlim(-0.6, len(duty_cycles) - 0.4)

    # -----------------------------------------------------------------------
    # Panel 1 – saturation_n
    # -----------------------------------------------------------------------
    ax1 = axes[0]
    for xi, (val, col) in enumerate(zip(sat_vals, colours)):
        ax1.bar(xi, val, BAR_WIDTH, color=col, alpha=BAR_ALPHA)
        ax1.text(xi, val + 0.15, f"{val:.0f}",
                 ha="center", va="bottom", fontsize=8, color=col, fontweight="bold")

    _fmt_xax(ax1)
    ax1.set_ylabel("Saturation Processes", fontsize=10)
    ax1.set_title("Saturation Point", fontsize=11, fontweight="bold")
    ax1.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax1.set_ylim(bottom=0, top=max(sat_vals) * 1.25)
    ax1.grid(axis="y", alpha=0.3)

    # Dynamic y-axis: panels 2 & 3 share the same upper bound so they are
    # directly comparable.  Headroom = 12 % above the tallest bar across both
    # panels; floor at 10 % so near-zero cases still look reasonable.
    def _pct_ymax(vals: list[float]) -> float:
        peak = max(vals) if vals else 0.0
        return max(peak * 1.12, 0.10)

    shared_pct_ymax = max(_pct_ymax(io_pct), _pct_ymax(cpu_pct), _pct_ymax(ram_pct))

    # -----------------------------------------------------------------------
    # Panel 2 – io_slack_added_pct
    # -----------------------------------------------------------------------
    ax2 = axes[1]
    for xi, (val, col) in enumerate(zip(io_pct, colours)):
        ax2.bar(xi, val, BAR_WIDTH, color=col, alpha=BAR_ALPHA)
        ax2.text(xi, val + 0.003, f"{val:.1%}",
                 ha="center", va="bottom", fontsize=7.5, color=col, fontweight="bold")

    _fmt_xax(ax2)
    ax2.set_ylabel("Slack Added (fraction of peak xput)", fontsize=10)
    ax2.set_title("I/O Slack", fontsize=11, fontweight="bold")
    ax2.set_ylim(0.0, shared_pct_ymax)
    ax2.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1.0))
    ax2.grid(axis="y", alpha=0.3)

    # -----------------------------------------------------------------------
    # Panel 3 – cpu_slack_added_pct only (bars, same scale as panel 2)
    # -----------------------------------------------------------------------
    ax3 = axes[2]
    for xi, (pct, col) in enumerate(zip(cpu_pct, colours)):
        ax3.bar(xi, pct, BAR_WIDTH, color=col, alpha=BAR_ALPHA)
        ax3.text(xi, pct + 0.003, f"{pct:.1%}",
                 ha="center", va="bottom", fontsize=7.5, color=col, fontweight="bold")

    _fmt_xax(ax3)
    ax3.set_ylabel("Slack Added (fraction of peak xput)", fontsize=10)
    ax3.set_title("CPU Slack", fontsize=11, fontweight="bold")
    ax3.set_ylim(0.0, shared_pct_ymax)
    ax3.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1.0))
    ax3.grid(axis="y", alpha=0.3)

    # -----------------------------------------------------------------------
    # Panel 4 – ram_slack_added_pct
    # -----------------------------------------------------------------------
    ax4 = axes[3]
    for xi, (pct, col) in enumerate(zip(ram_pct, colours)):
        ax4.bar(xi, pct, BAR_WIDTH, color=col, alpha=BAR_ALPHA)
        ax4.text(xi, pct + 0.003, f"{pct:.1%}",
                 ha="center", va="bottom", fontsize=7.5, color=col, fontweight="bold")

    _fmt_xax(ax4)
    ax4.set_ylabel("Slack Added (fraction of peak xput)", fontsize=10)
    ax4.set_title("RAM Slack", fontsize=11, fontweight="bold")
    ax4.set_ylim(0.0, shared_pct_ymax)
    ax4.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=1.0))
    ax4.grid(axis="y", alpha=0.3)

    # -----------------------------------------------------------------------
    # Save
    # -----------------------------------------------------------------------
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{case_prefix}_slack_summary.png"
    fig.savefig(out_path, dpi=FIGURE_DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot] Saved → {out_path}")


# ---------------------------------------------------------------------------
# Fungibility matrix plotter
# ---------------------------------------------------------------------------

def plot_fungibility_matrix(df: pd.DataFrame, out_path: Path) -> None:
    """
    Plots a fungibility matrix showing Normalized App Footprint (%)
    across different probe modes and background modes/intensities.
    """
    probe_modes = df["probe_mode"].unique().tolist()
    is_cpu = any("cpu" in str(m).lower() or m in ["cpu_int", "cpu_fp", "cpu_hash"] for m in df["bg_mode"].unique().tolist() + probe_modes)
    title_prefix = "CPU " if is_cpu else ("Memory " if any("mem" in str(m).lower() for m in probe_modes) else "I/O ")

    fig, ax = plt.subplots(figsize=(11, 6))

    colors = ['#1565C0', '#E64A19', '#2E7D32', '#6A1B9A', '#c44e52', '#8172b3', '#937860', '#da8bc3']

    if "bg_intensity" in df.columns:
        # Grouped bar chart for varying intensity
        intensities = sorted(df["bg_intensity"].unique().tolist())
        data = {pr: {} for pr in probe_modes}
        for _, row in df.iterrows():
            pr = row["probe_mode"]
            intensity = float(row["bg_intensity"])
            footprint = float(row["footprint_pct"])
            data[pr][intensity] = footprint

        x = np.arange(len(intensities))
        num_probes = len(probe_modes)
        width = 0.7 / max(num_probes, 1)

        for i, probe_mode in enumerate(probe_modes):
            y_vals = [data[probe_mode].get(intensity, 0.0) for intensity in intensities]
            offset = (i - (num_probes - 1) / 2.0) * width
            color = colors[i % len(colors)]
            ax.bar(x + offset, y_vals, width, label=f"Probe: {probe_mode}", 
                   color=color, edgecolor="white", alpha=0.88, zorder=3)

        ax.set_xticks(x)
        ax.set_xticklabels([f"BG Intensity:\n{int(intensity * 100)}%" for intensity in intensities], fontsize=10)
    else:
        # Grouped bar chart for varying background modes
        bg_modes = df["bg_mode"].unique().tolist()
        data = {bg: {pr: 0.0 for pr in probe_modes} for bg in bg_modes}
        for _, row in df.iterrows():
            bg = row["bg_mode"]
            pr = row["probe_mode"]
            if bg in data and pr in data[bg]:
                data[bg][pr] = float(row["footprint_pct"])

        x = np.arange(len(bg_modes))
        num_probes = len(probe_modes)
        width = 0.7 / max(num_probes, 1)

        for i, probe_mode in enumerate(probe_modes):
            y_vals = [data[bg_mode][probe_mode] for bg_mode in bg_modes]
            offset = (i - (num_probes - 1) / 2.0) * width
            color = colors[i % len(colors)]
            ax.bar(x + offset, y_vals, width, label=f"Probe: {probe_mode}", 
                   color=color, edgecolor="white", alpha=0.88, zorder=3)

        ax.set_xticks(x)
        ax.set_xticklabels([f"BG Workload:\n{m}" for m in bg_modes], fontsize=10)

    ax.set_ylabel("Normalized App Footprint (%)\n(Capacity - Slack) / Capacity", fontsize=11, fontweight="bold")
    ax.set_title(f"{title_prefix}Unit of Measure Fungibility:\nFootprint Measurement Invariance Across Different Probes", 
                 fontsize=12, fontweight="bold", pad=15)
    ax.legend(title="Unit of Measure (Probe)", loc="upper left", bbox_to_anchor=(1, 1))

    min_pct = df["footprint_pct"].min()
    max_pct = df["footprint_pct"].max()
    min_y = min(0.0, min_pct - 5.0) if not pd.isna(min_pct) else 0.0
    max_y = max(105.0, max_pct + 5.0) if not pd.isna(max_pct) else 105.0
    ax.set_ylim(min_y, max_y)

    ax.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=100.0))
    ax.grid(True, linestyle=':', alpha=0.7)
    ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    print(f"[plot] Saved fungibility matrix plot → {out_path}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Slack Meter CSV Plotter")
    parser.add_argument(
        "--csv", default="results/experiment.csv",
        help="Path to the experiment CSV file (default: results/experiment.csv)",
    )
    parser.add_argument(
        "--out-dir", default="results/plots",
        help="Directory to write PNG figures into (default: results/plots)",
    )
    parser.add_argument(
        "--row", type=int, default=None, metavar="N",
        help="Plot only case<N>.* rows (1–5).  Omit to plot all 5 cases.",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"[ERROR] CSV not found: {csv_path}")

    df = pd.read_csv(csv_path)

    # If it is a fungibility matrix results CSV
    if "bg_mode" in df.columns and "probe_mode" in df.columns:
        if "footprint_pct" in df.columns:
            df["footprint_pct"] = pd.to_numeric(df["footprint_pct"], errors="coerce")
        out_path = Path(args.out_dir) / "fungibility_plot.png"
        plot_fungibility_matrix(df, out_path)
        
        # Also save to the CSV directory if it's different from out_dir
        csv_dir = csv_path.parent
        if csv_dir.resolve() != Path(args.out_dir).resolve():
            alt_path = csv_dir / "fungibility_plot.png"
            try:
                plot_fungibility_matrix(df, alt_path)
            except Exception as e:
                print(f"[Warning] Could not save copy to {alt_path}: {e}")
        return

    # Coerce numeric columns (guards against stray commas like "0.953,,0.4303")
    for col in ["io_mix", "duty_cycle", "saturation_n",
                "cpu_slack_cores", "cpu_slack_added_pct",
                "io_slack_cores", "io_slack_added_pct",
                "ram_slack_cores", "ram_slack_added_pct"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(subset=["io_mix", "duty_cycle", "saturation_n"], inplace=True)

    out_dir = Path(args.out_dir)

    # Determine which case groups to plot
    if args.row is not None:
        prefixes = [f"case{args.row}"]
    else:
        # Discover all caseN prefixes present in the data
        prefixes = sorted({
            row.split(".")[0]
            for row in df["case"].astype(str)
            if "." in row
        })

    for prefix in prefixes:
        mask = df["case"].astype(str).str.startswith(prefix + ".")
        df_case = df[mask].copy()
        if df_case.empty:
            print(f"[WARN] No rows found for prefix '{prefix}' — skipping.")
            continue
        plot_case(df_case, prefix, out_dir)

    print(f"\n[done] Figures written to {out_dir}/")


if __name__ == "__main__":
    main()
