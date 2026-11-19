# One image, two jobs: `skynet serve` (the page) and `skynet respond` (the operator).
FROM docker.io/library/golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod *.go ./
COPY web ./web
RUN CGO_ENABLED=0 go build -trimpath -o /skynet .

FROM docker.io/library/alpine:3.21
# ns-brain's installer checks the release signature before it installs anything
RUN apk add --no-cache ca-certificates curl openssl \
 && curl -fsSL https://brain.fightclub.pro/install.sh | sh -s -- --dir /usr/local/bin \
 && apk del curl openssl
RUN adduser -D -u 10001 skynet \
 && mkdir -p /data /brains/skynet /brains/operator \
 && chown skynet /data /brains/skynet /brains/operator
COPY --from=build /skynet /usr/local/bin/skynet
COPY setup /opt/skynet/setup
USER skynet
ENV NS_BRAIN_NO_UPDATE_CHECK=1 TZ=UTC
WORKDIR /opt/skynet
CMD ["skynet", "serve"]
