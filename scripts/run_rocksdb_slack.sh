#!/usr/bin/env bash
# =============================================================================
# run_rocksdb_slack.sh
# ====================
# Orchestrates a full RocksDB slack experiment:
#
#   1. Load a RocksDB database via ycsb_test (skipped with --skip-load).
#   2. Run saturate_rocksdb.py to find the saturation knee (thread count at
#      peak ops/s).  The resulting JSON is passed to the probe scripts.
#   3. Run probe_rocksdb.py for IO, CPU, and RAM slack in sequence.
#      Each run adds synthetic probe workers (slack-meter worker binary) on top
#      of the live RocksDB workload and measures how much of each resource is
#      available before RocksDB throughput degrades.
#
# Prerequisites
# -------------
#   - ycsb_test binary built from the htap project.
#   - slack-meter worker binary (cmake -B build && cmake --build build).
#   - A YCSB workload .spec file (e.g., htap/src/test/ycsb/workloads/workloada.spec).
#
# Usage
# -----
#   # Minimal (uses defaults; adjusts paths to your environment):
#   BG_BINARY=/path/to/ycsb_test \
#   BG_DBPATH=/holly/rocksdb_bench \
#   BG_SPEC=/path/to/workloada.spec \
#   bash scripts/run_rocksdb_slack.sh
#
#   # Full example — reset DB per probe point (paper-quality results):
#   BG_BINARY=/path/to/ycsb_test \
#   BG_DBPATH=/holly/rocksdb_bench \
#   BG_SPEC=/path/to/workloada.spec \
#   RESET_DB_PER_POINT=true \
#   DROP_CACHES=true \
#   RECORD_COUNT=10000000 \
#   RUNTIME=120 \
#   SKIP=30 \
#   OUTPUT_DIR=results/rocksdb_slack_$(date +%Y%m%d_%H%M%S) \
#   bash scripts/run_rocksdb_slack.sh
#
#   # Skip the load phase (DB already exists):
#   ... --skip-load
#
#   # Skip saturation sweep (supply knee threads directly):
#   ... --skip-saturation --bg-threads 16
#
#   # Run only the saturation phase:
#   ... --phase saturate
#
#   # Run only the slack probes (requires existing saturation.json or --bg-threads):
#   ... --phase slack --bg-threads 16
#
#   # Run with custom linear saturation sweep parameters:
#   ... --sat-start-n 8 --sat-step 4 --sat-max-n 32
#
# =============================================================================

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

# Paths to binaries and workload spec (required)
BG_BINARY="${BG_BINARY:-}"                          # path to ycsb_test
BG_DBPATH="${BG_DBPATH:-/holly/rocksdb_bench}"      # RocksDB database directory
BG_SPEC="${BG_SPEC:-}"                              # YCSB .spec file

# slack-meter worker binary (built from this repo)
WORKER_BIN="${WORKER_BIN:-$REPO/build/worker}"

# DB sizing
RECORD_COUNT="${RECORD_COUNT:-10000000}"            # records to load (~20 GB with 2 KB records)
LOAD_THREADS="${LOAD_THREADS:-8}"                   # threads for load phase
ROCKSDB_PARALLELISM="${ROCKSDB_PARALLELISM:-32}"    # RocksDB compaction parallelism

# Saturation sweep
SAT_START_N="${SAT_START_N:-1}"                     # starting thread count in sat sweep
SAT_STEP="${SAT_STEP:-1}"                           # thread increment step in sat sweep
SAT_MAX_N="${SAT_MAX_N:-64}"                         # max thread count in sat sweep
THREAD_COUNTS="${THREAD_COUNTS:-}"                  # thread counts to sweep (auto-generated linear if empty)
SAT_RUNTIME="${SAT_RUNTIME:-}"                   # seconds per thread count in sat sweep (inherits from RUNTIME if not set)
SAT_SKIP="${SAT_SKIP:-}"                         # warmup skip in sat sweep (inherits from SKIP if not set)

