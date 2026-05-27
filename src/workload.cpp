#include "workload.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <random>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>

#ifdef HAS_URING
#include <liburing.h>
#endif

#ifdef __APPLE__
#define fdatasync(fd) fsync(fd)
#ifndef O_DIRECT
#define O_DIRECT 0
#endif
#endif

// How often the workload re-rolls its operation choice (milliseconds).
static constexpr int TICK_MS = 250;

// Size of each I/O operation (4 KiB).
// O_DIRECT requires transfer size, buffer alignment, and file offset to all be
// multiples of the logical block size.  4 KiB satisfies this on all common
// Linux filesystems and NVMe devices.
static constexpr size_t IO_BUF_SIZE = 4096;

// Transfer size for the 64 KiB random-read probe (Probe C).
// Must be a multiple of the logical block size (4 KiB).  Using 64 KiB shifts
// the bottleneck from command-issue overhead toward bandwidth, allowing
// comparison of IOPS-bound (4 KiB) vs. bandwidth-bound (64 KiB) probe paths.
static constexpr size_t BUF_64K_SIZE = 65536;

// Transfer size for sequential O_DIRECT reads (1 MiB, Probe D).
// Large enough to saturate the NVMe's sequential-read bandwidth pipeline;
// each op issues a single 1 MiB pread from the current cursor position.
static constexpr size_t SEQ_BUF_SIZE = 1048576;

// Iterations per CPU work unit.  Large enough to do real work per call,
// small enough that the tick loop can count many completions per 250ms.
static constexpr int CPU_ITERS = 18'400;

// ----------------------------------------------------------------------------
// do_cpu_work – one unit of CPU work: a tight arithmetic loop.
// Called repeatedly inside a tick until the tick window expires.
// ----------------------------------------------------------------------------
void do_cpu_int_work() {
  volatile uint64_t acc = 1;
  for (int i = 1; i <= CPU_ITERS; ++i) {
    acc = acc * (uint64_t)i ^ (acc >> 7);
  }
  (void)acc;
}

void do_cpu_work() {
  do_cpu_int_work();
}

void do_cpu_fp_work() {
  volatile double acc = 1.0;
  for (int i = 1; i <= CPU_ITERS; ++i) {
    acc = acc * 1.0001 - (double)i * 0.00001;
  }
  (void)acc;
}

