#include "workload.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <random>
#include <thread>
#include <unistd.h>

// How often the workload re-rolls its operation choice (milliseconds).
static constexpr int TICK_MS = 250;

// Size of each I/O operation (4 KiB).
// O_DIRECT requires transfer size, buffer alignment, and file offset to all be
// multiples of the logical block size.  4 KiB satisfies this on all common
// Linux filesystems and NVMe devices.
static constexpr size_t IO_BUF_SIZE = 4096;

// Transfer size for sequential O_DIRECT writes (128 KiB).
// Large enough to shift the measurement from IOPS-bound to bandwidth-bound;
// still a multiple of IO_BUF_SIZE so all O_DIRECT alignment constraints hold.
static constexpr size_t SEQ_BUF_SIZE = 128ULL * 1024;

// Iterations per CPU work unit.  Large enough to do real work per call,
// small enough that the tick loop can count many completions per 250ms.
static constexpr int CPU_ITERS = 18'400;

// ----------------------------------------------------------------------------
// do_cpu_work – one unit of CPU work: a tight arithmetic loop.
// Called repeatedly inside a tick until the tick window expires.
// ----------------------------------------------------------------------------
void do_cpu_work() {
  volatile uint64_t acc = 1;
  for (int i = 1; i <= CPU_ITERS; ++i) {
    acc = acc * (uint64_t)i ^ (acc >> 7);
  }
  (void)acc;
}

// ----------------------------------------------------------------------------
// open_io_file – open a per-worker scratch file for O_DIRECT overwrites.
//
// Steps:
//   1. Build a filename from tmp_dir + PID (unique per worker process).
//   2. Open with O_DIRECT | O_WRONLY | O_CREAT | O_TRUNC.
//   3. Pre-allocate file_size bytes with fallocate (falls back to ftruncate)
//      so every subsequent write is an overwrite of an existing extent —
//      no per-op metadata allocation.
//   4. Allocate a posix_memalign'd buffer (O_DIRECT requires address
//      alignment equal to the logical block size).
// ----------------------------------------------------------------------------
IoState open_io_file(const std::string &tmp_dir, size_t file_size) {
  IoState st;

  // Unique filename per worker process — no coordination needed.
  char path[512];
  snprintf(path, sizeof(path), "%s/sm_io_%d.dat", tmp_dir.c_str(),
           (int)getpid());
  st.path = path;

  // Aligned buffer required by O_DIRECT.
  if (posix_memalign(&st.buf, IO_BUF_SIZE, IO_BUF_SIZE) != 0)
    return st; // buf stays nullptr; caller checks fd < 0
  std::mt19937_64 init_rng(1337 + getpid());
  uint64_t *buf_ptr = static_cast<uint64_t *>(st.buf);
  for (size_t i = 0; i < IO_BUF_SIZE / sizeof(uint64_t); ++i) {
    buf_ptr[i] = init_rng();
  }

  // O_RDWR so the same fd serves both do_io_work (writes) and do_io_read_work.
  st.fd = open(path, O_RDWR | O_CREAT | O_TRUNC | O_DIRECT, 0600);
  if (st.fd < 0) {
    free(st.buf);
    st.buf = nullptr;
    return st;
  }

  // Pre-allocate so writes are overwrites, not allocating appends.
  // fallocate is preferred (doesn't zero-fill on most filesystems);
  // ftruncate is the fallback for filesystems that don't support it.
  if (fallocate(st.fd, 0, 0, (off_t)file_size) != 0) {
    if (ftruncate(st.fd, (off_t)file_size) != 0) {
      close(st.fd);
      st.fd = -1;
      free(st.buf);
      st.buf = nullptr;
      unlink(path);
      return st;
    }
  }

  st.file_size  = file_size;
  st.num_blocks = file_size / IO_BUF_SIZE;

  // ---- sequential O_DIRECT write buffer (128 KiB, aligned) ------------------
  // Initialise with random data so the first write isn't a trivially patterned
  // buffer; subsequent writes stamp a counter into each sector (see
  // do_io_seq_write_work), so deduplication is defeated without an RNG draw
  // on the hot path.
  if (posix_memalign(&st.seq_buf, IO_BUF_SIZE, SEQ_BUF_SIZE) == 0) {
    uint64_t *p = static_cast<uint64_t *>(st.seq_buf);
    for (size_t i = 0; i < SEQ_BUF_SIZE / sizeof(uint64_t); ++i)
      p[i] = init_rng();
  }
  // seq_cursor starts at 0; do_io_seq_write_work advances it.

  // ---- buffered write fd + buffer --------------------------------------------
  // Open a second file description on the same file without O_DIRECT so the
  // page cache is visible.  O_WRONLY suffices — do_io_buf_write_work never
  // reads through this fd.
  st.buf_fd = open(st.path.c_str(), O_WRONLY, 0600);
  if (st.buf_fd >= 0) {
    st.buf_write_buf = malloc(IO_BUF_SIZE);
    if (st.buf_write_buf) {
      uint64_t *p = static_cast<uint64_t *>(st.buf_write_buf);
      for (size_t i = 0; i < IO_BUF_SIZE / sizeof(uint64_t); ++i)
        p[i] = init_rng();
    } else {
      close(st.buf_fd);
      st.buf_fd = -1;
    }
  }
  // buf_seq_cursor starts at 0; do_io_buf_write_work advances it.

  return st;
}

