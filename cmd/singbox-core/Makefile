# Build tags mirror the protocol/feature set the stock sing-box v1.13.14
# binary currently deployed on the Pi is built with (QUIC-based protocols
# for hysteria2/tuic, WireGuard endpoints, uTLS/REALITY, ACME, gVisor TUN
# stack, and the Clash API TotalRay's builder.py already talks to).
# with_reality_server was merged into with_utls upstream and must not be
# passed separately (sing-box v1.13.14+ fails the build if it is).
TAGS := with_quic,with_wireguard,with_utls,with_acme,with_gvisor,with_clash_api

.PHONY: build test vet fmt

build:
	go build -tags $(TAGS) -o singbox-core .

test:
	go test -tags $(TAGS) ./...

vet:
	go vet -tags $(TAGS) ./...

fmt:
	gofmt -l .