# Probe sweep (applies to io, cpu, ram probes)
RUNTIME="${RUNTIME:-120}"
SKIP="${SKIP:-30}"
# Inherit SAT values from probe defaults if not explicitly set
if [[ -z "${SAT_RUNTIME}" ]]; then SAT_RUNTIME="${RUNTIME}"; fi
if [[ -z "${SAT_SKIP}" ]]; then SAT_SKIP="${SKIP}"; fi                       # warmup skip per measurement (= probe worker warmup)
DROP_PCT="${DROP_PCT:-0.05}"                        # RocksDB ops/s drop to call interference (5%)
INTERFERENCE_COUNT="${INTERFERENCE_COUNT:-3}"       # consecutive interference events to stop Phase 1
SAMPLES="${SAMPLES:-1}"                             # samples per probe level (use ≥3 for paper)
BASELINE_SAMPLES="${BASELINE_SAMPLES:-1}"           # samples for Phase 0 baseline
BINARY_STEPS="${BINARY_STEPS:-5}"                   # Phase 2 binary-search depth
MAX_PROBES="${MAX_PROBES:-64}"                      # max probe workers in Phase 1
STEP="${STEP:-1}"                                   # Phase 1 step size
FILE_SIZE_MIB="${FILE_SIZE_MIB:-256}"               # per-probe-worker scratch file size (MiB)

# Probe worker IO mode (for IO probe)
PROBE_IO_MODE="${PROBE_IO_MODE:-rand_write}"
# Probe worker CPU mode (for CPU probe)
PROBE_CPU_MODE="${PROBE_CPU_MODE:-cpu_int}"
# Probe worker memory mode (for RAM probe)
PROBE_MEM_MODE="${PROBE_MEM_MODE:-mem_copy}"

# Reset / cache behaviour
RESET_DB_PER_POINT="${RESET_DB_PER_POINT:-false}"   # wipe+reload DB before every measurement
DROP_CACHES="${DROP_CACHES:-true}"                  # drop OS page cache before each measurement

# Output
OUTPUT_DIR="${OUTPUT_DIR:-$REPO/results/rocksdb_slack_$(date +%Y%m%d_%H%M%S)}"
TMP_DIR="${TMP_DIR:-}"                              # temp spec/scratch; auto-selected if empty

# Scratch tmp selection (prefer /holly if available)
if [[ -z "$TMP_DIR" ]]; then
    if [[ -d "/holly" && -w "/holly" ]]; then
        TMP_DIR="/holly/slack-meter-rdb-probe"
    else
        TMP_DIR="/tmp/slack-meter-rdb-probe"
    fi
fi

# ---------------------------------------------------------------------------
# Flags (parsed from $@ to allow --flag overrides on the command line)
# ---------------------------------------------------------------------------
SKIP_LOAD=false
SKIP_SATURATION=false
BG_THREADS_OVERRIDE=""   # set with --bg-threads to bypass saturation sweep
PHASE="full"
RESOURCE_TYPE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-load)          SKIP_LOAD=true ;;
        --skip-saturation)    SKIP_SATURATION=true ;;
        --bg-threads)         BG_THREADS_OVERRIDE="$2"; shift ;;
        --bg-threads=*)       BG_THREADS_OVERRIDE="${1#*=}" ;;
        --output-dir)         OUTPUT_DIR="$2"; shift ;;
        --output-dir=*)       OUTPUT_DIR="${1#*=}" ;;
        --reset-db-per-point) RESET_DB_PER_POINT=true ;;
        --drop-caches)        DROP_CACHES=true ;;
        --phase)              PHASE="$2"; shift ;;
        --phase=*)            PHASE="${1#*=}" ;;
        --resource-type)         RESOURCE_TYPE="$2"; shift ;;
        --resource-type=*)       RESOURCE_TYPE="${1#*=}" ;;
        --io-mode)          PROBE_IO_MODE="$2"; shift ;;
        --io-mode=*)        PROBE_IO_MODE="${1#*=}" ;;
        --sat-start-n)        SAT_START_N="$2"; shift ;;
        --sat-start-n=*)      SAT_START_N="${1#*=}" ;;
        --sat-step)           SAT_STEP="$2"; shift ;;
        --sat-step=*)         SAT_STEP="${1#*=}" ;;
        --sat-max-n)          SAT_MAX_N="$2"; shift ;;
        --sat-max-n=*)        SAT_MAX_N="${1#*=}" ;;
        --thread-counts)      THREAD_COUNTS="$2"; shift ;;
        --thread-counts=*)    THREAD_COUNTS="${1#*=}" ;;
        *)  echo "[run_rocksdb_slack] Unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done

# Validate PHASE
if [[ "$PHASE" != "saturate" && "$PHASE" != "slack" && "$PHASE" != "full" ]]; then
    echo "ERROR: Invalid phase '$PHASE'. Must be one of: saturate, slack, full" >&2
    exit 1
