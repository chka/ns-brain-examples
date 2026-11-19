# Dockerfile: one image for both jobs, `skynet serve` (the page) and `skynet respond` (the
# operator). compose.yaml runs it twice with different commands.
#
# It is a MULTI-STAGE build. The first stage has the whole Go toolchain (hundreds of megabytes)
# and compiles the program; the second stage starts again from a small Alpine Linux image and
# copies in only the finished binary. The toolchain, the source and the build cache never reach
# the image you run, which keeps it small and gives an attacker who got in far less to work with.

# ---- Stage 1: build ----
# Pinned to a Go minor version and an Alpine release so a rebuild next year produces the same
# thing. docker.io/library/ is spelled out so the file also works with Podman, which does not
# assume Docker Hub.
FROM docker.io/library/golang:1.23-alpine AS build
WORKDIR /src
# Copy only what the build needs. The program uses nothing outside the Go standard library,
# so there is no go.sum and no dependency download step.
COPY go.mod *.go ./
COPY web ./web
# CGO_ENABLED=0 produces a fully static binary that needs no C library at run time.
# -trimpath removes this machine's file paths from the binary, so builds are reproducible
# and do not leak the builder's directory layout.
RUN CGO_ENABLED=0 go build -trimpath -o /skynet .

# ---- Stage 2: run ----
FROM docker.io/library/alpine:3.21

# Install ns-brain. Its installer downloads the release, checks the signature on the release's
# checksum file against the release keys and refuses to install anything that does not match;
# it needs openssl for that check and curl for the download. Both are removed again in the
# SAME RUN step: every RUN creates an image layer and a package deleted in a later step would
# still be inside an earlier layer. ca-certificates stays, because respond.go makes HTTPS calls
# to the chat API at run time.
RUN apk add --no-cache ca-certificates curl openssl \
 && curl -fsSL https://brain.fightclub.pro/install.sh | sh -s -- --dir /usr/local/bin \
 && apk del curl openssl

# Never run as root. adduser -D creates a user with no password, so nobody can log in as it.
# The directories the program writes to are created here and given to that user. When Docker
# mounts an empty named volume over them for the first time, it copies this ownership onto
# the volume, which is how the unprivileged user can write to /data and the brains.
RUN adduser -D -u 10001 skynet \
 && mkdir -p /data /brains/skynet /brains/operator \
 && chown skynet /data /brains/skynet /brains/operator

COPY --from=build /skynet /usr/local/bin/skynet
COPY setup /opt/skynet/setup
USER skynet
# NS_BRAIN_NO_UPDATE_CHECK: do not look for a newer ns-brain on every call; an image is
# upgraded by rebuilding it. TZ=UTC: every timestamp the program writes is in UTC.
ENV NS_BRAIN_NO_UPDATE_CHECK=1 TZ=UTC
# The README's commands say `setup/brains.sh`, relative to this directory.
WORKDIR /opt/skynet
# The default job; compose.yaml overrides it with `skynet respond` for the operator container.
CMD ["skynet", "serve"]
