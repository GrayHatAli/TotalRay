// Command singbox-core is a standalone daemon embedding sing-box as a
// library, exposing start/stop/add-outbound/remove-outbound over a local
// Unix-socket control API. See SINGBOX_CORE_EMBED_PLAN.md Phase 1.
//
// It is fully isolated from production: nothing in totalray/builder.py
// calls it yet, and its default socket path lives under /run/totalray/,
// separate from the stock sing-box unit's Clash API and config.
package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/GrayHatAli/TotalRay/cmd/singbox-core/internal/control"
	"github.com/GrayHatAli/TotalRay/cmd/singbox-core/internal/core"
)

func main() {
	socketPath := flag.String("socket", "/run/totalray/singbox-core.sock", "control socket path")
	flag.Parse()

	logger := log.New(os.Stderr, "singbox-core: ", log.LstdFlags)

	c := core.New()
	srv := &control.Server{
		SocketPath: *socketPath,
		Controller: c,
		Logger:     logger,
	}

	if err := srv.Listen(); err != nil {
		logger.Fatalf("listen: %v", err)
	}
	logger.Printf("control socket listening on %s", *socketPath)

	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve() }()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	select {
	case err := <-serveErr:
		if err != nil {
			logger.Printf("serve: %v", err)
		}
	case sig := <-sigCh:
		logger.Printf("received %s, shutting down", sig)
	}

	if err := srv.Close(); err != nil {
		logger.Printf("close server: %v", err)
	}
	if c.IsRunning() {
		if err := c.Stop(); err != nil {
			logger.Printf("stop box: %v", err)
		}
	}
}