fi

# Generate linear thread counts if THREAD_COUNTS is not explicitly overridden
if [[ -z "${THREAD_COUNTS:-}" ]]; then
    THREAD_COUNTS=""
    for ((n=SAT_START_N; n<=SAT_MAX_N; n+=SAT_STEP)); do
        THREAD_COUNTS="$THREAD_COUNTS $n"
    done
    THREAD_COUNTS="${THREAD_COUNTS# }"
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "[$(date '+%H:%M:%S')] $*"; }
sep() { echo ""; log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Build slack-meter if not already built
# ---------------------------------------------------------------------------
if [[ ! -x "$WORKER_BIN" ]]; then
    sep
    log "Building slack-meter worker …"
    cmake -B "$REPO/build" -DCMAKE_BUILD_TYPE=Release -S "$REPO"
    cmake --build "$REPO/build" --parallel "$(nproc 2>/dev/null || sysctl -n hw.ncpu)"
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
sep
log "Pre-flight checks"

[[ -n "$BG_BINARY" ]]  || die "BG_BINARY is not set. Point it at your ycsb_test binary."
[[ -f "$BG_BINARY" ]]  || die "ycsb_test binary not found: $BG_BINARY"
[[ -n "$BG_SPEC" ]]    || die "BG_SPEC is not set. Point it at a YCSB .spec file."
[[ -f "$BG_SPEC" ]]    || die "Workload spec not found: $BG_SPEC"
[[ -x "$WORKER_BIN" ]] || die "slack-meter worker binary not found at $WORKER_BIN. " \
                              "Build with: cmake -B build && cmake --build build"
command -v python3 >/dev/null 2>&1 || die "python3 is required."

mkdir -p "$OUTPUT_DIR" "$TMP_DIR"

log "Configuration summary:"
log "  ycsb_test        : $BG_BINARY"
log "  DB path          : $BG_DBPATH"
log "  Workload spec    : $BG_SPEC"
log "  Worker binary    : $WORKER_BIN"
log "  Record count     : $RECORD_COUNT"
log "  Runtime/pt       : ${RUNTIME}s  (skip ${SKIP}s)"
log "  Reset DB/pt      : $RESET_DB_PER_POINT"
log "  Drop caches      : $DROP_CACHES"
log "  Output dir       : $OUTPUT_DIR"
log "  Phase            : $PHASE"
log "  Thread counts    : $THREAD_COUNTS"

# ---------------------------------------------------------------------------
# Phase 1: Load DB
# ---------------------------------------------------------------------------
if [[ "$SKIP_LOAD" == "true" ]]; then
    sep
    log "Skipping load phase (--skip-load). Assuming DB exists at $BG_DBPATH."
    [[ -d "$BG_DBPATH" ]] || die "DB directory not found and --skip-load was set: $BG_DBPATH"
else
    sep
    log "Phase 1: Loading RocksDB database ($RECORD_COUNT records) …"

    # Wipe any existing DB so bootstrap opens a clean default-CF-only instance.
    # If the DB already has a "baseline" column family, DB::Open(default CF only)
    # will fail with "Column families not opened: baseline".
    if [[ -d "$BG_DBPATH" ]]; then
        log "  Wiping existing DB at $BG_DBPATH …"
        rm -rf "$BG_DBPATH"
    fi

    # Build a temp spec with dbpath overridden so it wins over any dbpath
    # set inside BG_SPEC.  We filter out existing definitions of overridden
    # keys to ensure our overrides take effect regardless of parser precedence.
    LOAD_SPEC=$(mktemp "$TMP_DIR/load_spec_XXXXX.spec")
    grep -E -v "^(dbpath|recordcount|operationcount|rocksdb_parallelism|xputwindow)[[:space:]]*=" "$BG_SPEC" > "$LOAD_SPEC" || true
    printf "\ndbpath=%s\nrecordcount=%s\noperationcount=%s\nrocksdb_parallelism=%s\nxputwindow=0\n" \
        "$BG_DBPATH" "$RECORD_COUNT" "$RECORD_COUNT" "$ROCKSDB_PARALLELISM" >> "$LOAD_SPEC"

    "$BG_BINARY" \
        -db        baseline \
        -dbpath    "$BG_DBPATH" \
        -P         "$LOAD_SPEC" \
        -bootstrap true \
        -threads   "$LOAD_THREADS" \
        -load      true \
        -run       false \
        -throughput false \
        -runtime   0 \
        -levels    7 \
        -table     baseline \
        2>&1 | tee "$OUTPUT_DIR/load.log"

    rm -f "$LOAD_SPEC"
    log "Load complete.  Log → $OUTPUT_DIR/load.log"

    # Drop OS page cache after loading so the first measurement reads from disk,
    # matching the per-point behaviour in probe_rocksdb.py and cpu_slack_sweep.sh.
    if [[ "$DROP_CACHES" == "true" ]]; then
        log "  Dropping OS page cache after initial load …"
        sync
        echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null \
            || log "  WARNING: drop_caches failed — first measurement may read from RAM."
    fi
fi

# ---------------------------------------------------------------------------
# Phase 2: Saturation sweep → find knee thread count
# ---------------------------------------------------------------------------
SATURATION_JSON="$OUTPUT_DIR/saturation.json"
BG_THREADS=""

if [[ "$PHASE" == "slack" ]] || [[ "$SKIP_SATURATION" == "true" ]] || [[ -n "$BG_THREADS_OVERRIDE" ]]; then
    sep
    if [[ -n "$BG_THREADS_OVERRIDE" ]]; then
        BG_THREADS="$BG_THREADS_OVERRIDE"
        log "Skipping saturation sweep. Using --bg-threads=$BG_THREADS as the knee."
        # Write a minimal saturation JSON so probe scripts can consume it
        cat > "$SATURATION_JSON" <<JSON
{
  "knee_threads":  $BG_THREADS,
  "peak_ops_s":    null,
  "workload_spec": "$BG_SPEC",
  "bg_binary":     "$BG_BINARY",
  "bg_dbpath":     "$BG_DBPATH",
  "runtime_s":     $RUNTIME,
  "skip_s":        $SKIP,
  "record_count":  $RECORD_COUNT
}
JSON
    elif [[ -f "$SATURATION_JSON" ]]; then
        BG_THREADS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['knee_threads'])" \
                     "$SATURATION_JSON")
        log "Skipping saturation sweep. Using existing knee from $SATURATION_JSON: $BG_THREADS threads"
    else
        log "Skipping saturation sweep. No existing $SATURATION_JSON found and --bg-threads not set."
        die "Must supply --bg-threads N or have an existing saturation.json in output directory when skipping saturation sweep."
    fi
