"""Small, non-retrying client for the scoped product Memory API."""

from __future__ import annotations

import hashlib
import json
import math

from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class MemoryClientError(RuntimeError):
    """Safe error code: server bodies, endpoints and credentials are never echoed."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # An auth header must never travel to a redirect target.
        return None


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _invalid_constant(_):
    raise ValueError("invalid_json_constant")


class ProductMemoryClient:
    """Keep endpoint identity separate from private connection configuration.

    No retry occurs here, including POST. The durable bridge decides whether a
    request is new or must be reconciled after an uncertain network outcome.
    """

    max_response_bytes = 2 * 1024 * 1024

    def __init__(self, base_url: str, *, timeout: float = 20, headers: dict | None = None):
        try:
            parts = urlsplit(base_url)
            valid = (
                parts.scheme in {"http", "https"}
                and parts.hostname
                and not parts.username
                and not parts.password
                and not parts.query
                and not parts.fragment
                and not any(char.isspace() for char in base_url)
            )
            _ = parts.port  # Validate malformed ports before producing a request.
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("invalid_service_url")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid_timeout")
        self._base_url = urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
        )
        self.service_identity = hashlib.sha256(self._base_url.encode()).hexdigest()
        self._headers = dict(headers or {})
        self._timeout = timeout
        handlers = [_NoRedirect()]
        if parts.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
            # Local integration must not traverse a machine's configured HTTP proxy.
            handlers.append(ProxyHandler({}))
        self._opener = build_opener(*handlers)

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            body = (
                json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
                if payload is not None
                else None
            )
            headers = {
                **self._headers,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            request = Request(self._base_url + path, data=body, headers=headers, method=method)
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise MemoryClientError("response_too_large")
            envelope = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
        except MemoryClientError:
            raise
        except HTTPError as error:
            raise MemoryClientError(f"http_status_{error.code}") from None
        except (OSError, URLError, TimeoutError, HTTPException):
            raise MemoryClientError("transport_outcome_unknown") from None
        except (TypeError, ValueError, RecursionError):
            raise MemoryClientError("invalid_response") from None
        if not isinstance(envelope, dict) or type(envelope.get("code")) is not int:
            raise MemoryClientError("invalid_response")
        if envelope["code"] != 200 or "data" not in envelope:
            raise MemoryClientError("product_request_rejected")
        return envelope

    def add(self, payload: dict) -> dict:
        return self._request("POST", "/product/add", payload)

    def get(self, memory_id: str, cube_id: str) -> dict:
        query = urlencode({"mem_cube_id": cube_id, "include_sources": "true"})
        return self._request("GET", f"/product/get_memory/{quote(memory_id, safe='')}?{query}")

    def find(self, cube_id: str, user_id: str, filters: dict) -> dict:
        return self._request(
            "POST",
            "/product/get_memory",
            {
                "mem_cube_id": cube_id,
                "user_id": user_id,
                "include_sources": True,
                "include_memory_view": ["detail_factual"],
                "page": 1,
                "page_size": None,
                "filter": {"and": [{key: value} for key, value in filters.items()]},
            },
        )
