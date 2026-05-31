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
//   io_mix   – fraction of non-sleep ops that are I/O  (0 = CPU-only, 1 = IO-only)
//   intensity – fraction of ticks that do real work    (0 = sleep-only, 1 = full)
// ----------------------------------------------------------------------------
struct WorkloadParams {
  double io_mix;       // [0, 1]
  double mem_mix;      // [0, 1]
  double intensity;    // [0, 1]
  int duration_secs;   // how long to run
  int warmup_secs;     // warmup time in seconds
  std::string tmp_dir; // scratch space for I/O ops
  uint64_t seed;       // RNG seed (fixed for reproducibility)
  std::string io_mode; // rand_write | rand_read | seq_write | seq_read
  int queue_depth;     // concurrency level per worker (default: 1)
  std::string cpu_mode; // cpu_int | cpu_fp | cpu_hash
  std::string mem_mode; // mem_copy | mem_read | mem_write
  size_t file_size;    // scratch file size in bytes (0 → use IO_FILE_SIZE default)
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
  double throughput;      // (cpu_ops + io_ops + mem_ops) / elapsed_secs
  double cpu_throughput;  // cpu_ops / elapsed_secs
  double io_throughput;   // io_ops  / elapsed_secs
  double mem_throughput;  // mem_ops / elapsed_secs
};

// ----------------------------------------------------------------------------
// IoState – per-worker I/O state opened once before the main loop.
//
// All four I/O variants use the same 4 KiB aligned buffer (buf) and the same
// file descriptor (fd, opened O_RDWR | O_DIRECT).  Sequential variants advance
// seq_cursor by IO_BUF_SIZE per op, wrapping at file_size.
//
// O_DIRECT constraints:
//   - buffer address aligned to IO_BUF_SIZE (posix_memalign)
//   - transfer size == IO_BUF_SIZE
//   - file offset a multiple of IO_BUF_SIZE
//
// The file is pre-allocated with fallocate so every write is an overwrite of
// an already-allocated extent — no metadata churn per op.
// ----------------------------------------------------------------------------
struct IoState {
  int fd = -1;           // O_RDWR | O_DIRECT
  size_t file_size = 0;
  size_t num_blocks = 0; // file_size / IO_BUF_SIZE
  std::string path;

  void *buf = nullptr;   // posix_memalign'd, IO_BUF_SIZE (4 KiB)

  size_t seq_cursor = 0; // current offset for sequential variants; advances by
                         // IO_BUF_SIZE per op, wraps at file_size

  // ---- legacy large-transfer buffers (used by the legacy I/O ops below) ----
  void *buf_64k = nullptr;   // posix_memalign'd, 64 KiB (rand_read_64k)
  size_t num_blocks_64k = 0; // file_size / BUF_64K_SIZE
  void *seq_buf = nullptr;   // posix_memalign'd, 1 MiB  (seq_read legacy)

#ifdef HAS_URING
  bool use_uring = false;
  struct io_uring ring;
  int queue_depth = 1;
  void **ring_bufs = nullptr;
#endif
};

// Open (or create) the per-worker scratch file and return an initialised
// IoState.  file_size must be a multiple of IO_BUF_SIZE.
// Returns a state with fd == -1 on failure.
static constexpr size_t IO_FILE_SIZE = 256ULL * 1024 * 1024; // 256 MiB
IoState open_io_file(const std::string &tmp_dir, const std::string &io_mode,
                     int queue_depth, size_t file_size = IO_FILE_SIZE);

// ----------------------------------------------------------------------------
// Four fungible 4 KiB I/O operations.
//
//   do_io_work_4k_rand_write  – pwrite 4 KiB to a random aligned offset
//   do_io_work_4k_rand_read   – pread  4 KiB from a random aligned offset
//   do_io_work_4k_seq_write   – pwrite 4 KiB at seq_cursor; advance cursor
//   do_io_work_4k_seq_read    – pread  4 KiB at seq_cursor; advance cursor
//
// All four use O_DIRECT so operations bypass the page cache and reach the
// storage stack directly.  No fsync — durability is outside the scope of
// throughput measurement.
// ----------------------------------------------------------------------------
void do_io_work_4k_rand_write(IoState &st, std::mt19937_64 &rng);
void do_io_work_4k_rand_read (IoState &st, std::mt19937_64 &rng);
void do_io_work_4k_seq_write (IoState &st, std::mt19937_64 &rng);
void do_io_work_4k_seq_read  (IoState &st);

// Close fd, free the aligned buffer, and unlink the scratch file.
void close_io_file(IoState &state);

// ----------------------------------------------------------------------------
// Legacy I/O operations (preserved for reference / future use)
//
// These predate the four-function fungible interface above.  They are not
// called by run_workload() under any current io_mode string; restore the
// relevant dispatch branch if you want to use them again.
// ----------------------------------------------------------------------------

// One read-modify-write (RMW) op on a random 4 KiB block (RW_balanced).
void do_io_work(IoState &st, std::mt19937_64 &rng);

// 4 KiB pure random write (rand_write legacy name).
void do_io_write_work(IoState &st, std::mt19937_64 &rng);

// 4 KiB pure random read (rand_read legacy name).
void do_io_read_work(IoState &st, std::mt19937_64 &rng);

// 50/50 random read or RMW write (rand_rw).
void do_io_rw_work(IoState &st, std::mt19937_64 &rng);

// 4 random reads + 1 random write (R_heavy).
void do_io_r_heavy_work(IoState &st, std::mt19937_64 &rng);

// 1 random read + 1 random write + 1 fdatasync (W_heavy).
void do_io_w_heavy_work(IoState &st, std::mt19937_64 &rng);

// 50/50 random read or random write (rw_mixed).
void do_io_rw_mixed_work(IoState &st, std::mt19937_64 &rng);

// 64 KiB random read (rand_read_64k).
void do_io_read_64k_work(IoState &st, std::mt19937_64 &rng);

// 1 MiB sequential read, cursor advances by SEQ_BUF_SIZE (seq_read legacy).
void do_io_seq_read_work(IoState &st);

// Run the workload described by params and return result.
WorkloadResult run_workload(const WorkloadParams &params);

// Individual CPU operations (exposed for unit testing / benchmarking).
void do_cpu_work();
void do_cpu_int_work();
void do_cpu_fp_work();
void do_cpu_hash_work();

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
void do_mem_work(MemState &st, const std::string &mem_mode);

// Free the buffer and zero the MemState fields.
void close_mem_buf(MemState &st);