else
    sep
    log "Phase 2: Saturation sweep (finding knee thread count) …"

    RESET_FLAG=""
    CACHE_FLAG=""
    [[ "$RESET_DB_PER_POINT" == "true" ]] && RESET_FLAG="--reset-db-per-point"
    [[ "$DROP_CACHES"        == "true" ]] && CACHE_FLAG="--drop-caches"

    python3 "$REPO/scripts/saturate_rocksdb.py" \
        --bg-binary            "$BG_BINARY" \
        --bg-dbpath            "$BG_DBPATH" \
        --bg-workload-spec     "$BG_SPEC" \
        --record-count         "$RECORD_COUNT" \
        --rocksdb-parallelism  "$ROCKSDB_PARALLELISM" \
        --load-threads         "$LOAD_THREADS" \
        --thread-counts        "$THREAD_COUNTS" \
        --runtime              "$SAT_RUNTIME" \
        --skip                 "$SAT_SKIP" \
        --output-dir           "$OUTPUT_DIR/saturation" \
        --output               "$SATURATION_JSON" \
        --tmp-dir              "$TMP_DIR" \
        ${RESET_FLAG:+"$RESET_FLAG"} \
        ${CACHE_FLAG:+"$CACHE_FLAG"}

    BG_THREADS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['knee_threads'])" \
                 "$SATURATION_JSON")
    log "Saturation knee: $BG_THREADS threads"
fi

log "Saturation JSON → $SATURATION_JSON"

if [[ "$PHASE" == "saturate" ]]; then
    sep
    log "Phase is 'saturate'. Exiting early as requested."
    exit 0
fi

# ---------------------------------------------------------------------------
# Common probe arguments
# ---------------------------------------------------------------------------
RESET_FLAG=""
CACHE_FLAG=""
[[ "$RESET_DB_PER_POINT" == "true" ]] && RESET_FLAG="--reset-db-per-point"
[[ "$DROP_CACHES"        == "true" ]] && CACHE_FLAG="--drop-caches"

