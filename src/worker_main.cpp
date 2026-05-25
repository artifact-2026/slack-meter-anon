//
// worker – standalone process that runs one workload instance and prints a
// single JSON object to stdout when done.
//
// Usage:
//   worker [--io-mix <float>] [--intensity <float>]
//          [--duration <secs>] [--tmp-dir <path>]
//
// Output (stdout):
//   {"cpu_ops":..., "io_ops":..., "sleep_ops":...,
//    "elapsed_secs":..., "throughput":...,
//    "io_mix":..., "intensity":...}
//

#include "workload.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

static void usage(const char* prog) {
    fprintf(stderr,
        "Usage: %s [--io-mix <float>] [--mem-mix <float>] [--intensity <float>]\n"
        "          [--duration <secs>] [--tmp-dir <path>]\n",
        prog);
}

int main(int argc, char* argv[]) {
    WorkloadParams params;
    params.io_mix        = 0.3;
    params.mem_mix       = 0.0;
    params.intensity     = 0.75;
    params.duration_secs = 30;
    params.warmup_secs   = 5;
    params.tmp_dir       = "/tmp/slack-meter";
    params.seed          = 42;
    params.io_mode       = "rand_write";

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--io-mix") == 0 && i + 1 < argc) {
            params.io_mix = atof(argv[++i]);
        } else if (strcmp(argv[i], "--mem-mix") == 0 && i + 1 < argc) {
            params.mem_mix = atof(argv[++i]);
        } else if (strcmp(argv[i], "--intensity") == 0 && i + 1 < argc) {
            params.intensity = atof(argv[++i]);
        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            params.duration_secs = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--warmup") == 0 && i + 1 < argc) {
            params.warmup_secs = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--tmp-dir") == 0 && i + 1 < argc) {
            params.tmp_dir = argv[++i];
        } else if (strcmp(argv[i], "--seed") == 0 && i + 1 < argc) {
            params.seed = (uint64_t)strtoull(argv[++i], nullptr, 10);
        } else if (strcmp(argv[i], "--io-mode") == 0 && i + 1 < argc) {
            params.io_mode = argv[++i];
        } else {
            usage(argv[0]);
            return 1;
        }
    }

    // Ensure tmp dir exists (best-effort; orchestrator should create it first)
    char mkdircmd[600];
    snprintf(mkdircmd, sizeof(mkdircmd), "mkdir -p %s", params.tmp_dir.c_str());
    if (system(mkdircmd) != 0) {
        fprintf(stderr, "[worker] warning: could not mkdir -p %s\n", params.tmp_dir.c_str());
    }

    const WorkloadResult r = run_workload(params);

    printf("{"
           "\"cpu_ops\":%lu,"
           "\"io_ops\":%lu,"
           "\"mem_ops\":%lu,"
           "\"sleep_ops\":%lu,"
           "\"elapsed_secs\":%.4f,"
           "\"throughput\":%.2f,"
           "\"cpu_throughput\":%.2f,"
           "\"io_throughput\":%.2f,"
           "\"mem_throughput\":%.2f,"
           "\"io_mix\":%.4f,"
           "\"mem_mix\":%.4f,"
           "\"intensity\":%.4f"
           "}\n",
           (unsigned long)r.cpu_ops,
           (unsigned long)r.io_ops,
           (unsigned long)r.mem_ops,
           (unsigned long)r.sleep_ops,
           r.elapsed_secs,
           r.throughput,
           r.cpu_throughput,
           r.io_throughput,
           r.mem_throughput,
           params.io_mix,
           params.mem_mix,
           params.intensity);

    return 0;
}
