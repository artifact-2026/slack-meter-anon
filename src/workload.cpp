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

// Size of each sequential I/O operation (32 KiB).
static constexpr size_t SEQ_IO_BUF_SIZE = 32768;

// Iterations per CPU work unit.
static constexpr int CPU_ITERS = 18'400;

// Pre-fill chunk size: 1 MiB — used only in open_io_file to initialise the
// scratch file.  Not stored in IoState; a local buffer is allocated and freed.
static constexpr size_t FILL_CHUNK = 1048576;

// Legacy transfer sizes (used by the legacy I/O ops at the bottom of this
// file).
static constexpr size_t BUF_64K_SIZE = 65536;   // rand_read_64k
static constexpr size_t SEQ_BUF_SIZE = 1048576; // seq_read (1 MiB sequential)

// ----------------------------------------------------------------------------
// CPU work variants
// ----------------------------------------------------------------------------
void do_cpu_int_work() {
  volatile uint64_t acc = 1;
  for (int i = 1; i <= CPU_ITERS; ++i) {
    acc = acc * (uint64_t)i ^ (acc >> 7);
  }
  (void)acc;
}

void do_cpu_work() { do_cpu_int_work(); }

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
// open_io_file – open a per-worker scratch file for O_DIRECT I/O.
//
// Steps:
//   1. Build a filename from tmp_dir + WORKER_ID (unique per worker process).
//   2. Open with O_RDWR | O_DIRECT (O_CREAT | O_TRUNC on first creation).
//   3. Pre-allocate file_size bytes with fallocate (falls back to ftruncate).
//   4. Pre-fill with non-zero data (ensures reads never hit unwritten extents).
//   5. Allocate a 4 KiB posix_memalign'd buffer for all I/O ops.
// ----------------------------------------------------------------------------
IoState open_io_file(const std::string &tmp_dir, const std::string &io_mode,
                     int queue_depth, size_t file_size) {
  IoState st;

  const char *worker_id_env = getenv("WORKER_ID");
  int id = worker_id_env ? atoi(worker_id_env) : (int)getpid();

  char path[512];
  snprintf(path, sizeof(path), "%s/sm_io_%d.dat", tmp_dir.c_str(), id);
  st.path = path;

  const char *reuse_env = getenv("REUSE_FILE");
  bool reuse = reuse_env && strcmp(reuse_env, "1") == 0;
  bool file_exists_and_ok = false;
  if (reuse) {
    struct stat st_buf;
    if (stat(path, &st_buf) == 0 && st_buf.st_size == (off_t)file_size)
      file_exists_and_ok = true;
  }

  // Aligned buffer — shared by all four I/O variants (sized to 32 KiB to
  // support seq ops).
  if (posix_memalign(&st.buf, 4096, SEQ_IO_BUF_SIZE) != 0)
    return st;

  std::mt19937_64 init_rng(1337 + id);
  uint64_t *buf_ptr = static_cast<uint64_t *>(st.buf);
  for (size_t i = 0; i < SEQ_IO_BUF_SIZE / sizeof(uint64_t); ++i)
    buf_ptr[i] = init_rng();

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
#ifdef __APPLE__
    if (ftruncate(st.fd, (off_t)file_size) != 0) {
#else
    if (fallocate(st.fd, 0, 0, (off_t)file_size) != 0 &&
        ftruncate(st.fd, (off_t)file_size) != 0) {
#endif
      close(st.fd);
      st.fd = -1;
      free(st.buf);
      st.buf = nullptr;
      unlink(path);
      return st;
    }

    // Pre-fill with non-zero data using a temporary 1 MiB chunk buffer so
    // reads never hit unwritten extents and writes are always overwrites.
    void *fill_buf = nullptr;
    if (posix_memalign(&fill_buf, IO_BUF_SIZE, FILL_CHUNK) == 0) {
      uint64_t *p = static_cast<uint64_t *>(fill_buf);
      static constexpr size_t WORDS_PER_SECTOR = 512 / sizeof(uint64_t);
      static constexpr size_t SECTORS = FILL_CHUNK / 512;
      for (off_t off = 0; off < (off_t)file_size; off += (off_t)FILL_CHUNK) {
        for (size_t i = 0; i < SECTORS; ++i)
          p[i * WORDS_PER_SECTOR] = (uint64_t)off | (uint64_t)i;
        [[maybe_unused]] ssize_t ret = pwrite(st.fd, fill_buf, FILL_CHUNK, off);
      }
      fsync(st.fd);
      free(fill_buf);
    }
  }

  st.file_size = file_size;
  st.num_blocks = file_size / IO_BUF_SIZE;
  st.num_blocks_64k = file_size / BUF_64K_SIZE;
  st.seq_cursor = 0;

  // Legacy large-transfer buffers — allocated so the legacy ops work if called.
  if (posix_memalign(&st.buf_64k, BUF_64K_SIZE, BUF_64K_SIZE) != 0)
    st.buf_64k = nullptr;
  if (posix_memalign(&st.seq_buf, IO_BUF_SIZE, SEQ_BUF_SIZE) == 0) {
    uint64_t *p = static_cast<uint64_t *>(st.seq_buf);
    for (size_t i = 0; i < SEQ_BUF_SIZE / sizeof(uint64_t); ++i)
      p[i] = init_rng();
  }
  (void)io_mode;
  (void)queue_depth;

  return st;
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
    if (!reuse)
      unlink(st.path.c_str());
    st.path.clear();
  }
}

