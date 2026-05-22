package health

import (
	"sync"
	"time"
)

// State represents the health state of a chain
type State struct {
	Healthy            bool
	Failures           int
	LastSuccess        time.Time
	LastFailure        time.Time
	WasReportedHealthy bool
}

// Tracker manages health states for multiple chains
type Tracker struct {
	mu     sync.RWMutex
	states map[int]*State
}

// NewTracker creates a new health tracker
func NewTracker() *Tracker {
	return &Tracker{
		states: make(map[int]*State),
	}
}

// IsHealthy returns true if the chain is healthy
func (t *Tracker) IsHealthy(chainIdx int) bool {
	t.mu.RLock()
	defer t.mu.RUnlock()

	state, ok := t.states[chainIdx]
	if !ok {
		return true // unknown status - assume healthy initially
	}
	return state.Healthy && state.Failures < 3
}

// MarkHealthy marks a chain as healthy
func (t *Tracker) MarkHealthy(chainIdx int) {
	t.mu.Lock()
	defer t.mu.Unlock()

	state, ok := t.states[chainIdx]
	if !ok {
		state = &State{}
		t.states[chainIdx] = state
	}

	wasHealthy := state.Healthy
	state.Healthy = true
	state.Failures = 0
	state.LastSuccess = time.Now()
	state.WasReportedHealthy = true

	// Only log recovery if previously unhealthy
	if !wasHealthy {
		state.WasReportedHealthy = false
	}
}

// MarkUnhealthy marks a chain as unhealthy
func (t *Tracker) MarkUnhealthy(chainIdx int) {
	t.mu.Lock()
	defer t.mu.Unlock()

	state, ok := t.states[chainIdx]
	if !ok {
		state = &State{}
		t.states[chainIdx] = state
	}

	wasHealthy := state.Healthy
	state.Healthy = false
	state.Failures++
	state.LastFailure = time.Now()
	state.WasReportedHealthy = false

	// Only log on first failure
	_ = wasHealthy
}

// GetHealthyChains returns indices of healthy chains
func (t *Tracker) GetHealthyChains(totalChains int) []int {
	var healthy []int
	for i := 0; i < totalChains; i++ {
		if t.IsHealthy(i) {
			healthy = append(healthy, i)
		}
	}
	return healthy
}

// CountHealthy returns the number of healthy chains
func (t *Tracker) CountHealthy(totalChains int) int {
	return len(t.GetHealthyChains(totalChains))
}

// Snapshot returns a copy of the chain state. Unknown chains are treated as
// initially healthy to match the Python proxy behavior.
func (t *Tracker) Snapshot(chainIdx int) State {
	t.mu.RLock()
	defer t.mu.RUnlock()

	state, ok := t.states[chainIdx]
	if !ok {
		return State{Healthy: true}
	}
	return *state
}