void do_cpu_hash_work() {
  volatile uint32_t hash = 2166136261U; // FNV-1a offset basis
  for (int i = 1; i <= CPU_ITERS; ++i) {
    hash = (hash ^ i) * 16777619ULL;
  }
  (void)hash;
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
IoState open_io_file(const std::string &tmp_dir,
                     const std::string &io_mode,
                     int queue_depth,
                     size_t file_size) {
  IoState st;

  const char *worker_id_env = getenv("WORKER_ID");
  int id = worker_id_env ? atoi(worker_id_env) : (int)getpid();

  // Unique filename per worker process/ID — no coordination needed.
  char path[512];
  snprintf(path, sizeof(path), "%s/sm_io_%d.dat", tmp_dir.c_str(), id);
  st.path = path;

  // Check if we want to reuse the file, and if it exists with the correct size.
  const char *reuse_env = getenv("REUSE_FILE");
  bool reuse = reuse_env && strcmp(reuse_env, "1") == 0;
  bool file_exists_and_ok = false;
  if (reuse) {
    struct stat st_buf;
    if (stat(path, &st_buf) == 0) {
      if (st_buf.st_size == (off_t)file_size) {
        file_exists_and_ok = true;
      }
    }
  }

  // Aligned buffer required by O_DIRECT.
  if (posix_memalign(&st.buf, IO_BUF_SIZE, IO_BUF_SIZE) != 0)
    return st; // buf stays nullptr; caller checks fd < 0
  std::mt19937_64 init_rng(1337 + id);
  uint64_t *buf_ptr = static_cast<uint64_t *>(st.buf);
  for (size_t i = 0; i < IO_BUF_SIZE / sizeof(uint64_t); ++i) {
    buf_ptr[i] = init_rng();
  }

  // O_RDWR so the same fd serves both do_io_work (writes) and do_io_read_work.
  if (file_exists_and_ok) {
    st.fd = open(path, O_RDWR | O_DIRECT, 0600);
  } else {
    st.fd = open(path, O_RDWR | O_CREAT | O_TRUNC | O_DIRECT, 0600);
  }

  if (st.fd < 0) {
    free(st.buf);
    st.buf = nullptr;
    return st;
  }
#ifdef __APPLE__
  fcntl(st.fd, F_NOCACHE, 1);
#endif

  if (!file_exists_and_ok) {
    // Pre-allocate so writes are overwrites, not allocating appends.
    // fallocate is preferred (doesn't zero-fill on most filesystems);
    // ftruncate is the fallback for filesystems that don't support it.
#ifdef __APPLE__
    if (ftruncate(st.fd, (off_t)file_size) != 0) {
      close(st.fd);
      st.fd = -1;
      free(st.buf);
      st.buf = nullptr;
      unlink(path);
      return st;
    }
#else
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
#endif
  }

  st.file_size = file_size;
  st.num_blocks      = file_size / IO_BUF_SIZE;
  st.num_blocks_64k  = file_size / BUF_64K_SIZE;

  // ---- 64 KiB aligned buffer for rand_read_64k (Probe C) -------------------
  if (posix_memalign(&st.buf_64k, BUF_64K_SIZE, BUF_64K_SIZE) != 0)
    st.buf_64k = nullptr;

  // ---- 1 MiB sequential buffer for seq_read (Probe D) ----------------------
  // Aligned to IO_BUF_SIZE (4 KiB); O_DIRECT only requires alignment to the
  // logical block size, not to the transfer size.
  if (posix_memalign(&st.seq_buf, IO_BUF_SIZE, SEQ_BUF_SIZE) == 0) {
    uint64_t *p = static_cast<uint64_t *>(st.seq_buf);
    for (size_t i = 0; i < SEQ_BUF_SIZE / sizeof(uint64_t); ++i)
      p[i] = init_rng();
  }

  // Pre-fill the file with non-zero data so reads never hit unwritten extents
  // and so rand_write overwrites existing blocks (no metadata allocation on
  // the hot path).  Writing in 1 MiB chunks is fast even for large files.
  if (!file_exists_and_ok && st.seq_buf) {
    uint64_t *p = static_cast<uint64_t *>(st.seq_buf);
    static constexpr size_t WORDS_PER_SECTOR = 512 / sizeof(uint64_t);
    static constexpr size_t SECTORS = SEQ_BUF_SIZE / 512;
    for (off_t off = 0; off < (off_t)file_size; off += SEQ_BUF_SIZE) {
      for (size_t i = 0; i < SECTORS; ++i)
        p[i * WORDS_PER_SECTOR] = (uint64_t)off | (uint64_t)i;
      [[maybe_unused]] ssize_t ret =
          pwrite(st.fd, st.seq_buf, SEQ_BUF_SIZE, off);
    }
    fsync(st.fd);
  }

#ifdef HAS_URING
  st.queue_depth = queue_depth > 1024 ? 1024 : queue_depth;
  if (st.queue_depth > 1) {
    if (io_uring_queue_init(st.queue_depth, &st.ring, 0) == 0) {
      st.use_uring = true;
      st.ring_bufs = static_cast<void **>(calloc(st.queue_depth, sizeof(void *)));
      if (st.ring_bufs) {
        if (io_mode == "rand_read_64k") {
          st.ring_buf_size = BUF_64K_SIZE;
        } else if (io_mode == "seq_read") {
          st.ring_buf_size = SEQ_BUF_SIZE;
        } else {
          st.ring_buf_size = IO_BUF_SIZE;
        }
        for (int i = 0; i < st.queue_depth; ++i) {
          if (posix_memalign(&st.ring_bufs[i], IO_BUF_SIZE, st.ring_buf_size) != 0) {
            st.ring_bufs[i] = nullptr;
          } else {
            if (io_mode == "rand_write" || io_mode == "rand_rw" || io_mode == "rand_read_write") {
              std::mt19937_64 init_rng(1337 + id + i);
              uint64_t *buf_ptr = static_cast<uint64_t *>(st.ring_bufs[i]);
              for (size_t j = 0; j < st.ring_buf_size / sizeof(uint64_t); ++j) {
                buf_ptr[j] = init_rng();
              }
            }
          }
        }
      }
    }
  }
#else
  (void)io_mode;
  (void)queue_depth;
#endif

  return st;
}

// ----------------------------------------------------------------------------
// do_io_work – one read-modify-write (RMW) op on a random 4 KiB block.
//
// Backs the "RW_balanced" io_mode: each op is exactly one read of a 4 KiB
// block from a random aligned offset, an in-memory mutation of the first
// 8-byte word of each of the 8 sectors (defeats sub-block dedup so the write
// leg actually hits the device), and a write of the same buffer back to the
// same offset.  The whole read+modify+write is counted as a single op so
// io_ops/sec is directly comparable to pure-read or pure-write IOPS at the
// "logical operation" level (with the caveat that each op consumes both a
// read and a write at the device).
//
// Symmetric counterparts:
//   do_io_read_work  – 4 KiB pure read       (R-only)
//   do_io_write_work – 4 KiB pure write      (W-only, used by rand_write)
//
// pread/pwrite are used instead of lseek+read/write so the file-position
// state is untouched (no serialisation point if multiple threads ever share
// the same fd).
// ----------------------------------------------------------------------------
void do_io_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;

  // Pick the offset once — both the read and the write target the same block.
  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);

  // ---- R leg: pull the 4 KiB block into our aligned buffer ----------------
  ssize_t r = pread(st.fd, st.buf, IO_BUF_SIZE, offset);
  if (r != (ssize_t)IO_BUF_SIZE) {
    perror("pread (RW_balanced)");
    abort();
  }

  // ---- M leg: mutate one 8-byte word per 512-byte sector ------------------
  // Same dedup-defeating trick used by do_io_write_work — without it a smart
  // storage layer could short-circuit the write when the buffer matches what
  // is already on disk.  8 fast RNG calls (~16 ns) are invisible next to the
  // ~10 µs NVMe round trip.
  uint64_t *buf_ptr = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 8; ++i) {
    buf_ptr[i * (512 / sizeof(uint64_t))] = rng();
  }

  // ---- W leg: push the mutated block back to the same offset --------------
  ssize_t w = pwrite(st.fd, st.buf, IO_BUF_SIZE, offset);
  if (w != (ssize_t)IO_BUF_SIZE) {
    perror("pwrite (RW_balanced)");
    abort();
  }
}

