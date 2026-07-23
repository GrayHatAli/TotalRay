"""Shared networking constants/helpers for talking to sing-box's own
transparent-redirect exclusion mechanism.

sing-box's tun inbound (with auto_redirect) sets up Linux policy routing
roughly like this (see `ip rule show` on a running gateway):

    9000: from all fwmark <OUTPUT_MARK> goto 9002   # sing-box's own traffic - skip TUN
    9001: from all fwmark <INPUT_MARK>  lookup 2022 # force into TUN
    9002: from all nop                              # (fallthrough)
    32766: from all lookup main                     # real default gateway
    32768: from all lookup 2022                     # TUN's default route

Because rule 32766 (main, the real gateway) is checked before rule 32768
(the TUN), any packet that is NOT explicitly forced into the TUN by
INPUT_MARK, and IS explicitly marked with OUTPUT_MARK, falls straight
through to the real gateway - genuinely direct, not "hoping the tunnel
happens to be healthy".

sing-box uses this to keep its own outbound connections (to the actual
proxy servers) from looping back into its own TUN. vpnman reuses the
exact same mark for its own traffic (subscription fetching) for the
same reason: it must never depend on the tunnel it is itself building.

builder.py pins both marks explicitly on the tun inbound (rather than
relying on sing-box's undocumented defaults), so this module is the
single source of truth both sides read from.

IMPORTANT: marking sockets is not enough on its own. Python's own name
resolution (socket.getaddrinfo, used internally by every HTTP client
before it ever opens a connection) is a thin wrapper around the C
library's getaddrinfo(3), which does its own low-level socket I/O
*inside libc* - it never goes through Python's socket.socket(), so
marking that class does nothing for DNS lookups. bypass_tun() therefore
also does its own minimal DNS-over-UDP query (using a socket it marks
itself) and feeds the result back in place of the real getaddrinfo()
call. TLS SNI / certificate hostname checks are unaffected by this: they
are driven by the *hostname string* urllib3 was given, not by whatever
address it was resolved to.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import struct

log = logging.getLogger("vpnman.net")

# sing-box's own documented defaults - pinned explicitly in builder.py too.
REDIRECT_INPUT_MARK = "0x2023"
REDIRECT_OUTPUT_MARK = "0x2024"
REDIRECT_OUTPUT_MARK_INT = int(REDIRECT_OUTPUT_MARK, 16)

# SO_MARK isn't always exposed as a socket module constant depending on the
# Python build; 36 is its fixed value on Linux (linux/asm-generic/socket.h).
_SO_MARK = getattr(socket, "SO_MARK", 36)

_real_socket = socket.socket
_real_getaddrinfo = socket.getaddrinfo


def _marked_udp_socket(timeout: float) -> socket.socket:
    s = _real_socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, _SO_MARK, REDIRECT_OUTPUT_MARK_INT)
    except OSError as exc:
        log.debug("could not set SO_MARK on DNS socket: %s", exc)
    s.settimeout(timeout)
    return s


def _build_query(hostname: str, qid: int) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # RD=1, 1 question
    question = b""
    for label in hostname.strip(".").encode("ascii").split(b"."):
        question += bytes([len(label)]) + label
    question += b"\x00" + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    return header + question


def _skip_name(buf: bytes, offset: int) -> int:
    """Advance past a (possibly compressed) DNS name, return new offset."""
    while True:
        length = buf[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:  # compression pointer, always 2 bytes
            return offset + 2
        offset += 1 + length


def _dns_query_a(hostname: str, server: str, timeout: float = 5.0) -> str | None:
    """Resolve one A record for `hostname` via `server`, using a socket
    marked with REDIRECT_OUTPUT_MARK so the query itself bypasses the TUN.
    Returns the first IPv4 address found, or None on any failure (NXDOMAIN,
    timeout, malformed response, ...) - callers fall back to the normal
    resolver in that case rather than hard-failing the whole request.
    """
    qid = 0x2024  # arbitrary, matches our mark for easy log correlation
    query = _build_query(hostname, qid)
    try:
        sock = _marked_udp_socket(timeout)
        try:
            sock.sendto(query, (server, 53))
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()

        resp_id, flags, qdcount, ancount = struct.unpack(">HHHH", data[:8])
        if resp_id != qid or ancount == 0:
            return None
        rcode = flags & 0x000F
        if rcode != 0:  # NXDOMAIN, SERVFAIL, ...
            return None

        offset = 12
        for _ in range(qdcount):
            offset = _skip_name(data, offset) + 4  # + QTYPE/QCLASS

        for _ in range(ancount):
            offset = _skip_name(data, offset)
            rtype, _rclass, _ttl, rdlength = struct.unpack(
                ">HHIH", data[offset:offset + 10])
            offset += 10
            if rtype == 1 and rdlength == 4:  # A record
                return socket.inet_ntoa(data[offset:offset + 4])
            offset += rdlength
        return None
    except (OSError, struct.error, IndexError) as exc:
        log.debug("direct DNS query for %s via %s failed: %s", hostname, server, exc)
        return None


class _MarkedSocket(_real_socket):
    """A socket subclass that tags every new socket with REDIRECT_OUTPUT_MARK
    as soon as it is created, before any connect()/sendto() happens."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.setsockopt(socket.SOL_SOCKET, _SO_MARK, REDIRECT_OUTPUT_MARK_INT)
        except OSError as exc:
            # Not root, not Linux, or the kernel doesn't support SO_MARK.
            # Fail open: the socket is simply not marked, and traffic goes
            # out through whatever the transparent TUN would otherwise do
            # with it (same behaviour as before this feature existed).
            log.debug("could not set SO_MARK on socket: %s", exc)


@contextlib.contextmanager
def bypass_tun(dns_server: str | None = None):
    """Context manager: every socket created inside this block is marked
    so sing-box's own routing rules send it out the real default gateway
    instead of the TUN. If `dns_server` is given, hostname resolution is
    ALSO done manually (see module docstring for why that's necessary) via
    that server, marked the same way; numeric addresses and any hostname
    that our manual resolver can't handle transparently fall back to the
    normal system resolver (which may or may not itself be tunnelled -
    that fallback path is what the caller's own retry/proxy logic is for).
    """
    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if dns_server and family in (0, socket.AF_INET):
            ip = _dns_query_a(host, dns_server)
            if ip:
                return _real_getaddrinfo(ip, port, family, type, proto, flags)
        return _real_getaddrinfo(host, port, family, type, proto, flags)

    socket.socket = _MarkedSocket
    socket.getaddrinfo = patched_getaddrinfo
    try:
        yield
    finally:
        socket.socket = _real_socket
        socket.getaddrinfo = _real_getaddrinfo
