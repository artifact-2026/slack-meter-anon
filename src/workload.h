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

// ----------------------------------------------------------------------------
// MemState – per-worker memory-bandwidth state allocated once before the main
// loop.
//
// The buffer spans 2 * MEM_BUF_DOUBLES doubles so do_mem_work() can perform a
// STREAM-style scale sweep: read from the hi half, scalar-multiply, write to
// the lo half, then reverse — generating one full cache-busting read+write
// pass per call.
//
// The buffer is sized well above any realistic L3 cache so every sweep forces
// traffic to DRAM rather than serving hits from cache.
// ----------------------------------------------------------------------------
static constexpr size_t MEM_BUF_DOUBLES = 4'000'000;  // 32 MiB per half, 64 MiB total

struct MemState {
    double* buf = nullptr;   // heap-allocated; length = 2 * MEM_BUF_DOUBLES doubles
    size_t  n   = 0;         // half-length in doubles (lo = buf[0..n), hi = buf[n..2n))
};

// Allocate and initialise the memory-bandwidth buffer.
// Returns a state with buf == nullptr on allocation failure.
MemState open_mem_buf();

// Perform one STREAM-scale sweep over the buffer:
//   lo[i] = MEM_SCALAR * hi[i]   (hi → lo pass, read+write)
//   hi[i] = MEM_SCALAR * lo[i]   (lo → hi pass, read+write)
// Each call saturates the memory bus for one unit of work.
void do_mem_work(MemState& st);

// Free the buffer and zero the MemState fields.
void close_mem_buf(MemState& st);
