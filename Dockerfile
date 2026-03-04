# ─── Stage 1: Go binaries ────────────────────────────────────────────────────
FROM golang:1.22-bookworm AS go-builder

WORKDIR /build

# sec-execd
COPY go/sec-execd/ ./go/sec-execd/
RUN cd go/sec-execd && \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/sec-execd .

# net-proxy
COPY go/net-proxy/ ./go/net-proxy/
RUN cd go/net-proxy && \
    CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/net-proxy .

# ─── Stage 2: Runtime image ───────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached)
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p zen_claw bridge && touch zen_claw/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf zen_claw bridge

# Copy the full source and re-install
COPY zen_claw/ zen_claw/
COPY bridge/ bridge/
RUN uv pip install --system --no-cache .

# Build the WhatsApp bridge
WORKDIR /app/bridge
RUN npm install && npm run build
WORKDIR /app

# Copy compiled Go binaries — placed in /app/bin/ so the sidecar supervisor
# finds them via its first candidate path (Path.cwd() / "bin" / binary_name)
COPY --from=go-builder /out/sec-execd bin/sec-execd
COPY --from=go-builder /out/net-proxy  bin/net-proxy

# Create config directory
RUN mkdir -p /root/.zen-claw

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["zen-claw"]
CMD ["status"]