// ----------------------------------------------------------------------------
// do_io_work – issue one 4 KiB O_DIRECT write to a random aligned offset,
// followed by fsync to match the original durability semantics.
//
// pwrite is used instead of lseek + write to avoid touching the file-position
// state (cleaner, and avoids a potential serialisation point in the kernel
// if multiple threads ever shared the same fd).
// ----------------------------------------------------------------------------
void do_io_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;

  // Mutate one 8-byte word per 512-byte sector to defeat both block-level
  // deduplication and any aggressive sub-block / sector-level deduplication.
  // The cost of 8 fast RNG calls (~16ns) is invisible next to the I/O latency.
  uint64_t *buf_ptr = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 8; ++i) {
    buf_ptr[i * (512 / sizeof(uint64_t))] = rng();
  }

  // Random 4 KiB-aligned offset within the pre-allocated file.
  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);
  if (pwrite(st.fd, st.buf, IO_BUF_SIZE, offset) == (ssize_t)IO_BUF_SIZE) {
    fsync(st.fd);
  }
}

// ----------------------------------------------------------------------------
// close_io_file – tear down the IoState opened by open_io_file.
// ----------------------------------------------------------------------------
void close_io_file(IoState &st) {
  if (st.fd >= 0) {
    close(st.fd);
    st.fd = -1;
  }
  if (st.buf) {
    free(st.buf);
    st.buf = nullptr;
  }
  if (st.seq_buf) {
    free(st.seq_buf);
    st.seq_buf = nullptr;
  }
  if (st.buf_fd >= 0) {
    close(st.buf_fd);
    st.buf_fd = -1;
  }
  if (st.buf_write_buf) {
    free(st.buf_write_buf);
    st.buf_write_buf = nullptr;
  }
  if (!st.path.empty()) {
    unlink(st.path.c_str());
    st.path.clear();
  }
}

// ----------------------------------------------------------------------------
// do_io_read_work – issue one 4 KiB O_DIRECT read from a random aligned
// offset.
//
// Symmetric counterpart to do_io_work on the same fd (opened O_RDWR).
// No fsync — reads have no durability component.  The read buffer is
// intentionally reused across calls: we only care about the device latency
// and throughput, not the content.
// ----------------------------------------------------------------------------
void do_io_read_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;

  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);
  // Return value intentionally ignored: we measure throughput, not content.
  [[maybe_unused]] ssize_t n = pread(st.fd, st.buf, IO_BUF_SIZE, offset);
}

// ----------------------------------------------------------------------------
// do_io_seq_write_work – issue one 128 KiB O_DIRECT write at the current
// sequential cursor, followed by fsync.
//
// The cursor advances by SEQ_BUF_SIZE on every call and wraps at file_size,
// producing a linear scan that repeats.  A sequential access pattern removes
// the seek component from the measurement and allows the storage controller to
// exercise its full sequential-write pipeline, shifting the bottleneck from
// IOPS to bandwidth.
//
// Deduplication is defeated by stamping st.seq_cursor (unique per call) into
// the first word of each 512-byte sector.  This is cheaper than RNG draws and
// produces verifiable data: the stamp is deterministic from the cursor value.
// ----------------------------------------------------------------------------
void do_io_seq_write_work(IoState &st) {
  if (st.fd < 0 || !st.seq_buf)
    return;

  // Stamp each sector's leading word with (cursor | sector_index) so every
  // 512-byte sector differs both within the buffer and across calls.
  uint64_t *buf_ptr = static_cast<uint64_t *>(st.seq_buf);
  static constexpr size_t WORDS_PER_SECTOR = 512 / sizeof(uint64_t);
  static constexpr size_t SECTORS          = SEQ_BUF_SIZE / 512;
  for (size_t i = 0; i < SECTORS; ++i)
    buf_ptr[i * WORDS_PER_SECTOR] = st.seq_cursor | (uint64_t)i;

  const off_t offset = (off_t)st.seq_cursor;
  if (pwrite(st.fd, st.seq_buf, SEQ_BUF_SIZE, offset) == (ssize_t)SEQ_BUF_SIZE)
    fsync(st.fd);

  // Advance cursor; wrap so we stay within the pre-allocated region.
  st.seq_cursor += SEQ_BUF_SIZE;
  if (st.seq_cursor + SEQ_BUF_SIZE > st.file_size)
    st.seq_cursor = 0;
}

