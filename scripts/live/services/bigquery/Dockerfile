# syntax=docker/dockerfile:1.4@sha256:9ba7531bd80fb0a858632727cf7a112fbfd19b17e94c4e84ced81e24ef1a0dbc
FROM --platform=$BUILDPLATFORM golang:1.25.3-bookworm@sha256:4f43b271f9673eb7bd0cb3a49cc17b08d8d6ee110277e26dbacc93c43a5a7793 AS builder
ARG TARGETARCH
WORKDIR /build
ENV GOTOOLCHAIN=local GOMAXPROCS=2 GOMEMLIMIT=2GiB CGO_ENABLED=0
ADD https://codeload.github.com/goccy/bigquery-emulator/tar.gz/a531d3deb716eaba4972f9afa88e03e2c0f1a1af /tmp/upstream.tar.gz
RUN echo 'af6584f6fe47066c150b096093a60e1d9ae6d1f9f261c654206574aaef219fe7  /tmp/upstream.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/upstream.tar.gz --strip-components=1 -C /build
COPY patches/ /patches/
RUN if [ -s /patches/SHA256SUMS ]; then (cd /patches && sha256sum -c SHA256SUMS); fi \
    && while IFS= read -r name; do \
         if [ -n "$name" ]; then \
           git apply --check --whitespace=error-all "/patches/$name" \
             && git apply --whitespace=error-all "/patches/$name" || exit 1; \
         fi; \
       done < /patches/series
COPY healthcheck.go /healthcheck.go
# Keep downloaded modules and compiler intermediates out of persistent image layers.
# Both architectures cross-compile on the native builder; no emulation is needed.
RUN --mount=type=tmpfs,target=/go/pkg/mod --mount=type=tmpfs,target=/root/.cache/go-build \
    go mod download \
    && GOOS=linux GOARCH=$TARGETARCH go build -trimpath -buildvcs=false \
       -ldflags '-s -w -X main.version=0.8.1-framework.1 -X main.revision=a531d3deb716eaba4972f9afa88e03e2c0f1a1af' \
       -o /out/bigquery-emulator ./cmd/bigquery-emulator \
    && GOOS=linux GOARCH=$TARGETARCH go build -trimpath -buildvcs=false \
       -ldflags '-s -w' -o /out/bigquery-healthcheck /healthcheck.go

FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
COPY --from=builder /out/ /bin/
COPY --from=builder /build/LICENSE /licenses/bigquery-emulator.LICENSE
COPY upstream.lock /work/upstream.lock
LABEL org.opencontainers.image.source="https://github.com/goccy/bigquery-emulator" \
      org.opencontainers.image.revision="a531d3deb716eaba4972f9afa88e03e2c0f1a1af" \
      org.opencontainers.image.version="0.8.1-framework.1"
WORKDIR /work
HEALTHCHECK --interval=2s --timeout=5s --start-period=2s --retries=30 CMD ["/bin/bigquery-healthcheck"]
ENTRYPOINT ["/bin/bigquery-emulator"]
