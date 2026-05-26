cmake_minimum_required(VERSION 3.16)
project(slack-meter LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Optimised release build by default so CPU work is realistic
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(CMAKE_CXX_FLAGS_RELEASE "-O2")
add_compile_options(-Wall -Wextra -Wpedantic)

# ── libworkload ───────────────────────────────────────────────────────────────
add_library(workload STATIC src/workload.cpp)
target_include_directories(workload PUBLIC src)

# Check for liburing (needed for io_uring support on Linux)
find_library(URING_LIB uring)
if(URING_LIB)
  target_compile_definitions(workload PRIVATE HAS_URING)
  target_link_libraries(workload PUBLIC uring)
  message(STATUS "Found liburing: ${URING_LIB}, enabling io_uring support")
else()
  message(STATUS "liburing not found, falling back to synchronous I/O")
endif()

# ── worker binary ─────────────────────────────────────────────────────────────
add_executable(worker src/worker_main.cpp)
target_link_libraries(worker PRIVATE workload)

# Link pthreads (needed for std::thread on Linux)
find_package(Threads REQUIRED)
target_link_libraries(worker PRIVATE Threads::Threads)

# ── install targets (used inside Docker) ─────────────────────────────────────
install(TARGETS worker DESTINATION bin)
