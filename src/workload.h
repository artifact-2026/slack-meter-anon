#pragma once

#include <cstdint>
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
    double   throughput;  // (cpu_ops + io_ops) / elapsed_secs
};

// Run the workload described by params and return result.
WorkloadResult run_workload(const WorkloadParams& params);

// Individual operations (exposed for unit testing / benchmarking)
// do_cpu_work performs one fixed-size unit of arithmetic work.
// Called in a loop inside a tick window to accumulate ops continuously.
void do_cpu_work();
void do_io_work(const std::string& tmp_dir);
