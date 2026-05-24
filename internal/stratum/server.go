package stratum

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/blakecoin/merged-mine-proxy/internal/block"
	"github.com/blakecoin/merged-mine-proxy/internal/share"
	"github.com/blakecoin/merged-mine-proxy/internal/work"
)

type Pool interface {
	CurrentJob(username string) *work.Job
	RegisterMiner(username, remote string)
	SubmitShare(context.Context, share.Submission) share.Result
}

// Server speaks the small Stratum subset needed by Blake-256 ASIC miners:
// subscribe, authorize, submit, and periodic mining.notify work updates.
type Server struct {
	Addr            string
	Pool            Pool
	Logger          *slog.Logger
	MaxSessions     int
	Difficulty      int
	AuthTimeout     time.Duration
	ReadIdleTimeout time.Duration
	WriteTimeout    time.Duration
	nextID          atomic.Uint64
	sessions        atomic.Int64
}

type request struct {
	ID     interface{}       `json:"id"`
	Method string            `json:"method"`
	Params []json.RawMessage `json:"params"`
}

type response struct {
	ID     interface{} `json:"id"`
	Result interface{} `json:"result"`
	Error  interface{} `json:"error"`
}

type session struct {
	id         uint64
	extraNonce string
	username   string
	remote     string
	userAgent  string
	server     *Server
	conn       net.Conn
	writer     *bufio.Writer
	writeMu    sync.Mutex
	done       chan struct{}
	notifying  bool
}

func (s *Server) ListenAndServe(ctx context.Context) error {
	ln, err := net.Listen("tcp", s.Addr)
	if err != nil {
		return err
	}
	defer ln.Close()
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	if s.Logger != nil {
		s.Logger.Info("stratum listening", "addr", s.Addr)
	}
	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		if max := s.maxSessions(); max > 0 && s.sessions.Load() >= int64(max) {
			if s.Logger != nil {
				s.Logger.Warn("stratum connection limit reached", "remote", conn.RemoteAddr().String(), "max_sessions", max)
			}
			_ = conn.Close()
			continue
		}
		// Each TCP session gets a stable extranonce1. Miners combine this with
		// their extranonce2 to make unique coinbases for submitted shares.
		id := s.nextID.Add(1)
		s.sessions.Add(1)
		sess := &session{
			id:         id,
			extraNonce: fmt.Sprintf("%08x", id),
			remote:     conn.RemoteAddr().String(),
			server:     s,
			conn:       conn,
			writer:     bufio.NewWriter(conn),
			done:       make(chan struct{}),
		}
		go func() {
			defer s.sessions.Add(-1)
			sess.run(ctx)
		}()
	}
}

func (s *session) run(ctx context.Context) {
	defer s.conn.Close()
	defer close(s.done)
	s.setReadDeadline(s.server.authTimeout())
	scanner := bufio.NewScanner(s.conn)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		select {
		case <-ctx.Done():
			return
		default:
		}
		var req request
		if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
			s.write(response{ID: nil, Error: []interface{}{20, "invalid-json", nil}})
			continue
		}
		s.handle(ctx, req)
		s.setReadDeadline(s.currentReadTimeout())
	}
	if err := scanner.Err(); err != nil && s.server.Logger != nil && ctx.Err() == nil {
		s.server.Logger.Debug("stratum session closed", "remote", s.remote, "error", err)
	}
}

