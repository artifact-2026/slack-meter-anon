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

// Size of each I/O operation write (4 KiB).
static constexpr size_t IO_BUF_SIZE = 4096;

// Iterations per CPU work unit.  Large enough to do real work per call,
// small enough that the tick loop can count many completions per 250ms.
static constexpr int CPU_ITERS = 50'000;

// ----------------------------------------------------------------------------
// do_cpu_work – one unit of CPU work: a tight arithmetic loop.
// Called repeatedly inside a tick until the tick window expires.
// Returns the number of completed iterations (always CPU_ITERS).
// ----------------------------------------------------------------------------
void do_cpu_work() {
    volatile uint64_t acc = 1;
    for (int i = 1; i <= CPU_ITERS; ++i) {
        acc = acc * (uint64_t)i ^ (acc >> 7);
    }
    (void)acc;
}

// ----------------------------------------------------------------------------
// do_io_work – write a 4 KiB buffer to a temp file with fsync, then unlink.
// Using fsync ensures the write actually hits the storage stack.
// ----------------------------------------------------------------------------
void do_io_work(const std::string& tmp_dir) {
    char path[512];
    snprintf(path, sizeof(path), "%s/sm_io_%d_%ld.tmp",
             tmp_dir.c_str(), (int)getpid(), (long)random());

    char buf[IO_BUF_SIZE];
    memset(buf, 0xAB, sizeof(buf));

    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;
    if (write(fd, buf, sizeof(buf)) == (ssize_t)sizeof(buf)) {
        fsync(fd);
    }
    close(fd);
    unlink(path);
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
// Counting completions within each tick (rather than one op per tick) gives
// throughput that reflects real work done and saturates naturally under load.
// ----------------------------------------------------------------------------
WorkloadResult run_workload(const WorkloadParams& params) {
    std::mt19937_64 rng(params.seed);
    std::uniform_real_distribution<double> dist(0.0, 1.0);

    WorkloadResult res{};
    const auto start    = std::chrono::steady_clock::now();
    const auto deadline = start + std::chrono::seconds(params.duration_secs);

    while (std::chrono::steady_clock::now() < deadline) {
        const auto tick_end = std::chrono::steady_clock::now()
                            + std::chrono::milliseconds(TICK_MS);

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
                // I/O phase: keep issuing fsync writes until the tick window closes.
                while (std::chrono::steady_clock::now() < tick_end) {
                    do_io_work(params.tmp_dir);
                    ++res.io_ops;
                }
            }
        }
    }

    const auto finish   = std::chrono::steady_clock::now();
    res.elapsed_secs    = std::chrono::duration<double>(finish - start).count();
    res.throughput      = (res.cpu_ops + res.io_ops) / res.elapsed_secs;
    res.cpu_throughput  = res.cpu_ops / res.elapsed_secs;
    res.io_throughput   = res.io_ops  / res.elapsed_secs;
    return res;
}
