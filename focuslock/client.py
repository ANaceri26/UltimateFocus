import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .api import API_PREFIX, PASSWORD_HEADER, load_or_create_token
from .errors import FocusLockError


class ApiClient:
    """Talks to a running FocusLock (serve / gui --api) over HTTP."""

    def __init__(self, base_url: str, token: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_home(cls, home: str | Path, host: str, port: int) -> "ApiClient":
        return cls(f"http://{host}:{port}", load_or_create_token(home))

    def request(self, method: str, path: str, body: Any = None, password: str | None = None) -> Any:
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if password:
            headers[PASSWORD_HEADER] = password
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{API_PREFIX}{path}", data=data, method=method, headers=headers
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise FocusLockError(f"API error {exc.code}: {_error_message(exc)}") from None
        except urllib.error.URLError as exc:
            raise FocusLockError(
                f"cannot reach FocusLock at {self.base_url}: {exc.reason}"
            ) from None
        return json.loads(raw) if raw else None

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any = None, password: str | None = None) -> Any:
        return self.request("POST", path, body if body is not None else {}, password)

    def delete(self, path: str, password: str | None = None) -> Any:
        return self.request("DELETE", path, password=password)


def _error_message(error: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(error.read()).get("error", error.reason))
    except (ValueError, AttributeError):
        return str(error.reason)
