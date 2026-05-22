.PHONY: build test run clean

BINARY_POOL=eliopool
BINARY_PROXY=merged-mine-proxy

build:
	go build -o bin/$(BINARY_POOL) ./cmd/eliopool
	go build -o bin/$(BINARY_PROXY) ./cmd/merged-mine-proxy

test:
	go test -v ./...

run: build
	./bin/$(BINARY_POOL)

clean:
	rm -rf bin/
