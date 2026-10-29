"""A small client for the dispatch API, used by the Streamlit console."""

from typing import Any, Protocol

import httpx


class ApiError(RuntimeError):
    pass


class Api(Protocol):
    def get(self, path: str, **params: Any) -> Any: ...
    def post(self, path: str, body: dict[str, Any] | None = None) -> Any: ...


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def _check(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ApiError(f"{response.status_code}: {detail}")
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self._check(
            self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        )

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._check(self.http.post(path, json=body or {}))
