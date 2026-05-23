package health

import (
	"testing"
	"time"
)

func TestTracker_IsHealthy(t *testing.T) {
	tracker := NewTracker()

	// Unknown chain should be healthy
	if !tracker.IsHealthy(0) {
		t.Error("Unknown chain should be healthy")
	}

	// Mark as unhealthy after 3 failures
	tracker.MarkUnhealthy(0)
	tracker.MarkUnhealthy(0)
	tracker.MarkUnhealthy(0)
	if tracker.IsHealthy(0) {
		t.Error("Chain with 3 failures should be unhealthy")
	}

	// Mark as healthy
	tracker.MarkHealthy(0)
	if !tracker.IsHealthy(0) {
		t.Error("Chain should be healthy after MarkHealthy")
	}
}

func TestTracker_GetHealthyChains(t *testing.T) {
	tracker := NewTracker()

	// All chains should be healthy initially
	healthy := tracker.GetHealthyChains(3)
	if len(healthy) != 3 {
		t.Errorf("Expected 3 healthy chains, got %d", len(healthy))
	}

	// Mark one as unhealthy
	tracker.MarkUnhealthy(1)
	healthy = tracker.GetHealthyChains(3)
	if len(healthy) != 2 {
		t.Errorf("Expected 2 healthy chains, got %d", len(healthy))
	}
}

func TestTracker_StateTransitions(t *testing.T) {
	tracker := NewTracker()

	// Test failure counting
	tracker.MarkUnhealthy(0)
	tracker.MarkUnhealthy(0)
	if tracker.states[0].Failures != 2 {
		t.Errorf("Expected 2 failures, got %d", tracker.states[0].Failures)
	}

	// Test recovery resets failures
	tracker.MarkHealthy(0)
	if tracker.states[0].Failures != 0 {
		t.Errorf("Expected 0 failures after recovery, got %d", tracker.states[0].Failures)
	}
	if !tracker.states[0].LastSuccess.After(time.Time{}) {
		t.Error("LastSuccess should be set after MarkHealthy")
	}
}
