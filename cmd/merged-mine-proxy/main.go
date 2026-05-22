package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/SidGrip/eliopool-15.21-go/internal/config"
	"github.com/SidGrip/eliopool-15.21-go/internal/server"
)

const version = "0.1.0-go-15.21"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "--version" {
		fmt.Println(version)
		os.Exit(0)
	}

	cfg, err := config.ParseFlags(os.Args[1:])
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error parsing flags: %v\n", err)
		os.Exit(1)
	}

	// Setup logging
	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))

	// Write PID file if requested
	if cfg.PIDFile != "" {
		if err := os.WriteFile(cfg.PIDFile, []byte(fmt.Sprintf("%d", os.Getpid())), 0644); err != nil {
			logger.Error("Failed to write PID file", "error", err)
		}
	}

	// Setup log file if requested
	if cfg.LogFile != "" {
		f, err := os.OpenFile(cfg.LogFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
		if err != nil {
			logger.Error("Failed to open log file", "error", err)
		} else {
			defer f.Close()
			logger = slog.New(slog.NewTextHandler(f, &slog.HandlerOptions{
				Level: slog.LevelDebug,
			}))
		}
	}

	listener, err := server.NewListener(cfg, logger)
	if err != nil {
		logger.Error("Failed to create listener", "error", err)
		os.Exit(1)
	}

	addr := fmt.Sprintf("127.0.0.1:%d", cfg.WorkerPort)
	logger.Info("Starting merged-mine-proxy", "version", version, "addr", addr)

	ln, err := net.Listen("tcp4", addr)
	if err != nil {
		logger.Error("Failed to bind HTTP listener", "addr", addr, "error", err)
		os.Exit(1)
	}
	backgroundCtx, stopBackground := context.WithCancel(context.Background())
	defer stopBackground()
	listener.StartBackground(backgroundCtx)
	srv := &http.Server{
		Handler:           listener,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      3 * time.Minute,
		IdleTimeout:       2 * time.Minute,
		MaxHeaderBytes:    16 << 10,
	}

	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			logger.Error("HTTP server error", "error", err)
			os.Exit(1)
		}
	}()

	// Wait for interrupt
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	logger.Info("Shutting down")
	stopBackground()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
