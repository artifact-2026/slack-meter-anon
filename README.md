# slack-meter

Empirically measures CPU and I/O slack in a resource-constrained system.

The core idea: saturate a system with a mixed baseline workload, then determine
which resource is the bottleneck by measuring how much "pure" CPU-only or
I/O-only work can be added before the baseline's throughput drops. See
[theidea.md](theidea.md) for the full methodology.

---

## Prerequisites

On Cloudlab node (Ubuntu 22.04):

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake python3 python3-pip
pip3 install matplotlib numpy
```

To run the containerised experiments:

```bash
sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER   # then re-login
```

---

## Build

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel $(nproc)
```

This produces a single binary: `build/worker`.

---

## Run

### Quick start (full pipeline)

```bash
bash scripts/run_experiment.sh
```

This builds, runs the full experiment, generates an HTML report, and opens it.
Tune behaviour with environment variables:

| Variable    | Default          | Description                              |
|-------------|------------------|------------------------------------------|
| `DURATION`  | `30`             | Seconds each worker probe runs           |
| `MAX_PROCS` | `32`             | Max processes in the saturation sweep    |
| `TMP_DIR`   | `/tmp/slack-meter` | Scratch directory for I/O operations   |
| `MODE`      | `full`           | `saturation`, `slack-cpu`, `slack-io`, or `full` |

Example — faster iteration run:

```bash
DURATION=10 MAX_PROCS=8 bash scripts/run_experiment.sh
```

---

### Step by step

**1. Saturation sweep only**

```bash
python3 scripts/orchestrate.py \
    --mode saturation \
    --duration 30 \
    --max-procs 32 \
    --output results/experiment.json
```

Ramps up baseline processes (`io_mix=0.3, intensity=0.75`) until throughput
peaks and drops. Writes the saturation point and per-process throughput data
to the results file.

**2. Slack measurement only** (after a saturation run exists)

```bash
# CPU slack
python3 scripts/orchestrate.py --mode slack-cpu --output results/experiment.json

# I/O slack
python3 scripts/orchestrate.py --mode slack-io  --output results/experiment.json
```

Uses a hybrid binary search on intensity to find the minimum CPU-only (or
I/O-only) load that causes baseline throughput to drop by ≥5%.

**3. Generate the HTML report**

```bash
python3 scripts/report.py results/experiment.json --report results/report.html
```

Produces a self-contained HTML file with embedded plots — no server needed,
just open it in a browser.

---

### Docker

The container runs with 4 cores and 400 MB/s read+write I/O. The root block
device is auto-detected — just run:

```bash
bash infra/run.sh
```

If auto-detection picks the wrong device, override it:

```bash
BLOCK_DEVICE=/dev/nvme0n1 bash infra/run.sh
```

Results land in `results/experiment.json`.

---

## Output

All results go to `results/` (gitignored):

```
results/
├── experiment.json   # raw data from orchestrate.py
└── report.html       # plots and summary — open in any browser
```

The report includes a saturation curve (throughput vs. process count) and a
slack search plot for each resource, plus the final slack measurement in the
form `(processes, intensity)`.

---

## Workload model

Each worker process runs a tick loop (every 250 ms):

- With probability `(1 − intensity)`: **sleep** — yields the CPU
- With probability `intensity × (1 − io_mix)`: **CPU work** — tight arithmetic loop
- With probability `intensity × io_mix`: **I/O work** — 4 KiB `fsync` write to `TMP_DIR`

The baseline workload is `(io_mix=0.3, intensity=0.75)` — CPU-heavy with some I/O.
Pure resource probes use `io_mix=0` (CPU-only) or `io_mix=1` (I/O-only).

---

## Project layout

```
slack-meter/
├── CMakeLists.txt
├── requirements.txt
├── src/
│   ├── workload.h          # WorkloadParams, WorkloadResult, API
│   ├── workload.cpp        # tick loop, CPU work, I/O work
│   └── worker_main.cpp     # CLI entry point → stdout JSON
├── scripts/
│   ├── orchestrate.py      # saturation + slack experiments
│   ├── report.py           # HTML report generation
│   └── run_experiment.sh   # one-shot build + run + report
└── infra/
    ├── Dockerfile
    └── docker-compose.yml
```