// ----------------------------------------------------------------------------
// Four fungible 4 KiB I/O operations
// ----------------------------------------------------------------------------

// pwrite 4 KiB to a random aligned offset (rand_write).
// Mutates one 8-byte word per 512-byte sector before writing to defeat
// sub-block deduplication at the storage layer.
void do_io_work_4k_rand_write(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  uint64_t *p = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 8; ++i)
    p[i * (512 / sizeof(uint64_t))] = rng();
  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);
  ssize_t ret = pwrite(st.fd, st.buf, IO_BUF_SIZE, offset);
  if (ret != (ssize_t)IO_BUF_SIZE) {
    perror("pwrite rand_write");
    abort();
  }

  // Read target sleep padding from environment
  static const char *pad_env = getenv("WRITE_PAD_NS");
  static const long long pad_ns = pad_env ? std::atoll(pad_env) : 0;
  if (pad_ns > 0) {
    std::this_thread::sleep_for(std::chrono::nanoseconds(pad_ns));
  }
}

// pread 4 KiB from a random aligned offset (rand_read).
void do_io_work_4k_rand_read(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);
  [[maybe_unused]] ssize_t n = pread(st.fd, st.buf, IO_BUF_SIZE, offset);
}

// pwrite 32 KiB to a random aligned offset (rand_write_32k).
// Mutates sector words before writing (same dedup-defeat as rand_write).
void do_io_work_32k_rand_write(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  uint64_t *p = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 64; ++i)
    p[i * (512 / sizeof(uint64_t))] = rng();
  const size_t num_blocks_32k = st.file_size / SEQ_IO_BUF_SIZE;
  if (num_blocks_32k == 0)
    return;
  const off_t offset = (off_t)((rng() % num_blocks_32k) * SEQ_IO_BUF_SIZE);
  ssize_t ret = pwrite(st.fd, st.buf, SEQ_IO_BUF_SIZE, offset);
  if (ret != (ssize_t)SEQ_IO_BUF_SIZE) {
    perror("pwrite rand_write_32k");
    abort();
  }

  // Read target sleep padding from environment
  static const char *pad_env = getenv("WRITE_PAD_NS");
  static const long long pad_ns = pad_env ? std::atoll(pad_env) : 0;
  if (pad_ns > 0) {
    std::this_thread::sleep_for(std::chrono::nanoseconds(pad_ns));
  }
}

// pread 32 KiB from a random aligned offset (rand_read_32k).
void do_io_work_32k_rand_read(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  const size_t num_blocks_32k = st.file_size / SEQ_IO_BUF_SIZE;
  if (num_blocks_32k == 0)
    return;
  const off_t offset = (off_t)((rng() % num_blocks_32k) * SEQ_IO_BUF_SIZE);
  [[maybe_unused]] ssize_t n = pread(st.fd, st.buf, SEQ_IO_BUF_SIZE, offset);
}

// pwrite 4 KiB at seq_cursor; advance cursor by IO_BUF_SIZE, wrap at file_size.
// Mutates sector words before writing (same dedup-defeat as rand_write).
void do_io_work_4k_seq_write(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  uint64_t *p = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 8; ++i)
    p[i * (512 / sizeof(uint64_t))] = rng();
  const off_t offset = (off_t)st.seq_cursor;
  ssize_t ret = pwrite(st.fd, st.buf, IO_BUF_SIZE, offset);
  if (ret != (ssize_t)IO_BUF_SIZE) {
    perror("pwrite seq_write_4k");
    abort();
  }
  st.seq_cursor += IO_BUF_SIZE;
  if (st.seq_cursor >= st.file_size)
    st.seq_cursor = 0;
}

// pread 4 KiB at seq_cursor; advance cursor by IO_BUF_SIZE, wrap at file_size.
void do_io_work_4k_seq_read(IoState &st) {
  if (st.fd < 0 || !st.buf)
    return;
  const off_t offset = (off_t)st.seq_cursor;
  [[maybe_unused]] ssize_t n = pread(st.fd, st.buf, IO_BUF_SIZE, offset);
  st.seq_cursor += IO_BUF_SIZE;
  if (st.seq_cursor >= st.file_size)
    st.seq_cursor = 0;
}

