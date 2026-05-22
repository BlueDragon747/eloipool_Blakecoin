package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/SidGrip/eliopool-15.21-go/internal/pool"
)

const version = "0.1.0-go-15.21"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "--version" {
		fmt.Println(version)
		return
	}
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	cfg, err := pool.Parse(os.Args[1:])
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error parsing flags: %v\n", err)
		os.Exit(2)
	}
	svc, err := pool.NewService(cfg, logger)
	if err != nil {
		logger.Error("failed to initialize pool", "error", err)
		os.Exit(1)
	}
	defer svc.Close()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := svc.Run(ctx); err != nil && ctx.Err() == nil {
		logger.Error("pool stopped", "error", err)
		os.Exit(1)
	}
}
