// Package control implements the daemon's Unix-socket control API: a
// newline-delimited JSON request/response protocol for start/stop/
// add_outbound/remove_outbound/is_running, as specified in
// SINGBOX_CORE_EMBED_PLAN.md Phase 1.
package control

import "encoding/json"

// Request is one line of the control protocol.
//
//	{"cmd":"start","config":{...}}
//	{"cmd":"stop"}
//	{"cmd":"add_outbound","config":{...}}
//	{"cmd":"remove_outbound","tag":"..."}
//	{"cmd":"is_running"}
type Request struct {
	Cmd    string          `json:"cmd"`
	Config json.RawMessage `json:"config,omitempty"`
	Tag    string          `json:"tag,omitempty"`
}

// Response is one line of the control protocol, sent back for every
// Request. Exactly one of Error (on failure) or the success fields is set.
type Response struct {
	OK      bool   `json:"ok"`
	Error   string `json:"error,omitempty"`
	Running bool   `json:"running,omitempty"` // set on a successful is_running
}

func ok() Response {
	return Response{OK: true}
}

func okRunning(running bool) Response {
	return Response{OK: true, Running: running}
}

func fail(err error) Response {
	return Response{OK: false, Error: err.Error()}
}
