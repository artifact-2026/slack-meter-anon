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
    double      mem_mix;       // [0, 1]
    double      intensity;     // [0, 1]
    int         duration_secs; // how long to run
    std::string tmp_dir;       // scratch space for I/O ops
    uint64_t    seed;          // RNG seed (fixed for reproducibility)
    std::string io_mode;       // rand_write, rand_read, seq_write, buf_write
};

// ----------------------------------------------------------------------------
// WorkloadResult – counters written by run_workload()
// ----------------------------------------------------------------------------
struct WorkloadResult {
    uint64_t cpu_ops;
    uint64_t io_ops;
    uint64_t mem_ops;
    uint64_t sleep_ops;
    double   elapsed_secs;
    double   throughput;      // (cpu_ops + io_ops + mem_ops) / elapsed_secs  — used for saturation
    double   cpu_throughput;  // cpu_ops / elapsed_secs
    double   io_throughput;   // io_ops  / elapsed_secs
    double   mem_throughput;  // mem_ops / elapsed_secs
};

// ----------------------------------------------------------------------------
// IoState – per-worker I/O state opened once before the main loop.
//
// The struct carries resources for all four I/O work variants so the scratch
// file is opened and pre-allocated exactly once regardless of which variants
// are exercised.
//
// fd (O_RDWR | O_DIRECT) is shared by:
//   - do_io_work          – random 4 KiB O_DIRECT write  + fsync
//   - do_io_read_work     – random 4 KiB O_DIRECT read
//   - do_io_seq_write_work– sequential 128 KiB O_DIRECT write + fsync
//
// buf_fd (O_WRONLY, no O_DIRECT) is used by:
//   - do_io_buf_write_work– sequential 4 KiB buffered write + fdatasync
//
// O_DIRECT constraints on fd:
//   - buffer address aligned to IO_BUF_SIZE  (posix_memalign)
//   - transfer size a multiple of IO_BUF_SIZE
//   - file offset a multiple of IO_BUF_SIZE
//
// The file is pre-allocated with fallocate so every write is an overwrite of
// an already-allocated extent — no metadata churn per op.
// ----------------------------------------------------------------------------
struct IoState {
    // ---- shared file identity --------------------------------------------------
    int         fd         = -1;      // O_RDWR | O_DIRECT
    size_t      file_size  = 0;
    size_t      num_blocks = 0;       // file_size / IO_BUF_SIZE, for fast modulo
    std::string path;

    // ---- random 4 KiB O_DIRECT write/read  (do_io_work / do_io_read_work) -----
    void*       buf        = nullptr; // posix_memalign'd, IO_BUF_SIZE bytes

    // ---- sequential 128 KiB O_DIRECT write  (do_io_seq_write_work) ------------
    void*       seq_buf    = nullptr; // posix_memalign'd, SEQ_BUF_SIZE bytes
    size_t      seq_cursor = 0;       // current write offset; advances by SEQ_BUF_SIZE

    // ---- sequential 4 KiB buffered write  (do_io_buf_write_work) --------------
    int         buf_fd         = -1;      // same file, opened without O_DIRECT
    void*       buf_write_buf  = nullptr; // malloc'd, IO_BUF_SIZE bytes
    size_t      buf_seq_cursor = 0;       // current write offset; advances by IO_BUF_SIZE
};

// Open (or create) the per-worker scratch file and return an initialised
// IoState.  file_size must be a multiple of IO_BUF_SIZE.
// Returns a state with fd == -1 on failure.
static constexpr size_t IO_FILE_SIZE = 256ULL * 1024 * 1024;  // 256 MiB
IoState open_io_file(const std::string& tmp_dir,
                     size_t file_size = IO_FILE_SIZE,
                     const std::string& io_mode = "rand_write");

// Issue one 4 KiB O_DIRECT write to a random aligned offset within the file.
// rng is the caller's existing generator — no extra state needed.
void do_io_work(IoState& st, std::mt19937_64& rng);

// Issue one 4 KiB O_DIRECT read from a random aligned offset within the file.
// Symmetric counterpart to do_io_work; measures random-read IOPS on the same
// code path.  No fsync — reads have no durability component.
void do_io_read_work(IoState& st, std::mt19937_64& rng);

// Issue one 128 KiB O_DIRECT write at the current sequential cursor, then
// fsync.  Advances st.seq_cursor by SEQ_BUF_SIZE on every call, wrapping at
// file_size.  Measures sequential write throughput rather than random IOPS.
void do_io_seq_write_work(IoState& st);

// Issue one 4 KiB buffered (non-O_DIRECT) write at the current sequential
// cursor via buf_fd, then fdatasync.  Advances st.buf_seq_cursor by IO_BUF_SIZE
// on every call, wrapping at file_size.  Exercises the page-cache write path
// and models database-WAL / log-structured write patterns.
void do_io_buf_write_work(IoState& st);

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
