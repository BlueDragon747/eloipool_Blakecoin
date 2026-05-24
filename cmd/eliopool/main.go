package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/blakecoin/merged-mine-proxy/internal/pool"
)

const version = "0.2.5-go-25.2"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "--version" {
		fmt.Println(version)
		return
	}
	cfg, err := pool.Parse(os.Args[1:])
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error parsing flags: %v\n", err)
		os.Exit(2)
	}
	logWriter, closeLog, err := poolLogWriter(cfg.PoolLogPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error opening pool log: %v\n", err)
		os.Exit(1)
	}
	defer closeLog()
	logger := slog.New(slog.NewTextHandler(logWriter, &slog.HandlerOptions{Level: slog.LevelInfo}))
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

func poolLogWriter(path string) (io.Writer, func(), error) {
	path = strings.TrimSpace(path)
	if path == "" {
		return os.Stdout, func() {}, nil
	}
	if dir := filepath.Dir(path); dir != "." && dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, nil, err
		}
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, nil, err
	}
	return io.MultiWriter(os.Stdout, f), func() { _ = f.Close() }, nil
}
