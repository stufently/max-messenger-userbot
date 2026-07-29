"""HTTP-клиент демона и коды выхода.

Коды выхода стабильны и задокументированы: `maxubctl` рассчитан на вызов из
скриптов и агентами, для которых разбор текста ошибки — плохой контракт.
"""

from __future__ import annotations

from typing import Any

import httpx

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_AUTH = 4
EXIT_CONFLICT = 5
EXIT_UNSUPPORTED = 6
EXIT_NOT_FOUND = 7

_STATUS_TO_EXIT = {
    400: EXIT_ERROR,
    401: EXIT_AUTH,
    404: EXIT_NOT_FOUND,
    409: EXIT_CONFLICT,
    422: EXIT_USAGE,
    501: EXIT_UNSUPPORTED,
}


class ApiError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class ApiClient:
    def __init__(self, base_url: str, token: str | None, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base_url}{path}"
        try:
            response = httpx.request(
                method, url, headers=self._headers(), timeout=self._timeout, **kwargs
            )
        except httpx.ConnectError as exc:
            raise ApiError(
                f"демон недоступен по адресу {self._base_url}: {exc}", EXIT_UNREACHABLE
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(f"ошибка запроса: {exc}", EXIT_ERROR) from exc

        if response.status_code >= 400:
            raise ApiError(
                self._describe(response), _STATUS_TO_EXIT.get(response.status_code, EXIT_ERROR)
            )
        if not response.content:
            return None
        return response.json()

    @staticmethod
    def _describe(response: httpx.Response) -> str:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        return f"HTTP {response.status_code}: {detail or response.text.strip() or 'без деталей'}"

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)
