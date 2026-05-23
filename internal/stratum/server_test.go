package stratum

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/blakecoin/merged-mine-proxy/internal/share"
	"github.com/blakecoin/merged-mine-proxy/internal/work"
)

func TestReverseWordOrderMatchesEloipoolSwap32(t *testing.T) {
	prev := "00000000000000000000000000000000000000000000000000000000deadbeef"
	got := reverseWordOrder(prev)
	want := "deadbeef00000000000000000000000000000000000000000000000000000000"
	if got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

func TestServerDefaults(t *testing.T) {
	s := &Server{}
	if s.maxSessions() != 2048 {
		t.Fatalf("max sessions = %d", s.maxSessions())
	}
	if s.difficulty() != 1 {
		t.Fatalf("difficulty = %d", s.difficulty())
	}
	if s.authTimeout() <= 0 || s.readIdleTimeout() <= 0 || s.writeTimeout() <= 0 {
		t.Fatal("expected positive default timeouts")
	}
}

func TestMaxSessionLimitClosesExcessConnection(t *testing.T) {
	addr := freeAddr(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s := &Server{Addr: addr, Pool: fakePool{}, MaxSessions: 1, AuthTimeout: time.Second}
	errCh := make(chan error, 1)
	go func() { errCh <- s.ListenAndServe(ctx) }()

	conn1 := dialRetry(t, addr)
	defer conn1.Close()
	waitForSessions(t, s, 1)
	if s.sessions.Load() != 1 {
		t.Fatalf("sessions = %d, want 1", s.sessions.Load())
	}

	conn2 := dialRetry(t, addr)
	defer conn2.Close()
	_ = conn2.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
	buf := make([]byte, 1)
	if _, err := conn2.Read(buf); err == nil {
		t.Fatal("expected excess connection to close")
	}

	cancel()
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("server did not stop")
	}
}

type fakePool struct{}

func (fakePool) CurrentJob(string) *work.Job  { return nil }
func (fakePool) RegisterMiner(string, string) {}
func (fakePool) SubmitShare(context.Context, share.Submission) share.Result {
	return share.Rejected("test")
}

func freeAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().String()
}

func dialRetry(t *testing.T, addr string) net.Conn {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		conn, err := net.Dial("tcp", addr)
		if err == nil {
			return conn
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("server did not listen on %s", addr)
	return nil
}

func waitForSessions(t *testing.T, s *Server, want int64) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if s.sessions.Load() == want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("sessions = %d, want %d", s.sessions.Load(), want)
}