// pwrite 32 KiB at seq_cursor; advance cursor by SEQ_IO_BUF_SIZE, wrap at
// file_size. Mutates sector words before writing (same dedup-defeat as
// rand_write).
void do_io_work_32k_seq_write(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  uint64_t *p = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 64; ++i)
    p[i * (512 / sizeof(uint64_t))] = rng();
  const off_t offset = (off_t)st.seq_cursor;
  ssize_t ret = pwrite(st.fd, st.buf, SEQ_IO_BUF_SIZE, offset);
  if (ret != (ssize_t)SEQ_IO_BUF_SIZE) {
    perror("pwrite seq_write_32k");
    abort();
  }
  st.seq_cursor += SEQ_IO_BUF_SIZE;
  if (st.seq_cursor >= st.file_size)
    st.seq_cursor = 0;
}

// pread 32 KiB at seq_cursor; advance cursor by SEQ_IO_BUF_SIZE, wrap at
// file_size.
void do_io_work_32k_seq_read(IoState &st) {
  if (st.fd < 0 || !st.buf)
    return;
  const off_t offset = (off_t)st.seq_cursor;
  [[maybe_unused]] ssize_t n = pread(st.fd, st.buf, SEQ_IO_BUF_SIZE, offset);
  st.seq_cursor += SEQ_IO_BUF_SIZE;
  if (st.seq_cursor >= st.file_size)
    st.seq_cursor = 0;
}

// ----------------------------------------------------------------------------
// Memory bandwidth variants
// ----------------------------------------------------------------------------
static constexpr double MEM_SCALAR = 3.0;

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

void do_mem_work(MemState &st, const std::string &mem_mode) {
  if (!st.buf)
    return;
  double *lo = st.buf;
  double *hi = st.buf + st.n;
  if (mem_mode == "mem_read") {
    volatile double sum = 0.0;
    for (size_t i = 0; i < 2 * st.n; ++i)
      sum += st.buf[i];
  } else if (mem_mode == "mem_write") {
    for (size_t i = 0; i < 2 * st.n; ++i)
      st.buf[i] = 3.0;
  } else {
    // default: mem_copy / STREAM-scale
    for (size_t i = 0; i < st.n; ++i)
      lo[i] = MEM_SCALAR * hi[i];
    for (size_t i = 0; i < st.n; ++i)
      hi[i] = MEM_SCALAR * lo[i];
  }
}

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
//      If n < io_mix     →  I/O phase: dispatch to one of the four I/O ops.
//      Else if n < io_mix + mem_mix → MEM phase.
//      Else              →  CPU phase.
// ----------------------------------------------------------------------------
WorkloadResult run_workload(const WorkloadParams &params) {
  std::mt19937_64 rng(params.seed);
  std::uniform_real_distribution<double> dist(0.0, 1.0);

  const size_t fsize = (params.file_size > 0) ? params.file_size : IO_FILE_SIZE;
  IoState io_state =
      open_io_file(params.tmp_dir, params.io_mode, params.queue_depth, fsize);
  if (io_state.fd < 0)
    fprintf(stderr, "[worker] open_io_file failed for dir %s\n",
            params.tmp_dir.c_str());

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
      res.cpu_ops = res.io_ops = res.mem_ops = res.sleep_ops = 0;
      measure_start = std::chrono::steady_clock::now();
      in_warmup = false;
    }

    const auto tick_end =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(TICK_MS);

    const double m = dist(rng);
    if (m > params.intensity) {
      std::this_thread::sleep_for(std::chrono::milliseconds(TICK_MS));
      ++res.sleep_ops;
    } else {
      const double n = dist(rng);
      if (n < params.io_mix) {
        // ---- I/O phase ------------------------------------------------------
        // Synchronous path — all four I/O modes.
        while (std::chrono::steady_clock::now() < tick_end) {
          if (params.io_mode == "rand_read") {
            do_io_work_4k_rand_read(io_state, rng);
          } else if (params.io_mode == "rand_read_32k") {
            do_io_work_32k_rand_read(io_state, rng);
          } else if (params.io_mode == "rand_write") {
            do_io_work_4k_rand_write(io_state, rng);
          } else if (params.io_mode == "rand_write_32k") {
            do_io_work_32k_rand_write(io_state, rng);
          } else if (params.io_mode == "seq_write_4k") {
            do_io_work_4k_seq_write(io_state, rng);
          } else if (params.io_mode == "seq_write") {
            do_io_work_32k_seq_write(io_state, rng);
          } else if (params.io_mode == "seq_read_4k") {
            do_io_work_4k_seq_read(io_state);
          } else if (params.io_mode == "seq_read") {
            do_io_work_32k_seq_read(io_state);
          } else if (params.io_mode == "rw_mixed") {
            do_io_rw_mixed_work(io_state, rng);
          } else {
            // default: rand_write
            do_io_work_4k_rand_write(io_state, rng);
          }
          ++res.io_ops;
        }

      } else if (n < params.io_mix + params.mem_mix) {
        // ---- MEM phase ------------------------------------------------------
        while (std::chrono::steady_clock::now() < tick_end) {
          do_mem_work(mem_state, params.mem_mode);
          ++res.mem_ops;
        }
      } else {
        // ---- CPU phase ------------------------------------------------------
        while (std::chrono::steady_clock::now() < tick_end) {
          if (params.cpu_mode == "cpu_fp")
            do_cpu_fp_work();
          else if (params.cpu_mode == "cpu_hash")
            do_cpu_hash_work();
          else
            do_cpu_int_work();
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

// ============================================================================
// Legacy I/O operations — preserved for reference / future use.
// Not dispatched by run_workload() under any current io_mode string.
// ============================================================================

// RMW: pread 4 KiB, mutate sector words, pwrite back to same offset
// (RW_balanced).
void do_io_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);
  ssize_t r = pread(st.fd, st.buf, IO_BUF_SIZE, offset);
  if (r != (ssize_t)IO_BUF_SIZE) {
    perror("pread (RW_balanced)");
    abort();
  }
  uint64_t *p = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 8; ++i)
    p[i * (512 / sizeof(uint64_t))] = rng();
  ssize_t w = pwrite(st.fd, st.buf, IO_BUF_SIZE, offset);
  if (w != (ssize_t)IO_BUF_SIZE) {
    perror("pwrite (RW_balanced)");
    abort();
  }
}

