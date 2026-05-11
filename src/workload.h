#pragma once

#include <cstdint>
#include <random>
#include <string>

// ----------------------------------------------------------------------------
// WorkloadParams
// Defines a workload by two dimensions:
//   io_mix   – fraction of non-sleep ops that are I/O  (0 = CPU-only, 1 = IO-only)
//   intensity – fraction of ticks that do real work     (0 = sleep-only, 1 = full)
// ----------------------------------------------------------------------------
struct WorkloadParams {
    double      io_mix;        // [0, 1]
    double      intensity;     // [0, 1]
    int         duration_secs; // how long to run
    std::string tmp_dir;       // scratch space for I/O ops
    uint64_t    seed;          // RNG seed (fixed for reproducibility)
};

// ----------------------------------------------------------------------------
// WorkloadResult – counters written by run_workload()
// ----------------------------------------------------------------------------
struct WorkloadResult {
    uint64_t cpu_ops;
    uint64_t io_ops;
    uint64_t sleep_ops;
    double   elapsed_secs;
    double   throughput;      // (cpu_ops + io_ops) / elapsed_secs  — used for saturation
    double   cpu_throughput;  // cpu_ops / elapsed_secs
    double   io_throughput;   // io_ops  / elapsed_secs
};

// ----------------------------------------------------------------------------
// IoState – per-worker I/O state opened once before the main loop.
//
// Using O_DIRECT bypasses the page cache so writes go straight to the storage
// stack.  O_DIRECT requires:
//   - buffer address aligned to IO_BUF_SIZE (allocated via posix_memalign)
//   - transfer size a multiple of IO_BUF_SIZE (always 4 KiB here)
//   - file offset a multiple of IO_BUF_SIZE  (enforced in do_io_work)
//
// The file is pre-allocated with fallocate so every write is an overwrite of
// an already-allocated extent — no metadata churn per op.
// ----------------------------------------------------------------------------
struct IoState {
    int         fd        = -1;
    void*       buf       = nullptr;  // posix_memalign'd, IO_BUF_SIZE aligned
    size_t      file_size = 0;
    size_t      num_blocks = 0;       // file_size / IO_BUF_SIZE, for fast modulo
    std::string path;
};

// Open (or create) the per-worker scratch file and return an initialised
// IoState.  file_size must be a multiple of IO_BUF_SIZE.
// Returns a state with fd == -1 on failure.
static constexpr size_t IO_FILE_SIZE = 256ULL * 1024 * 1024;  // 256 MiB
IoState open_io_file(const std::string& tmp_dir,
                     size_t file_size = IO_FILE_SIZE);

// Issue one 4 KiB O_DIRECT write to a random aligned offset within the file.
// rng is the caller's existing generator — no extra state needed.
void do_io_work(IoState& state, std::mt19937_64& rng);

// Close fd, free the aligned buffer, and unlink the scratch file.
void close_io_file(IoState& state);

// Run the workload described by params and return result.
WorkloadResult run_workload(const WorkloadParams& params);

// Individual operation (exposed for unit testing / benchmarking).
// do_cpu_work performs one fixed-size unit of arithmetic work.
void do_cpu_work();
