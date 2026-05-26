#pragma once

#include <cstdint>
#include <random>
#include <string>

#ifdef HAS_URING
#include <liburing.h>
#endif

// ----------------------------------------------------------------------------
// WorkloadParams
// Defines a workload by two dimensions:
//   io_mix   – fraction of non-sleep ops that are I/O  (0 = CPU-only, 1 =
//   IO-only) intensity – fraction of ticks that do real work     (0 =
//   sleep-only, 1 = full)
// ----------------------------------------------------------------------------
struct WorkloadParams {
  double io_mix;       // [0, 1]
  double mem_mix;      // [0, 1]
  double intensity;    // [0, 1]
  int duration_secs;   // how long to run
  int warmup_secs;     // warmup time in seconds
  std::string tmp_dir; // scratch space for I/O ops
  uint64_t seed;       // RNG seed (fixed for reproducibility)
  std::string io_mode; // rand_write | rand_read | rand_read_64k | seq_read
  int queue_depth;     // concurrency level per worker (default: 1)
};

// ----------------------------------------------------------------------------
// WorkloadResult – counters written by run_workload()
// ----------------------------------------------------------------------------
struct WorkloadResult {
  uint64_t cpu_ops;
  uint64_t io_ops;
  uint64_t mem_ops;
  uint64_t sleep_ops;
  double elapsed_secs;
  double throughput; // (cpu_ops + io_ops + mem_ops) / elapsed_secs  — used for
                     // saturation
  double cpu_throughput; // cpu_ops / elapsed_secs
  double io_throughput;  // io_ops  / elapsed_secs
  double mem_throughput; // mem_ops / elapsed_secs
};

// ----------------------------------------------------------------------------
// IoState – per-worker I/O state opened once before the main loop.
//
// The struct carries resources for all four I/O work variants so the scratch
// file is opened and pre-allocated exactly once regardless of which variants
// are exercised.
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
  // ---- shared file identity
  // --------------------------------------------------
  int fd = -1; // O_RDWR | O_DIRECT
  size_t file_size = 0;
  size_t num_blocks = 0; // file_size / IO_BUF_SIZE, for fast modulo
  std::string path;

  // ---- random 4 KiB O_DIRECT write/read  (do_io_work / do_io_read_work) -----
  void *buf = nullptr; // posix_memalign'd, IO_BUF_SIZE (4 KiB) bytes

  // ---- random 64 KiB O_DIRECT read  (do_io_read_64k_work, Probe C) ----------
  void *buf_64k = nullptr;   // posix_memalign'd, BUF_64K_SIZE (64 KiB) bytes
  size_t num_blocks_64k = 0; // file_size / BUF_64K_SIZE, for fast modulo

  // ---- sequential 1 MiB O_DIRECT read  (do_io_seq_read_work, Probe D) -------
  void *seq_buf = nullptr; // posix_memalign'd, SEQ_BUF_SIZE (1 MiB) bytes
  size_t seq_cursor = 0;   // current offset; advances by SEQ_BUF_SIZE

#ifdef HAS_URING
  bool use_uring = false;
  struct io_uring ring;
  int queue_depth = 1;
  void **ring_bufs = nullptr;
  size_t ring_buf_size = 0;
#endif
};

// Open (or create) the per-worker scratch file and return an initialised
// IoState.  file_size must be a multiple of IO_BUF_SIZE.
// Returns a state with fd == -1 on failure.
static constexpr size_t IO_FILE_SIZE = 256ULL * 1024 * 1024; // 256 MiB
IoState open_io_file(const std::string &tmp_dir, const std::string &io_mode,
                     int queue_depth, size_t file_size = IO_FILE_SIZE);

// Issue one 4 KiB O_DIRECT write to a random aligned offset within the file.
// rng is the caller's existing generator — no extra state needed.
// No fsync — matches the durability semantics of the read variants so that
// rand_write measures raw storage-layer IOPS, not the durability pipeline.
void do_io_work(IoState &st, std::mt19937_64 &rng);

// Issue one 4 KiB O_DIRECT read from a random aligned offset within the file.
// Symmetric counterpart to do_io_work; measures random-read IOPS on the same
// code path.  No fsync — reads have no durability component.
void do_io_read_work(IoState &st, std::mt19937_64 &rng);

// Issue one 64 KiB O_DIRECT read from a random 64 KiB-aligned offset (Probe C).
// Tests whether slack is sensitive to operation granularity vs. 4 KiB
// rand_read.
void do_io_read_64k_work(IoState &st, std::mt19937_64 &rng);

// Issue one 1 MiB O_DIRECT sequential read at the current cursor (Probe D).
// Advances st.seq_cursor by SEQ_BUF_SIZE on every call, wrapping at file_size.
// Saturates NVMe sequential-read bandwidth rather than random IOPS.
void do_io_seq_read_work(IoState &st);

// Close fd, free the aligned buffer, and unlink the scratch file.
void close_io_file(IoState &state);

// Run the workload described by params and return result.
WorkloadResult run_workload(const WorkloadParams &params);

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
static constexpr size_t MEM_BUF_DOUBLES =
    4'000'000; // 32 MiB per half, 64 MiB total

struct MemState {
  double *buf = nullptr; // heap-allocated; length = 2 * MEM_BUF_DOUBLES doubles
  size_t n = 0; // half-length in doubles (lo = buf[0..n), hi = buf[n..2n))
};

// Allocate and initialise the memory-bandwidth buffer.
// Returns a state with buf == nullptr on allocation failure.
MemState open_mem_buf();

// Perform one STREAM-scale sweep over the buffer:
//   lo[i] = MEM_SCALAR * hi[i]   (hi → lo pass, read+write)
//   hi[i] = MEM_SCALAR * lo[i]   (lo → hi pass, read+write)
// Each call saturates the memory bus for one unit of work.
void do_mem_work(MemState &st);

// Free the buffer and zero the MemState fields.
void close_mem_buf(MemState &st);
