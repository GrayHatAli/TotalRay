from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any

log = logging.getLogger(__name__)


class RoundStateStore:
    """Concurrency-safe, atomic persistence for scheduler round state."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    value = json.load(fh)
            except (OSError, ValueError, TypeError):
                return {}
            return value if isinstance(value, dict) else {}

    def _write(self, state: dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".round-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(temporary, 0o664)
            os.replace(temporary, self.path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def update(self, kind: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            state = self.snapshot()
            current = state.get(kind) or {}
            current.update(fields)
            current["updated_at"] = time.time()
            state[kind] = current
            try:
                self._write(state)
            except OSError as exc:
                log.debug("could not write round status: %s", exc)
            return current.copy()

    def queue(self, kind: str, reason: str | None = None,
              blocked_by: str | None = None, **fields: Any) -> dict[str, Any]:
        values = {"state": "queued", "running": False, "reason": reason,
                  "blocked_by": blocked_by}
        values.update(fields)
        return self.update(kind, **values)

    def start(self, kind: str, round_id: str | None = None,
              total: int | None = None, **fields: Any) -> dict[str, Any]:
        if round_id is None:
            round_id = uuid.uuid4().hex[:8]
        values: dict[str, Any] = {
            "state": "running",
            "running": True,
            "round_id": round_id,
            "started_at": time.time(),
            "finished_at": None,
            "last_error": None,
            "blocked_by": None,
            "reason": None,
            "items_processed": 0,
            "items_ok": 0,
            "items_failed": 0,
        }
        if total is not None:
            values["items_total"] = total
        values.update(fields)
        if "round_id" in fields and fields["round_id"] is None:
            values["round_id"] = round_id
        return self.update(kind, **values)

    def progress(self, kind: str, processed: int | None = None,
                 ok: int | None = None, failed: int | None = None,
                 **fields: Any) -> dict[str, Any]:
        values: dict[str, Any] = {"state": "running", "running": True}
        if processed is not None:
            values["items_processed"] = processed
        if ok is not None:
            values["items_ok"] = ok
        if failed is not None:
            values["items_failed"] = failed
        # Accept the aliases the scheduler used historically.
        if "ok" in fields:
            values["items_ok"] = fields.pop("ok")
        if "failed" in fields:
            values["items_failed"] = fields.pop("failed")
        if "total" in fields:
            values["items_total"] = fields.pop("total")
        values.update(fields)
        return self.update(kind, **values)

    def finish(self, kind: str, success: bool = True,
               error: str | None = None, **fields: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "state": "idle" if success else "failed",
            "running": False,
            "finished_at": time.time(),
            "last_error": error,
        }
        values.update(fields)
        return self.update(kind, **values)

    def skip(self, kind: str, reason: str, blocked_by: str | None = None,
             **fields: Any) -> dict[str, Any]:
        values = {"state": "skipped", "running": False,
                  "reason": reason, "blocked_by": blocked_by,
                  "finished_at": time.time()}
        values.update(fields)
        return self.update(kind, **values)

    def recover(self) -> dict[str, Any]:
        with self._lock:
            state = self.snapshot()
            for value in state.values():
                if isinstance(value, dict) and value.get("running"):
                    recovered_at = time.time()
                    value.update({"state": "failed", "running": False,
                                  "reason": "daemon_restart",
                                  "last_error": "round interrupted by daemon restart",
                                  "finished_at": recovered_at,
                                  "recovered_at": recovered_at})
            try:
                self._write(state)
            except OSError as exc:
                log.debug("could not recover round status: %s", exc)
            return state
