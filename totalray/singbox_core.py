"""Client for the embedded sing-box daemon's local control socket."""
from __future__ import annotations

import json
import socket


class SingboxCoreError(RuntimeError):
    """The embedded core rejected a control request."""


class SingboxCoreClient:
    def __init__(self, socket_path: str, timeout: float = 5.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, command: str, **fields) -> dict:
        payload = {"cmd": command, **fields}
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.timeout)
            conn.connect(self.socket_path)
            conn.sendall(line)
            response = self._readline(conn)
        try:
            result = json.loads(response)
        except (TypeError, ValueError) as exc:
            raise SingboxCoreError(f"invalid core response: {response!r}") from exc
        if not result.get("ok"):
            raise SingboxCoreError(result.get("error") or "singbox-core rejected request")
        return result

    @staticmethod
    def _readline(conn: socket.socket) -> str:
        chunks = []
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        if not chunks:
            raise SingboxCoreError("singbox-core closed the connection without a response")
        return b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")

    def is_running(self) -> bool:
        return bool(self.request("is_running").get("running"))

    def start(self, config: dict) -> None:
        self.request("start", config=config)

    def stop(self) -> None:
        self.request("stop")

    def add_outbound(self, config: dict) -> None:
        self.request("add_outbound", config=config)

    def remove_outbound(self, tag: str) -> None:
        self.request("remove_outbound", tag=tag)