// ----------------------------------------------------------------------------
// do_io_buf_write_work – issue one 4 KiB buffered write at the current
// sequential cursor via buf_fd, followed by fdatasync.
//
// buf_fd is opened without O_DIRECT, so writes go through the page cache
// before being flushed to stable storage by fdatasync.  fdatasync is preferred
// over fsync because it does not update file metadata timestamps, keeping the
// measurement focused on data-path latency rather than metadata overhead.
//
// This variant models real application write patterns: database WALs,
// append-only logs, and journaling filesystems all write through the page
// cache and call fdatasync (or equivalent) for durability.
// ----------------------------------------------------------------------------
void do_io_buf_write_work(IoState &st) {
  if (st.buf_fd < 0 || !st.buf_write_buf)
    return;

  // Stamp each sector's leading word with the cursor, same logic as
  // do_io_seq_write_work, so deduplication is defeated deterministically.
  uint64_t *buf_ptr = static_cast<uint64_t *>(st.buf_write_buf);
  static constexpr size_t WORDS_PER_SECTOR = 512 / sizeof(uint64_t);
  static constexpr size_t SECTORS          = IO_BUF_SIZE / 512;  // 8 sectors
  for (size_t i = 0; i < SECTORS; ++i)
    buf_ptr[i * WORDS_PER_SECTOR] = st.buf_seq_cursor | (uint64_t)i;

  const off_t offset = (off_t)st.buf_seq_cursor;
  if (pwrite(st.buf_fd, st.buf_write_buf, IO_BUF_SIZE, offset) == (ssize_t)IO_BUF_SIZE)
    fdatasync(st.buf_fd);

  // Advance cursor; wrap so we stay within the pre-allocated region.
  st.buf_seq_cursor += IO_BUF_SIZE;
  if (st.buf_seq_cursor + IO_BUF_SIZE > st.file_size)
    st.buf_seq_cursor = 0;
}

// Multiplier used in the STREAM-style scale sweep.  Must be kept out of the
// buffer initialisation path (different value) so the compiler cannot fold
// both sweeps into a no-op identity transform.
static constexpr double MEM_SCALAR = 3.0;

// ----------------------------------------------------------------------------
// open_mem_buf – allocate and initialise the cache-busting double buffer.
//
// The buffer is intentionally 64 MiB (2 * MEM_BUF_DOUBLES * 8 bytes) so that
// every do_mem_work() sweep is guaranteed to miss the L3 cache and reach DRAM,
// mirroring how O_DIRECT in do_io_work() bypasses the page cache and reaches
// the storage stack.
//
// Values are initialised to (i + 1.0) — non-zero so the scalar multiply always
// produces a meaningfully different result and cannot be silently elided.
// ----------------------------------------------------------------------------
MemState open_mem_buf() {
  MemState st;
  st.n   = MEM_BUF_DOUBLES;
  st.buf = static_cast<double *>(malloc(2 * st.n * sizeof(double)));
  if (!st.buf) {
    st.n = 0;
    return st;
  }
  for (size_t i = 0; i < 2 * st.n; ++i)
    st.buf[i] = static_cast<double>(i + 1);
  return st;
}

