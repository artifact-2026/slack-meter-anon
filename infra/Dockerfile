# ── Build stage ──────────────────────────────────────────────────────────────
FROM ubuntu:22.04 AS builder

RUN apt-get update -y && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY CMakeLists.txt .
COPY src/ src/

RUN cmake -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --parallel "$(nproc)"


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM ubuntu:22.04

RUN apt-get update -y && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-dev \
        ca-certificates \
        sysstat \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Python analysis deps
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app

# Copy the compiled worker binary
COPY --from=builder /src/build/worker /app/build/worker

# Copy scripts
COPY scripts/ /app/scripts/

# Default scratch dir for I/O work
RUN mkdir -p /tmp/slack-meter /app/results

ENV PYTHONUNBUFFERED=1

ENV SKIP_BUILD=1

ENTRYPOINT ["bash", "/app/scripts/run_saturation_sweep.sh"]
