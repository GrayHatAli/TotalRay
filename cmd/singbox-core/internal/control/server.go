package control

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"sync"
)

// maxLine caps a single request/response line. sing-box configs with many
// outbounds can run to tens of KB; 8MiB is generous headroom.
const maxLine = 8 << 20

// Controller is the subset of core.Core the control server needs. Defined
// here (rather than depending on the core package's concrete type) so
// server tests can exercise the protocol against a fake.
type Controller interface {
	Start(configJSON []byte) error
	Stop() error
	IsRunning() bool
	AddOutbound(configJSON []byte) error
	RemoveOutbound(tag string) error
}

// Server serves the control protocol over a Unix domain socket.
type Server struct {
	SocketPath string
	Controller Controller
	// SocketMode is the permission bits applied to the socket file after
	// listening starts. Defaults to 0660 if zero.
	SocketMode os.FileMode

	Logger *log.Logger

	mu       sync.Mutex
	listener net.Listener
	wg       sync.WaitGroup
	closed   bool
}

// Listen creates the Unix socket, removing any stale socket file left over
// from a previous, uncleanly-terminated run at the same path first.
func (s *Server) Listen() error {
	if s.SocketPath == "" {
		return errors.New("control: SocketPath must not be empty")
	}

	if err := removeStaleSocket(s.SocketPath); err != nil {
		return fmt.Errorf("remove stale socket: %w", err)
	}

	ln, err := net.Listen("unix", s.SocketPath)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", s.SocketPath, err)
	}

	mode := s.SocketMode
	if mode == 0 {
		mode = 0o660
	}
	if err := os.Chmod(s.SocketPath, mode); err != nil {
		_ = ln.Close()
		return fmt.Errorf("chmod socket: %w", err)
	}

	s.mu.Lock()
	s.listener = ln
	s.mu.Unlock()
	return nil
}

// removeStaleSocket unlinks path if it exists and looks like a leftover
// Unix socket (not some unrelated file a typo could otherwise destroy).
func removeStaleSocket(path string) error {
	fi, err := os.Stat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if fi.Mode().Type() != os.ModeSocket {
		return fmt.Errorf("%s exists and is not a socket, refusing to remove", path)
	}
	return os.Remove(path)
}

// Serve accepts connections until Close is called. Call Listen first.
func (s *Server) Serve() error {
	s.mu.Lock()
	ln := s.listener
	s.mu.Unlock()
	if ln == nil {
		return errors.New("control: Listen must be called before Serve")
	}

	for {
		conn, err := ln.Accept()
		if err != nil {
			s.mu.Lock()
			closed := s.closed
			s.mu.Unlock()
			if closed {
				s.wg.Wait()
				return nil
			}
			return fmt.Errorf("accept: %w", err)
		}
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			s.handleConn(conn)
		}()
	}
}

// Close stops accepting new connections, waits for in-flight ones to
// finish, and removes the socket file.
func (s *Server) Close() error {
	s.mu.Lock()
	s.closed = true
	ln := s.listener
	s.mu.Unlock()

	var err error
	if ln != nil {
		err = ln.Close()
	}
	s.wg.Wait()
	_ = os.Remove(s.SocketPath)
	return err
}

func (s *Server) handleConn(conn net.Conn) {
	defer conn.Close()

	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 0, 64*1024), maxLine)
	writer := bufio.NewWriter(conn)

	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		resp := s.dispatch(line)
		if err := writeResponse(writer, resp); err != nil {
			s.logf("write response: %v", err)
			return
		}
	}
	if err := scanner.Err(); err != nil {
		s.logf("read request: %v", err)
	}
}

func (s *Server) dispatch(line []byte) Response {
	var req Request
	if err := json.Unmarshal(line, &req); err != nil {
		return fail(fmt.Errorf("invalid request: %w", err))
	}

	switch req.Cmd {
	case "start":
		if len(req.Config) == 0 {
			return fail(errors.New("start requires config"))
		}
		if err := s.Controller.Start(req.Config); err != nil {
			return fail(err)
		}
		return ok()

	case "stop":
		if err := s.Controller.Stop(); err != nil {
			return fail(err)
		}
		return ok()

	case "add_outbound":
		if len(req.Config) == 0 {
			return fail(errors.New("add_outbound requires config"))
		}
		if err := s.Controller.AddOutbound(req.Config); err != nil {
			return fail(err)
		}
		return ok()

	case "remove_outbound":
		if req.Tag == "" {
			return fail(errors.New("remove_outbound requires tag"))
		}
		if err := s.Controller.RemoveOutbound(req.Tag); err != nil {
			return fail(err)
		}
		return ok()

	case "is_running":
		return okRunning(s.Controller.IsRunning())

	default:
		return fail(fmt.Errorf("unknown cmd %q", req.Cmd))
	}
}

func writeResponse(w *bufio.Writer, resp Response) error {
	b, err := json.Marshal(resp)
	if err != nil {
		return err
	}
	if _, err := w.Write(b); err != nil {
		return err
	}
	if err := w.WriteByte('\n'); err != nil {
		return err
	}
	return w.Flush()
}

func (s *Server) logf(format string, args ...any) {
	if s.Logger != nil {
		s.Logger.Printf(format, args...)
	}
}
