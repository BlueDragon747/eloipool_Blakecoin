package share

import "time"

type Submission struct {
	Username    string
	RemoteAddr  string
	JobID       string
	ExtraNonce1 string
	ExtraNonce2 string
	NTime       string
	Nonce       string
	UserAgent   string
	Received    time.Time
}

type Result struct {
	Accepted       bool
	RejectReason   string
	UpstreamResult bool
	TargetHex      string
	Solution       string
}

func Accepted(targetHex, solution string) Result {
	return Result{Accepted: true, UpstreamResult: true, TargetHex: targetHex, Solution: solution}
}

func Rejected(reason string) Result {
	return Result{RejectReason: reason}
}