// ----------------------------------------------------------------------------
// do_mem_work – one STREAM-scale sweep over the double buffer.
//
// Adapted from mem_bw.cpp's two-pass scale pattern:
//   Pass 1 (hi → lo):  lo[i] = MEM_SCALAR * hi[i]
//   Pass 2 (lo → hi):  hi[i] = MEM_SCALAR * lo[i]
//
// Each pass reads n doubles from one half and writes n doubles to the other,
// producing ~64 MiB of DRAM traffic per call (2 × n × 8 bytes read +
// 2 × n × 8 bytes written).  Because n = MEM_BUF_DOUBLES = 4 M doubles the
// working set dwarfs any real L3 cache, so all traffic goes to main memory —
// the RAM axis of the I/O / RAM / CPU triangle.
//
// No RNG parameter is needed: unlike I/O ops (which randomise their offset to
// avoid storage-controller optimisations), streaming bandwidth is measured by
// sequential access.  Sequential access is also what maximises DRAM row-buffer
// hit rate and therefore produces the highest sustainable bandwidth.
// ----------------------------------------------------------------------------
void do_mem_work(MemState &st) {
  if (!st.buf)
    return;

  double *lo = st.buf;
  double *hi = st.buf + st.n;

  // Pass 1: read from hi half, write to lo half.
  for (size_t i = 0; i < st.n; ++i)
    lo[i] = MEM_SCALAR * hi[i];

  // Pass 2: read from lo half, write to hi half.
  for (size_t i = 0; i < st.n; ++i)
    hi[i] = MEM_SCALAR * lo[i];
}

// ----------------------------------------------------------------------------
// close_mem_buf – release the buffer allocated by open_mem_buf.
// ----------------------------------------------------------------------------
void close_mem_buf(MemState &st) {
  free(st.buf);
  st.buf = nullptr;
  st.n   = 0;
}

// ----------------------------------------------------------------------------
// run_workload – main loop.
//
// Every TICK_MS ms the loop re-rolls its operation choice:
//   1. Draw m ~ Uniform(0,1).
//      If m > intensity  →  sleep for TICK_MS ms (yield the CPU).
//   2. Else draw n ~ Uniform(0,1).
//      If n > io_mix     →  CPU phase: keep calling do_cpu_work() until the
//                           tick window expires, counting each call as one op.
//      Else              →  I/O phase: keep calling do_io_work() until the
//                           tick window expires, counting each call as one op.
//
// The scratch file is opened once before the loop (open_io_file) and closed
// after (close_io_file), so do_io_work only issues pwrite — no open/close/
// unlink overhead per op.
// ----------------------------------------------------------------------------
WorkloadResult run_workload(const WorkloadParams &params) {
  std::mt19937_64 rng(params.seed);
  std::uniform_real_distribution<double> dist(0.0, 1.0);

  // Open the per-worker scratch file once for the duration of the run.
  IoState io_state = open_io_file(params.tmp_dir);
  if (io_state.fd < 0) {
    fprintf(stderr, "[worker] open_io_file failed for dir %s\n",
            params.tmp_dir.c_str());
  }

  MemState mem_state = open_mem_buf();

  WorkloadResult res{};
  const auto start = std::chrono::steady_clock::now();
  const auto deadline = start + std::chrono::seconds(params.duration_secs);

  while (std::chrono::steady_clock::now() < deadline) {
    const auto tick_end =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(TICK_MS);

    const double m = dist(rng);
    if (m > params.intensity) {
      // Sleep tick: yield the CPU for one tick window.
      std::this_thread::sleep_for(std::chrono::milliseconds(TICK_MS));
      ++res.sleep_ops;
    } else {
      const double n = dist(rng);
      if (n < params.io_mix) {
        // I/O phase: keep issuing ops until the tick closes.
        while (std::chrono::steady_clock::now() < tick_end) {
          if (params.io_mode == "rand_read") {
            do_io_read_work(io_state, rng);
          } else if (params.io_mode == "seq_write") {
            do_io_seq_write_work(io_state);
          } else if (params.io_mode == "buf_write") {
            do_io_buf_write_work(io_state);
          } else {
            // default to rand_write
            do_io_work(io_state, rng);
          }
          ++res.io_ops;
        }
      } else if (n < params.io_mix + params.mem_mix) {
        // MEM phase: hammer memory bandwidth
        while (std::chrono::steady_clock::now() < tick_end) {
          do_mem_work(mem_state);
          ++res.mem_ops;
        }
      } else {
        // CPU phase: hammer do_cpu_work() until the tick window closes.
        while (std::chrono::steady_clock::now() < tick_end) {
          do_cpu_work();
          ++res.cpu_ops;
        }
      }
    }
  }

  close_io_file(io_state);
  close_mem_buf(mem_state);

  const auto finish = std::chrono::steady_clock::now();
  res.elapsed_secs = std::chrono::duration<double>(finish - start).count();
  res.throughput = (res.cpu_ops + res.io_ops + res.mem_ops) / res.elapsed_secs;
  res.cpu_throughput = res.cpu_ops / res.elapsed_secs;
  res.io_throughput = res.io_ops / res.elapsed_secs;
  res.mem_throughput = res.mem_ops / res.elapsed_secs;
  return res;
}