COMMON_PROBE_ARGS=(
    --saturation-json      "$SATURATION_JSON"
    --bg-threads           "$BG_THREADS"
    --rocksdb-parallelism  "$ROCKSDB_PARALLELISM"
    --record-count         "$RECORD_COUNT"
    --runtime              "$RUNTIME"
    --skip                 "$SKIP"
    --load-threads         "$LOAD_THREADS"
    --worker-bin           "$WORKER_BIN"
    --drop-pct             "$DROP_PCT"
    --interference-threshold-count "$INTERFERENCE_COUNT"
    --samples              "$SAMPLES"
    --baseline-samples     "$BASELINE_SAMPLES"
    --binary-steps         "$BINARY_STEPS"
    --max-probes           "$MAX_PROBES"
    --step                 "$STEP"
    --file-size-mib        "$FILE_SIZE_MIB"
    --tmp-dir              "$TMP_DIR"
    --xputfile-dir         "$OUTPUT_DIR/xput_csvs"
    ${RESET_FLAG:+"$RESET_FLAG"}
    ${CACHE_FLAG:+"$CACHE_FLAG"}
)

# ---------------------------------------------------------------------------
# Phase 3: IO slack probe
# ---------------------------------------------------------------------------
if [ -z "$RESOURCE_TYPE" ] || [ "$RESOURCE_TYPE" = "io" ]; then
    sep
    log "Phase 3: IO slack probe (probe_io_mode=$PROBE_IO_MODE) …"
    python3 "$REPO/scripts/probe_rocksdb.py" \
        --probe-type     io \
        --probe-io-mode  "$PROBE_IO_MODE" \
        --output         "$OUTPUT_DIR/probe_io.json" \
        --plot           "$OUTPUT_DIR/probe_io.png" \
        "${COMMON_PROBE_ARGS[@]}"
    log "IO probe done.  Result → $OUTPUT_DIR/probe_io.json"
fi

# ---------------------------------------------------------------------------
# Phase 4: CPU slack probe
# ---------------------------------------------------------------------------
if [ -z "$RESOURCE_TYPE" ] || [ "$RESOURCE_TYPE" = "cpu" ]; then
    sep
    log "Phase 4: CPU slack probe (probe_cpu_mode=$PROBE_CPU_MODE) …"
    python3 "$REPO/scripts/probe_rocksdb.py" \
        --probe-type      cpu \
        --probe-cpu-mode  "$PROBE_CPU_MODE" \
        --output          "$OUTPUT_DIR/probe_cpu.json" \
        --plot            "$OUTPUT_DIR/probe_cpu.png" \
        "${COMMON_PROBE_ARGS[@]}"
    log "CPU probe done.  Result → $OUTPUT_DIR/probe_cpu.json"
fi

# ---------------------------------------------------------------------------
# Phase 5: RAM slack probe
# ---------------------------------------------------------------------------
if [ -z "$RESOURCE_TYPE" ] || [ "$RESOURCE_TYPE" = "ram" ]; then
    sep
    log "Phase 5: RAM slack probe (probe_mem_mode=$PROBE_MEM_MODE) …"
    python3 "$REPO/scripts/probe_rocksdb.py" \
        --probe-type      ram \
        --probe-mem-mode  "$PROBE_MEM_MODE" \
        --output          "$OUTPUT_DIR/probe_ram.json" \
        --plot            "$OUTPUT_DIR/probe_ram.png" \
        "${COMMON_PROBE_ARGS[@]}"
    log "RAM probe done.  Result → $OUTPUT_DIR/probe_ram.json"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
sep
log "All probes complete."
log ""
log "Results directory : $OUTPUT_DIR"
log ""
log "  Saturation JSON  : $OUTPUT_DIR/saturation.json"
log "  IO slack result  : $OUTPUT_DIR/probe_io.json"
log "  IO slack plot    : $OUTPUT_DIR/probe_io.png"
log "  CPU slack result : $OUTPUT_DIR/probe_cpu.json"
log "  CPU slack plot   : $OUTPUT_DIR/probe_cpu.png"
log "  RAM slack result : $OUTPUT_DIR/probe_ram.json"
log "  RAM slack plot   : $OUTPUT_DIR/probe_ram.png"
log ""
log "To plot all three on one figure:"
log "  python3 scripts/plot.py \\"
log "    --input $OUTPUT_DIR/probe_io.json \\"
log "    --input $OUTPUT_DIR/probe_cpu.json \\"
log "    --input $OUTPUT_DIR/probe_ram.json"
