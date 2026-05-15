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

  st.fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_DIRECT, 0600);
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

  st.file_size = file_size;
  st.num_blocks = file_size / IO_BUF_SIZE;
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
  if (!st.path.empty()) {
    unlink(st.path.c_str());
    st.path.clear();
  }
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
      if (n > params.io_mix) {
        // CPU phase: hammer do_cpu_work() until the tick window closes.
        while (std::chrono::steady_clock::now() < tick_end) {
          do_cpu_work();
          ++res.cpu_ops;
        }
      } else {
        // I/O phase: keep issuing O_DIRECT pwrite ops until the tick closes.
        while (std::chrono::steady_clock::now() < tick_end) {
          do_io_work(io_state, rng);
          ++res.io_ops;
        }
      }
    }
  }

  close_io_file(io_state);

  const auto finish = std::chrono::steady_clock::now();
  res.elapsed_secs = std::chrono::duration<double>(finish - start).count();
  res.throughput = (res.cpu_ops + res.io_ops) / res.elapsed_secs;
  res.cpu_throughput = res.cpu_ops / res.elapsed_secs;
  res.io_throughput = res.io_ops / res.elapsed_secs;
  return res;
}
