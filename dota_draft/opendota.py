"""Shared OpenDota API client: request pacing, retries, stderr logging."""

import logging
import sys
import threading
import time
from typing import Any

import httpx

BASE_URL = "https://api.opendota.com/api"

# Free tier: 60 requests per minute. 1.1 s between requests keeps us under it.
MIN_INTERVAL_S = 1.1
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

log = logging.getLogger(__name__)


def setup_logging(level: int = logging.INFO) -> None:
    """Send all logging to stderr. Stdout carries the MCP protocol."""
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


class OpenDotaError(Exception):
    """Raised when OpenDota cannot be reached or returns an error."""


class QueryTimeout(OpenDotaError):
    """Raised when an explorer query hits the server's time limit. Retrying the same query won't help."""


class OpenDotaClient:
    def __init__(self, base_url: str = BASE_URL, timeout_s: float = 30.0) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s)
        self._lock = threading.Lock()
        self._last_request = 0.0

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a path under the API and return the parsed JSON."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_turn()
            try:
                response = self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                error = f"{path}: {exc.__class__.__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return response.json()
                error = f"{path}: HTTP {response.status_code}: {response.text[:200]}"
                if "timeout" in response.text.lower():
                    raise QueryTimeout(error)
                if response.status_code not in RETRY_STATUSES:
                    raise OpenDotaError(error)
            log.warning("Attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, error)
            if attempt < MAX_ATTEMPTS:
                time.sleep(2**attempt)
        raise OpenDotaError(error)

    def explorer(self, sql: str) -> list[dict[str, Any]]:
        """Run SQL on the OpenDota explorer and return its rows."""
        result = self.get("/explorer", params={"sql": sql})
        if result.get("err"):
            error = f"explorer: {result['err']}"
            raise QueryTimeout(error) if "timeout" in str(result["err"]).lower() else OpenDotaError(error)
        return result["rows"]

    def _wait_turn(self) -> None:
        with self._lock:
            wait = self._last_request + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