// 4 KiB pure random write (legacy rand_write name).
void do_io_write_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  uint64_t *p = static_cast<uint64_t *>(st.buf);
  for (int i = 0; i < 8; ++i)
    p[i * (512 / sizeof(uint64_t))] = rng();
  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);
  ssize_t ret = pwrite(st.fd, st.buf, IO_BUF_SIZE, offset);
  if (ret != (ssize_t)IO_BUF_SIZE) {
    perror("pwrite");
    abort();
  }
}

// 4 KiB pure random read (legacy rand_read name).
void do_io_read_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf)
    return;
  const off_t offset = (off_t)((rng() % st.num_blocks) * IO_BUF_SIZE);
  [[maybe_unused]] ssize_t n = pread(st.fd, st.buf, IO_BUF_SIZE, offset);
}

// 50/50 random read or RMW write (rand_rw).
void do_io_rw_work(IoState &st, std::mt19937_64 &rng) {
  if (rng() % 2 == 0)
    do_io_read_work(st, rng);
  else
    do_io_work(st, rng);
}

// 4 random reads + 1 random write (R_heavy).
void do_io_r_heavy_work(IoState &st, std::mt19937_64 &rng) {
  for (int i = 0; i < 4; ++i)
    do_io_read_work(st, rng);
  do_io_write_work(st, rng);
}

// 1 random read + 1 random write + 1 fdatasync (W_heavy).
void do_io_w_heavy_work(IoState &st, std::mt19937_64 &rng) {
  do_io_read_work(st, rng);
  do_io_write_work(st, rng);
  if (st.fd >= 0)
    fdatasync(st.fd);
}

// 50/50 random read or random write (rw_mixed).
void do_io_rw_mixed_work(IoState &st, std::mt19937_64 &rng) {
  if (rng() % 2 == 0)
    do_io_read_work(st, rng);
  else
    do_io_write_work(st, rng);
}

// 64 KiB O_DIRECT read from a random 64 KiB-aligned offset (rand_read_64k).
void do_io_read_64k_work(IoState &st, std::mt19937_64 &rng) {
  if (st.fd < 0 || !st.buf_64k || st.num_blocks_64k == 0)
    return;
  const off_t offset = (off_t)((rng() % st.num_blocks_64k) * BUF_64K_SIZE);
  [[maybe_unused]] ssize_t n = pread(st.fd, st.buf_64k, BUF_64K_SIZE, offset);
}

// 1 MiB O_DIRECT sequential read; cursor advances by SEQ_BUF_SIZE (seq_read
// legacy).
void do_io_seq_read_work(IoState &st) {
  if (st.fd < 0 || !st.seq_buf)
    return;
  const off_t offset = (off_t)st.seq_cursor;
  [[maybe_unused]] ssize_t n = pread(st.fd, st.seq_buf, SEQ_BUF_SIZE, offset);
  st.seq_cursor += SEQ_BUF_SIZE;
  if (st.seq_cursor + SEQ_BUF_SIZE > st.file_size)
    st.seq_cursor = 0;
}