func (s *session) handle(ctx context.Context, req request) {
	switch req.Method {
	case "mining.subscribe":
		// Tell the miner which notifications it will receive and provide
		// extranonce1 plus the extranonce2 byte count.
		if len(req.Params) > 0 {
			_ = json.Unmarshal(req.Params[0], &s.userAgent)
		}
		s.write(response{ID: req.ID, Result: []interface{}{
			[]interface{}{
				[]interface{}{"mining.set_difficulty", "diff"},
				[]interface{}{"mining.notify", "job"},
			},
			s.extraNonce,
			4,
		}})
	case "mining.authorize":
		// The username is the payout address/mining key, optionally with a
		// .worker suffix. Password is accepted but not used.
		var username, password string
		if len(req.Params) > 0 {
			_ = json.Unmarshal(req.Params[0], &username)
		}
		if len(req.Params) > 1 {
			_ = json.Unmarshal(req.Params[1], &password)
		}
		_ = password
		s.username = username
		s.server.Pool.RegisterMiner(username, s.remote)
		s.write(response{ID: req.ID, Result: true})
		s.setReadDeadline(s.server.readIdleTimeout())
		s.sendDifficulty(s.server.difficulty())
		// Send the first job immediately so a miner can start hashing without
		// waiting for the periodic notify loop.
		s.sendJob(true)
		if !s.notifying {
			s.notifying = true
			go s.notifyLoop(ctx)
		}
	case "mining.submit":
		// Submission params are username, job id, extranonce2, ntime, nonce.
		// Pool.SubmitShare rebuilds the block header and decides whether this
		// is a normal share, parent block, or auxpow candidate.
		var parts []string
		for _, raw := range req.Params {
			var value string
			_ = json.Unmarshal(raw, &value)
			parts = append(parts, value)
		}
		if len(parts) < 5 {
			s.write(response{ID: req.ID, Error: []interface{}{20, "malformed-submit", nil}})
			return
		}
		sub := share.Submission{
			Username:    parts[0],
			RemoteAddr:  s.remote,
			JobID:       parts[1],
			ExtraNonce1: s.extraNonce,
			ExtraNonce2: parts[2],
			NTime:       parts[3],
			Nonce:       parts[4],
			UserAgent:   s.userAgent,
			Received:    time.Now(),
		}
		result := s.server.Pool.SubmitShare(ctx, sub)
		if result.Accepted {
			s.write(response{ID: req.ID, Result: true})
			return
		}
		reason := result.RejectReason
		if reason == "" {
			reason = "rejected"
		}
		s.write(response{ID: req.ID, Error: []interface{}{20, reason, nil}})
	case "mining.extranonce.subscribe":
		s.write(response{ID: req.ID, Result: true})
	default:
		s.write(response{ID: req.ID, Error: []interface{}{20, "unknown-method", nil}})
	}
}

func (s *session) notifyLoop(ctx context.Context) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-s.done:
			return
		case <-ticker.C:
			s.sendJob(false)
		}
	}
}

func (s *session) sendDifficulty(diff int) {
	s.writeJSON(map[string]interface{}{
		"id":     nil,
		"method": "mining.set_difficulty",
		"params": []interface{}{diff},
	})
}

func (s *session) sendJob(clean bool) {
	job := s.server.Pool.CurrentJob(s.username)
	if job == nil || job.Template == nil {
		return
	}
	version := fmt.Sprintf("%08x", job.Template.Version)
	ntime := fmt.Sprintf("%08x", job.Template.CurTime)
	bits := job.Template.BitsHex
	prev := block.ReverseWordOrder(job.Template.PreviousBlockHex)
	branches := job.Branches
	if branches == nil {
		branches = []string{}
	}
	// Stratum wants the previous block hash as 32-bit words in wire order.
	// The work manager keeps normal display hex, so convert at send time.
	s.writeJSON(map[string]interface{}{
		"id":     nil,
		"method": "mining.notify",
		"params": []interface{}{
			job.ID,
			prev,
			job.Coinbase1,
			job.Coinbase2,
			branches,
			version,
			bits,
			ntime,
			clean,
		},
	})
}

func (s *session) write(resp response) {
	s.writeJSON(resp)
}

func (s *session) writeJSON(v interface{}) {
	blob, err := json.Marshal(v)
	if err != nil {
		return
	}
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	_ = s.conn.SetWriteDeadline(time.Now().Add(s.server.writeTimeout()))
	_, _ = s.writer.Write(blob)
	_ = s.writer.WriteByte('\n')
	_ = s.writer.Flush()
}

func (s *session) setReadDeadline(timeout time.Duration) {
	if timeout <= 0 {
		return
	}
	_ = s.conn.SetReadDeadline(time.Now().Add(timeout))
}

func (s *session) currentReadTimeout() time.Duration {
	if s.username == "" {
		return s.server.authTimeout()
	}
	return s.server.readIdleTimeout()
}

func (s *Server) maxSessions() int {
	if s.MaxSessions <= 0 {
		return 2048
	}
	return s.MaxSessions
}

func (s *Server) difficulty() int {
	if s.Difficulty <= 0 {
		return 1
	}
	return s.Difficulty
}

func (s *Server) authTimeout() time.Duration {
	if s.AuthTimeout <= 0 {
		return 30 * time.Second
	}
	return s.AuthTimeout
}

func (s *Server) readIdleTimeout() time.Duration {
	if s.ReadIdleTimeout <= 0 {
		return 10 * time.Minute
	}
	return s.ReadIdleTimeout
}

func (s *Server) writeTimeout() time.Duration {
	if s.WriteTimeout <= 0 {
		return 15 * time.Second
	}
	return s.WriteTimeout
}

func ParseDifficulty(v interface{}) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case string:
		i, _ := strconv.Atoi(n)
		return i
	default:
		return 1
	}
}
