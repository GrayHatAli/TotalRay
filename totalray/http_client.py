"""Centralized HTTP client with retry/backoff and failed-request logging."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


def _make_session(retries: int = 2, backoff_factor: float = 0.3) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=retries, backoff_factor=backoff_factor,
                  status_forcelist=(500, 502, 503, 504), allowed_methods=frozenset(['GET','POST','PUT','DELETE','HEAD','OPTIONS']))
    s.mount('https://', HTTPAdapter(max_retries=retry))
    s.mount('http://', HTTPAdapter(max_retries=retry))
    return s


class FailedRequestLogger:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger = logging.getLogger('totalray.failed_requests')
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            self.logger.addHandler(handler)

    def record(self, entry: dict[str, Any]) -> None:
        try:
            self.logger.info(json.dumps(entry, ensure_ascii=False))
        except Exception:
            log.debug('failed to write failed-request log')


class HTTPClient:
    def __init__(self, settings):
        self.settings = settings
        self.session = _make_session(retries=int(settings['test'].get('retries', 1)))
        self.failed_logger = FailedRequestLogger(os.path.join(settings.data_dir, 'totalray_failed_requests.log'))

    def get(self, url: str, timeout: int = 20, **kwargs) -> requests.Response:
        try:
            resp = self.session.get(url, timeout=timeout, **kwargs)
            # log non-2xx responses as failures for troubleshooting
            if not (200 <= getattr(resp, "status_code", 0) < 300):
                try:
                    snippet = (resp.text[:800] if hasattr(resp, "text") else "")
                except Exception:
                    snippet = ""
                entry = {
                    'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                    'method': 'GET',
                    'url': url,
                    'status_code': getattr(resp, 'status_code', None),
                    'response_snippet': snippet,
                }
                try:
                    self.failed_logger.record(entry)
                except Exception:
                    log.debug('could not record non-2xx response')
            return resp
        except Exception as exc:
            self._log_failure('GET', url, kwargs, exc)
            raise

    def request(self, method: str, url: str, timeout: int = 20, **kwargs) -> requests.Response:
        try:
            resp = self.session.request(method, url, timeout=timeout, **kwargs)
            if not (200 <= getattr(resp, "status_code", 0) < 300):
                try:
                    snippet = (resp.text[:800] if hasattr(resp, "text") else "")
                except Exception:
                    snippet = ""
                entry = {
                    'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                    'method': method,
                    'url': url,
                    'status_code': getattr(resp, 'status_code', None),
                    'response_snippet': snippet,
                }
                try:
                    self.failed_logger.record(entry)
                except Exception:
                    log.debug('could not record non-2xx response')
            return resp
        except Exception as exc:
            self._log_failure(method, url, kwargs, exc)
            raise

    def _log_failure(self, method: str, url: str, kwargs: dict[str, Any], exc: Exception) -> None:
        entry = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'method': method,
            'url': url,
            'kwargs': {k: (v if k not in ('headers','auth','cookies') else 'REDACTED') for k,v in kwargs.items()},
            'error': repr(exc),
        }
        try:
            self.failed_logger.record(entry)
        except Exception:
            log.debug('could not record failed request')


# convenience factory
_client_cache = {}

def client_for(settings):
    key = id(settings)
    if key not in _client_cache:
        _client_cache[key] = HTTPClient(settings)
    return _client_cache[key]
