"""Networking helpers for TotalRay (socket marking and bypass helpers)."""
from __future__ import annotations

import contextlib
import logging
import socket
import struct

log = logging.getLogger("totalray.net")

REDIRECT_INPUT_MARK = "0x2023"
REDIRECT_OUTPUT_MARK = "0x2024"
REDIRECT_OUTPUT_MARK_INT = int(REDIRECT_OUTPUT_MARK, 16)

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
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    question = b""
    for label in hostname.strip(".").encode("ascii").split(b"."):
        question += bytes([len(label)]) + label
    question += b"\x00" + struct.pack(">HH", 1, 1)
    return header + question


def _skip_name(buf: bytes, offset: int) -> int:
    while True:
        length = buf[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length


def _dns_query_a(hostname: str, server: str, timeout: float = 5.0) -> str | None:
    qid = 0x2024
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
        if rcode != 0:
            return None

        offset = 12
        for _ in range(qdcount):
            offset = _skip_name(data, offset) + 4

        for _ in range(ancount):
            offset = _skip_name(data, offset)
            rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            if rtype == 1 and rdlength == 4:
                return socket.inet_ntoa(data[offset:offset + 4])
            offset += rdlength
        return None
    except (OSError, struct.error, IndexError) as exc:
        log.debug("direct DNS query for %s via %s failed: %s", hostname, server, exc)
        return None


class _MarkedSocket(_real_socket):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.setsockopt(socket.SOL_SOCKET, _SO_MARK, REDIRECT_OUTPUT_MARK_INT)
        except OSError as exc:
            log.debug("could not set SO_MARK on socket: %s", exc)


@contextlib.contextmanager
def bypass_tun(dns_server: str | None = None):
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