// ----------------------------------------------------------------------------
// do_io_write_work – issue one 4 KiB O_DIRECT write to a random aligned
// offset.  Pure write leg with no preceding read.
//
// Backs the "rand_write" io_mode (legacy default).  Mutates one 8-byte word
// per 512-byte sector before issuing pwrite to defeat block- and sub-block-
// level deduplication, then writes at a random 4 KiB-aligned offset within
// the pre-allocated file.  No fsync — matches the durability semantics of
// the read variants so rand_write measures raw storage-layer IOPS, not the
// durability pipeline.
// ----------------------------------------------------------------------------
void do_io_write_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;

  uint64_t *buf_ptr = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 8; ++i) {
    buf_ptr[i * (512 / sizeof(uint64_t))] = rng();
  }

  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);
  ssize_t ret = pwrite(st.fd, st.buf, IO_BUF_SIZE, offset);
  if (ret != (ssize_t)IO_BUF_SIZE) {
    perror("pwrite");
    abort();
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
  if (st.buf_64k) {
    free(st.buf_64k);
    st.buf_64k = nullptr;
  }
  if (st.seq_buf) {
    free(st.seq_buf);
    st.seq_buf = nullptr;
  }
  if (!st.path.empty()) {
    const char *reuse_env = getenv("REUSE_FILE");
    bool reuse = reuse_env && strcmp(reuse_env, "1") == 0;
    if (!reuse) {
      unlink(st.path.c_str());
    }
    st.path.clear();
  }

#ifdef HAS_URING
  if (st.use_uring) {
    io_uring_queue_exit(&st.ring);
    st.use_uring = false;
  }
  if (st.ring_bufs) {
    for (int i = 0; i < st.queue_depth; ++i) {
      if (st.ring_bufs[i]) {
        free(st.ring_bufs[i]);
      }
    }
    free(st.ring_bufs);
    st.ring_bufs = nullptr;
  }
#endif
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

void do_io_rw_work(IoState &st, std::mt19937_64 &rng) {
  if (rng() % 2 == 0) {
    do_io_read_work(st, rng);
  } else {
    do_io_work(st, rng);
  }
}

// ----------------------------------------------------------------------------
// do_io_read_64k_work – issue one 64 KiB O_DIRECT read from a random
// 64 KiB-aligned offset (Probe C).
//
// Compared to the 4 KiB rand_read, this shifts the bottleneck from command-
// issue rate toward sequential-read bandwidth.  Using a larger transfer exposes
// whether the device can saturate its internal read pipeline even when the
// number of outstanding commands is lower.
// ----------------------------------------------------------------------------
void do_io_read_64k_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf_64k || st.num_blocks_64k == 0)
    return;

  const off_t offset = (off_t)((rng() % st.num_blocks_64k) * BUF_64K_SIZE);
  [[maybe_unused]] ssize_t n = pread(st.fd, st.buf_64k, BUF_64K_SIZE, offset);
}

