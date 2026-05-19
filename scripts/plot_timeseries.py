#!/usr/bin/env python3
"""
plot_timeseries.py
==================
Parses the raw iostat / vmstat output produced by run_timeseries.sh,
writes iostat.csv, and renders a four-panel time-series PNG.

Panels
------
  1. CPU breakdown  — user, sys, iowait, idle  (from vmstat)
  2. Disk IOPS      — r/s, w/s                 (from iostat)
  3. Disk bandwidth — rkB/s, wkB/s             (from iostat)
  4. Disk util/wait — %util, await             (from iostat)

Usage
-----
    python3 scripts/plot_timeseries.py --output-dir results/timeseries \\
                                       [--device sda]                  \\
                                       [--interval 1]                  \\
                                       [--nprocs 4]                    \\
                                       [--io-mix 0.3]                  \\
                                       [--intensity 0.75]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Optional

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


# ---------------------------------------------------------------------------
# vmstat parser
# ---------------------------------------------------------------------------

def parse_vmstat_raw(path: Path) -> tuple[list[str], list[dict]]:
    """
    Parse the raw text written by `vmstat -n interval N`.

    Format (vmstat -n suppresses repeated headers):

        procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
         r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
         0  0    512 189281600 ...
         0  0    512 189281408 ...

    Line 1  – section labels  (skipped)
    Line 2  – column names    (used as CSV headers, stripped + split)
    Lines 3+ – data rows

    Returns (headers, rows).  'st' (steal) is absent on bare-metal hosts;
    the parser is column-count–agnostic so it handles both cases.
    """
    headers: list[str] = []
    rows: list[dict] = []

    if not path.exists():
        print(f"[plot] WARNING: {path} not found — skipping vmstat panels", file=sys.stderr)
        return headers, rows

    with open(path) as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            if lineno == 1:
                # Section label row — skip
                continue
            if lineno == 2:
                # Column-name row: "r  b  swpd  free ..."
                headers = line.split()
                continue
            # Repeated header lines (vmstat without -n) start with a letter
            if line and not line[0].isdigit() and not line[0] == ' ':
                if line.split()[0].isalpha():
                    continue
            # Data row
            parts = line.split()
            if not parts or not parts[0].lstrip('-').isdigit():
                continue
            if not headers:
                continue
            row: dict = {}
            for col, val in zip(headers, parts):
                try:
                    row[col] = float(val)
                except ValueError:
                    pass
            if row:
                rows.append(row)

    return headers, rows


def write_vmstat_csv(headers: list[str], rows: list[dict], path: Path) -> None:
    """Persist parsed vmstat rows as a tidy CSV."""
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    return headers, rows


# ---------------------------------------------------------------------------
# iostat parser
# ---------------------------------------------------------------------------

def parse_iostat_raw(path: Path, device: Optional[str] = None) -> tuple[list[str], list[dict]]:
    """
    Parse the raw text written by `iostat -x -d -y [-t] interval N`.

    The file looks like (with -t):

        Linux 5.x ...  (hostname)  MM/DD/YYYY  _x86_64_  (N CPU)

        MM/DD/YYYY HH:MM:SS AM
        Device    r/s  w/s  rkB/s  wkB/s  ...  %util
        sda       0.0  2.0   0.0   16.0   ...   1.6

        MM/DD/YYYY HH:MM:SS AM
        Device    r/s  w/s  ...
        sda       0.0  4.0  ...

    Without -t the timestamp lines are absent.

    Returns (headers, rows) where each row is {col: float, '_sample': int}.
    The Device column is kept as a string.
    """
    if not path.exists():
        print(f"[plot] WARNING: {path} not found — skipping iostat panels", file=sys.stderr)
        return [], []

    headers: list[str] = []
    rows: list[dict] = []
    sample = 0
    current_ts: Optional[str] = None
    in_device_block = False

    # Regex for a timestamp line produced by iostat -t
    ts_re = re.compile(r"^\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}")

    with open(path) as f:
        for raw_line in f:
            line = raw_line.rstrip()

            if not line:
                in_device_block = False
                continue

            # Skip the Linux banner line
            if line.startswith("Linux"):
                continue

            # Timestamp line (iostat -t)
            if ts_re.match(line):
                current_ts = line.strip()
                in_device_block = False
                continue

            # Device header line — starts a new sample block
            if re.match(r"\s*Device\b", line, re.IGNORECASE):
                headers = line.split()
                in_device_block = True
                sample += 1
                continue

            # Data line inside a device block
            if in_device_block and headers:
                parts = line.split()
                if not parts:
                    continue
                if len(parts) != len(headers):
                    # Some iostat versions emit a different column count on the
                    # first sample; skip mismatched lines rather than crashing.
                    continue

                dev_name = parts[0]
                if device and dev_name != device:
                    continue

                row: dict = {"Device": dev_name, "_sample": sample, "_ts": current_ts or ""}
                for col, val in zip(headers[1:], parts[1:]):
                    try:
                        row[col] = float(val)
                    except ValueError:
                        row[col] = 0.0
                rows.append(row)

    # Build ordered numeric headers (excluding Device, _sample, _ts)
    num_headers = [h for h in headers[1:] if h not in ("Device",)]
    return num_headers, rows


def write_iostat_csv(headers: list[str], rows: list[dict], path: Path) -> None:
    """Persist parsed iostat rows as a tidy CSV."""
    all_cols = ["_sample", "_ts", "Device"] + headers
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=all_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def _time_axis(n: int, interval: int) -> list[float]:
    return [i * interval for i in range(n)]


def plot(
    vmstat_rows: list[dict],
    iostat_rows: list[dict],
    interval: int,
    device: str,
    out_path: Path,
    title_extra: str = "",
) -> None:
    if not HAS_MPL:
        print("[plot] matplotlib not installed — skipping plot (pip install matplotlib)", file=sys.stderr)
        return

    fig, axes = plt.subplots(4, 1, figsize=(12, 14), sharex=True)
    fig.suptitle(
        f"Workload time series{(' — ' + title_extra) if title_extra else ''}",
        fontsize=13, fontweight="bold", y=0.98,
    )

    # ---- Panel 1: CPU breakdown (vmstat) ----------------------------------
    ax = axes[0]
    if vmstat_rows:
        t = _time_axis(len(vmstat_rows), interval)
        us = [r.get("us", 0) for r in vmstat_rows]
        sy = [r.get("sy", 0) for r in vmstat_rows]
        wa = [r.get("wa", 0) for r in vmstat_rows]
        id_ = [r.get("id", 0) for r in vmstat_rows]
        ax.stackplot(t, us, sy, wa, id_,
                     labels=["user", "sys", "iowait", "idle"],
                     colors=["#4c72b0", "#dd8452", "#c44e52", "#cccccc"],
                     alpha=0.85)
        ax.set_ylabel("CPU %")
        ax.set_ylim(0, 100)
        ax.legend(loc="upper right", ncol=4, fontsize=8)
        ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%g%%"))
    else:
        ax.text(0.5, 0.5, "No vmstat data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title("CPU breakdown (vmstat)", fontsize=10, loc="left")

    # ---- Panel 2: Disk IOPS (iostat) --------------------------------------
    ax = axes[1]
    if iostat_rows:
        samples = sorted(set(r["_sample"] for r in iostat_rows))
        # Average across devices if multiple (unlikely when DEVICE is set)
        def avg_by_sample(col: str) -> list[float]:
            out = []
            for s in samples:
                vals = [r.get(col, 0.0) for r in iostat_rows if r["_sample"] == s]
                out.append(sum(vals) / len(vals) if vals else 0.0)
            return out

        t = _time_axis(len(samples), interval)
        r_s = avg_by_sample("r/s")
        w_s = avg_by_sample("w/s")
        ax.plot(t, r_s, label="r/s",  color="#4c72b0", linewidth=1.5)
        ax.plot(t, w_s, label="w/s",  color="#dd8452", linewidth=1.5)
        ax.fill_between(t, r_s, alpha=0.15, color="#4c72b0")
        ax.fill_between(t, w_s, alpha=0.15, color="#dd8452")
        ax.set_ylabel("IOPS")
        ax.legend(loc="upper right", fontsize=8)
    else:
        ax.text(0.5, 0.5, "No iostat data", ha="center", va="center", transform=ax.transAxes)
    dev_label = f" ({device})" if device else ""
    ax.set_title(f"Disk IOPS{dev_label} (iostat)", fontsize=10, loc="left")

    # ---- Panel 3: Disk bandwidth (iostat) ---------------------------------
    ax = axes[2]
    if iostat_rows:
        rk = avg_by_sample("rkB/s")
        wk = avg_by_sample("wkB/s")
        # Convert to MB/s for readability if values are large
        scale, unit = (1024.0, "MB/s") if max(rk + wk, default=0) > 2048 else (1.0, "kB/s")
        ax.plot(t, [v / scale for v in rk], label=f"read {unit}",  color="#4c72b0", linewidth=1.5)
        ax.plot(t, [v / scale for v in wk], label=f"write {unit}", color="#dd8452", linewidth=1.5)
        ax.fill_between(t, [v / scale for v in rk], alpha=0.15, color="#4c72b0")
        ax.fill_between(t, [v / scale for v in wk], alpha=0.15, color="#dd8452")
        ax.set_ylabel(unit)
        ax.legend(loc="upper right", fontsize=8)
    else:
        ax.text(0.5, 0.5, "No iostat data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(f"Disk bandwidth{dev_label} (iostat)", fontsize=10, loc="left")

    # ---- Panel 4: %util and await (iostat) --------------------------------
    ax = axes[3]
    ax2 = ax.twinx()
    if iostat_rows:
        util = avg_by_sample("%util")
        # await may appear as 'await', 'r_await', or be absent
        # Prefer w_await (write latency) — the workload is write-heavy.
        # If the sysstat version only has a generic 'await', fall back to that.
        # r_await is deliberately deprioritised: with no reads it's always 0.
        has = lambda c: any(c in r for r in iostat_rows)
        w_await_col = "w_await" if has("w_await") else ("await" if has("await") else None)
        r_await_col = "r_await" if has("r_await") else None

        ax.plot(t, util, label="%util", color="#2ca02c", linewidth=1.5)
        ax.fill_between(t, util, alpha=0.15, color="#2ca02c")
        ax.set_ylabel("%util", color="#2ca02c")
        ax.set_ylim(0, 105)
        ax.tick_params(axis="y", labelcolor="#2ca02c")

        plotted_await = False
        if w_await_col:
            aw = avg_by_sample(w_await_col)
            ax2.plot(t, aw, label=f"{w_await_col} (ms)", color="#9467bd",
                     linewidth=1.5, linestyle="--")
            plotted_await = True
        if r_await_col and r_await_col != w_await_col:
            aw_r = avg_by_sample(r_await_col)
            # Only plot r_await if it has non-zero values (reads may be absent)
            if any(v > 0 for v in aw_r):
                ax2.plot(t, aw_r, label=f"{r_await_col} (ms)", color="#e377c2",
                         linewidth=1.2, linestyle=":")
                plotted_await = True

        if plotted_await:
            ax2.set_ylabel("await (ms)", color="#9467bd")
            ax2.tick_params(axis="y", labelcolor="#9467bd")
            lines1, labels1 = ax.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8)
        else:
            ax.legend(loc="upper right", fontsize=8)
    else:
        ax.text(0.5, 0.5, "No iostat data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(f"Disk utilisation & latency{dev_label} (iostat)", fontsize=10, loc="left")

    # ---- Shared x-axis label ----------------------------------------------
    axes[-1].set_xlabel("Elapsed time (s)")
    for a in axes:
        a.grid(axis="y", linestyle=":", linewidth=0.6, alpha=0.7)
        a.spines[["top", "right"]].set_visible(False)

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[plot] Saved: {out_path}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Parse iostat/vmstat output and plot time series.")
    parser.add_argument("--output-dir", default="results/timeseries", metavar="DIR")
    parser.add_argument("--device",     default="",  metavar="DEV",
                        help="Block device name to isolate in iostat (e.g. sda, nvme0n1)")
    parser.add_argument("--interval",   type=int, default=1, metavar="S",
                        help="Sampling interval used when collecting (seconds)")
    parser.add_argument("--nprocs",     type=int, default=0,  metavar="N")
    parser.add_argument("--io-mix",     type=float, default=0.3, metavar="F")
    parser.add_argument("--intensity",  type=float, default=0.75, metavar="F")
    args = parser.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # -- vmstat --------------------------------------------------------------
    vmstat_headers, vmstat_rows = parse_vmstat_raw(out / "vmstat_raw.txt")
    print(f"[plot] vmstat: {len(vmstat_rows)} samples")
    if vmstat_rows:
        write_vmstat_csv(vmstat_headers, vmstat_rows, out / "vmstat.csv")
        print(f"[plot] Wrote {out / 'vmstat.csv'}")

    # -- iostat --------------------------------------------------------------
    iostat_headers, iostat_rows = parse_iostat_raw(
        out / "iostat_raw.txt",
        device=args.device or None,
    )
    print(f"[plot] iostat: {len(iostat_rows)} device-sample rows "
          f"({'device=' + args.device if args.device else 'all devices'})")

    if iostat_rows:
        write_iostat_csv(iostat_headers, iostat_rows, out / "iostat.csv")
        print(f"[plot] Wrote {out / 'iostat.csv'}")

    # -- Plot ----------------------------------------------------------------
    extra = ""
    if args.nprocs:
        extra = (f"{args.nprocs} worker{'s' if args.nprocs != 1 else ''}  "
                 f"io_mix={args.io_mix}  intensity={args.intensity}")

    plot(
        vmstat_rows=vmstat_rows,
        iostat_rows=iostat_rows,
        interval=args.interval,
        device=args.device,
        out_path=out / "timeseries.png",
        title_extra=extra,
    )

    if not vmstat_rows and not iostat_rows:
        print("[plot] No data found — nothing to plot.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