// ----------------------------------------------------------------------------
// do_io_seq_read_work – issue one 1 MiB O_DIRECT sequential read at the
// current cursor position (Probe D).
//
// Each call reads a contiguous 1 MiB chunk and advances the cursor, wrapping
// at file_size.  The large transfer size saturates the NVMe's sequential-read
// bandwidth pipeline; the bottleneck shifts entirely to read bandwidth rather
// than command-issue latency or IOPS.
// ----------------------------------------------------------------------------
void do_io_seq_read_work(IoState &st) {
  if (st.fd < 0 || !st.seq_buf)
    return;

  const off_t offset = (off_t)st.seq_cursor;
  [[maybe_unused]] ssize_t n = pread(st.fd, st.seq_buf, SEQ_BUF_SIZE, offset);

  st.seq_cursor += SEQ_BUF_SIZE;
  if (st.seq_cursor + SEQ_BUF_SIZE > st.file_size)
    st.seq_cursor = 0;
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
  st.n = MEM_BUF_DOUBLES;
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
void do_mem_work(MemState &st, const std::string &mem_mode) {
  if (!st.buf)
    return;

  double *lo = st.buf;
  double *hi = st.buf + st.n;

  if (mem_mode == "mem_read") {
    // Read-only memory access
    volatile double sum = 0.0;
    for (size_t i = 0; i < 2 * st.n; ++i) {
      sum += st.buf[i];
    }
  } else if (mem_mode == "mem_write") {
    // Write-only memory access
    for (size_t i = 0; i < 2 * st.n; ++i) {
      st.buf[i] = 3.0;
    }
  } else {
    // default: mem_copy / mem_stream
    // Pass 1: read from hi half, write to lo half.
    for (size_t i = 0; i < st.n; ++i)
      lo[i] = MEM_SCALAR * hi[i];

    // Pass 2: read from lo half, write to hi half.
    for (size_t i = 0; i < st.n; ++i)
      hi[i] = MEM_SCALAR * lo[i];
  }
}

// ----------------------------------------------------------------------------
// close_mem_buf – release the buffer allocated by open_mem_buf.
// ----------------------------------------------------------------------------
void close_mem_buf(MemState &st) {
  free(st.buf);
  st.buf = nullptr;
  st.n = 0;
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
  const size_t fsize = (params.file_size > 0) ? params.file_size : IO_FILE_SIZE;
  IoState io_state = open_io_file(params.tmp_dir, params.io_mode, params.queue_depth, fsize);
  if (io_state.fd < 0) {
    fprintf(stderr, "[worker] open_io_file failed for dir %s\n",
            params.tmp_dir.c_str());
  }

  MemState mem_state = open_mem_buf();

  WorkloadResult res{};
  const auto start = std::chrono::steady_clock::now();
  const auto warmup_deadline = start + std::chrono::seconds(params.warmup_secs);
  const auto deadline =
      warmup_deadline + std::chrono::seconds(params.duration_secs);

  auto measure_start = start;
  bool in_warmup = (params.warmup_secs > 0);

  while (std::chrono::steady_clock::now() < deadline) {
    if (in_warmup && std::chrono::steady_clock::now() >= warmup_deadline) {
      res.cpu_ops = 0;
      res.io_ops = 0;
      res.mem_ops = 0;
      res.sleep_ops = 0;
      measure_start = std::chrono::steady_clock::now();
      in_warmup = false;
    }
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
#ifdef HAS_URING
        // RW_balanced is RMW (read → mutate buffer → write to the same offset).
        // The mutate step lives in user space, so we cannot just link a read
        // and write SQE at the kernel level.  Until we add per-slot phase
        // tracking (issue read → on-CQE mutate buffer → issue write), fall
        // back to the synchronous path for this mode.
        if (io_state.use_uring && params.io_mode != "RW_balanced") {
          int in_flight = 0;
          int free_slots[1024];
          int free_count = io_state.queue_depth;
          for (int i = 0; i < io_state.queue_depth; ++i) {
            free_slots[i] = i;
          }

          while (std::chrono::steady_clock::now() < tick_end || in_flight > 0) {
            bool tick_active = (std::chrono::steady_clock::now() < tick_end);
            while (tick_active && free_count > 0) {
              int slot = free_slots[--free_count];

              struct io_uring_sqe *sqe = io_uring_get_sqe(&io_state.ring);
              if (!sqe) {
                free_slots[free_count++] = slot;
                break;
              }

              off_t offset = 0;
              size_t len = io_state.ring_buf_size;
              void *buf = io_state.ring_bufs[slot];

              if (params.io_mode == "rand_read") {
                offset = (off_t)((rng() % io_state.num_blocks) * IO_BUF_SIZE);
                io_uring_prep_read(sqe, io_state.fd, buf, len, offset);
              } else if (params.io_mode == "rand_read_64k") {
                offset = (off_t)((rng() % io_state.num_blocks_64k) * BUF_64K_SIZE);
                io_uring_prep_read(sqe, io_state.fd, buf, len, offset);
              } else if (params.io_mode == "seq_read") {
                offset = (off_t)io_state.seq_cursor;
                io_uring_prep_read(sqe, io_state.fd, buf, len, offset);
                io_state.seq_cursor += SEQ_BUF_SIZE;
                if (io_state.seq_cursor + SEQ_BUF_SIZE > io_state.file_size) {
                  io_state.seq_cursor = 0;
                }
              } else if (params.io_mode == "rand_rw" || params.io_mode == "rand_read_write") {
                if (rng() % 2 == 0) {
                  offset = (off_t)((rng() % io_state.num_blocks) * IO_BUF_SIZE);
                  io_uring_prep_read(sqe, io_state.fd, buf, len, offset);
                } else {
                  uint64_t *buf_ptr = static_cast<uint64_t *>(buf);
                  for (int i = 0; i < 8; ++i) {
                    buf_ptr[i * (512 / sizeof(uint64_t))] = rng();
                  }
                  offset = (off_t)((rng() % io_state.num_blocks) * IO_BUF_SIZE);
                  io_uring_prep_write(sqe, io_state.fd, buf, len, offset);
                }
              } else {
                // rand_write
                uint64_t *buf_ptr = static_cast<uint64_t *>(buf);
                for (int i = 0; i < 8; ++i) {
                  buf_ptr[i * (512 / sizeof(uint64_t))] = rng();
                }
                offset = (off_t)((rng() % io_state.num_blocks) * IO_BUF_SIZE);
                io_uring_prep_write(sqe, io_state.fd, buf, len, offset);
              }

              io_uring_sqe_set_data(sqe, reinterpret_cast<void *>(static_cast<uintptr_t>(slot)));
              ++in_flight;
            }

            if (in_flight > 0) {
              io_uring_submit(&io_state.ring);
            }

            bool must_wait = (free_count == 0 || (!tick_active && in_flight > 0));
            struct io_uring_cqe *cqe = nullptr;

            if (must_wait) {
              int ret = io_uring_wait_cqe(&io_state.ring, &cqe);
              if (ret < 0) {
                continue;
              }
            } else {
              io_uring_peek_cqe(&io_state.ring, &cqe);
            }

            while (cqe) {
              int slot = static_cast<int>(reinterpret_cast<uintptr_t>(io_uring_cqe_get_data(cqe)));
              if (cqe->res < 0) {
                fprintf(stderr, "io_uring operation failed: %s\n", strerror(-cqe->res));
                abort();
              } else if (static_cast<size_t>(cqe->res) != io_state.ring_buf_size) {
                fprintf(stderr, "io_uring short read/write: %d bytes (expected %zu)\n",
                        cqe->res, io_state.ring_buf_size);
                abort();
              }

              free_slots[free_count++] = slot;
              --in_flight;
              ++res.io_ops;

              io_uring_cqe_seen(&io_state.ring, cqe);
              cqe = nullptr;
              io_uring_peek_cqe(&io_state.ring, &cqe);
            }
          }
        } else
#endif
        {
          while (std::chrono::steady_clock::now() < tick_end) {
            if (params.io_mode == "rand_read") {
              do_io_read_work(io_state, rng);
            } else if (params.io_mode == "rand_read_64k") {
              do_io_read_64k_work(io_state, rng);
            } else if (params.io_mode == "seq_read") {
              do_io_seq_read_work(io_state);
            } else if (params.io_mode == "rand_rw" || params.io_mode == "rand_read_write") {
              do_io_rw_work(io_state, rng);
            } else if (params.io_mode == "RW_balanced") {
              // RMW: 1 op = pread 4 KiB + mutate sector words + pwrite back.
              do_io_work(io_state, rng);
            } else {
              // default: rand_write (legacy pure-write path)
              do_io_write_work(io_state, rng);
            }
            ++res.io_ops;
          }
        }
      } else if (n < params.io_mix + params.mem_mix) {
        // MEM phase: hammer memory bandwidth
        while (std::chrono::steady_clock::now() < tick_end) {
          do_mem_work(mem_state, params.mem_mode);
          ++res.mem_ops;
        }
      } else {
        // CPU phase: hammer CPU flavor until the tick window closes.
        while (std::chrono::steady_clock::now() < tick_end) {
          if (params.cpu_mode == "cpu_fp") {
            do_cpu_fp_work();
          } else if (params.cpu_mode == "cpu_hash") {
            do_cpu_hash_work();
          } else {
            do_cpu_int_work();
          }
          ++res.cpu_ops;
        }
      }
    }
  }

  close_io_file(io_state);
  close_mem_buf(mem_state);

  const auto finish = std::chrono::steady_clock::now();
  res.elapsed_secs =
      std::chrono::duration<double>(finish - measure_start).count();
  res.throughput = (res.cpu_ops + res.io_ops + res.mem_ops) / res.elapsed_secs;
  res.cpu_throughput = res.cpu_ops / res.elapsed_secs;
  res.io_throughput = res.io_ops / res.elapsed_secs;
  res.mem_throughput = res.mem_ops / res.elapsed_secs;
  return res;
}
