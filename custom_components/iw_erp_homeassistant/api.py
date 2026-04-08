"""Shared HTTP client helpers and error classification for iw_erp_homeassistant.

Every API failure in this integration should flow through :func:`log_api_error`
so that a single structured log line is emitted containing every useful
diagnostic field (URL, HTTP status, ERP-specific error headers, exception type,
exception message and a truncated response body). :func:`api_get_json` is the
preferred entry point for authenticated GET requests that return JSON - it
reads the server's own error body *before* raising, so the line in the log
contains the real reason.

The :data:`ERR_*` constants double as config-flow error keys (``errors["base"]``)
and map 1:1 to translation strings in ``strings.json`` / ``translations/*.json``.

The ERP server attaches the headers ``X-IW-ERROR-CODE`` and ``X-IW-ERROR-JSON``
to some error responses. These are captured into :class:`ApiError` so callers
can surface them in the UI (via ``description_placeholders``) without digging
through logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

# Error keys surfaced to the config-flow UI and strings.json.
ERR_CANNOT_CONNECT = "cannot_connect"
ERR_TIMEOUT = "timeout"
ERR_INVALID_AUTH = "invalid_auth"
ERR_INVALID_HOST = "invalid_host"
ERR_SSL = "ssl_error"
ERR_SERVER = "server_error"
ERR_NOT_FOUND = "not_found"
ERR_INVALID_RESPONSE = "invalid_response"
ERR_UNKNOWN = "unknown"

# Auth-class errors that should trigger reauth rather than a transient retry.
AUTH_ERROR_KEYS = frozenset({ERR_INVALID_AUTH})

# ERP-specific response headers that carry structured error information.
ERP_ERROR_CODE_HEADER = "X-IW-ERROR-CODE"
ERP_ERROR_JSON_HEADER = "X-IW-ERROR-JSON"

# Truncate response bodies so one-line log entries stay readable and
# HTML error pages or binary payloads cannot flood the log.
_BODY_SNIPPET_LEN = 500
# Keep the ERP JSON detail field shorter again - it typically fits.
_ERP_DETAIL_LEN = 300


@dataclass
class ApiError:
    """Structured result returned from :func:`api_get_json` on failure.

    ``key`` is the translation key surfaced to the config flow UI. ``erp_code``
    and ``erp_detail`` come straight from the ``X-IW-ERROR-CODE`` /
    ``X-IW-ERROR-JSON`` response headers when the ERP server sets them.
    """

    key: str
    status: int | None = None
    erp_code: str | None = None
    erp_detail: str | None = None
    exception_type: str | None = None

    def placeholders(self) -> dict[str, str]:
        """Return a ``description_placeholders`` dict for ``async_show_form``.

        ``last_error`` is the human-readable blob appended to the form
        description; it is empty when no ERP-specific detail is available so
        the description stays clean on the happy path.
        """
        parts: list[str] = []
        if self.erp_code:
            parts.append(self.erp_code)
        if self.erp_detail:
            parts.append(self.erp_detail)
        detail = " — ".join(parts) if parts else ""
        last_error = f"\n\nLast attempt: {detail}" if detail else ""
        return {
            "last_error": last_error,
            "error_code": self.erp_code or "",
            "error_detail": self.erp_detail or "",
        }


def sanitize_url(url: str) -> str:
    """Return ``url`` with any embedded userinfo stripped.

    A user might type ``https://user:password@host`` into the config flow; we
    must never let that reach the logs. Parse failures fall back to ``"***"``
    rather than risking the original value.
    """
    if not url:
        return url
    try:
        parts = urlparse(url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            parts = parts._replace(netloc=host)
            return urlunparse(parts)
        return url
    except Exception:  # pragma: no cover - defensive
        return "***"


def _truncate(text: str | None, limit: int = _BODY_SNIPPET_LEN) -> str:
    """Return ``text`` trimmed to ``limit`` chars, single-lined, never ``None``."""
    if not text:
        return ""
    text = text.strip().replace("\n", " ").replace("\r", " ")
    if len(text) > limit:
        return text[:limit] + f"... [+{len(text) - limit} bytes]"
    return text


def extract_erp_error_headers(
    response: aiohttp.ClientResponse,
) -> tuple[str | None, str | None]:
    """Return ``(erp_code, erp_detail)`` from the ERP's custom error headers.

    ``erp_detail`` is the raw value of ``X-IW-ERROR-JSON`` - we do not attempt
    to pretty-print it because the header is already a single line and the
    caller may want the original string.
    """
    try:
        code = response.headers.get(ERP_ERROR_CODE_HEADER)
        detail = response.headers.get(ERP_ERROR_JSON_HEADER)
    except Exception:  # pragma: no cover - defensive
        return None, None
    return (code or None), (detail or None)


async def read_body_snippet(response: aiohttp.ClientResponse) -> str:
    """Read up to ~1 KB of the response body safely.

    Decoding uses ``errors='replace'`` so binary payloads do not blow up.
    Prefer :func:`read_body_text` when you need the full text for both
    JSON parsing and logging - this helper consumes the stream.
    """
    try:
        raw = await response.content.read(_BODY_SNIPPET_LEN * 2)
    except Exception:  # pragma: no cover - defensive
        return ""
    try:
        return _truncate(raw.decode("utf-8", errors="replace"))
    except Exception:  # pragma: no cover - defensive
        return _truncate(repr(raw))


def classify_http_status(status: int) -> str:
    """Map an HTTP status code to an error key."""
    if status in (401, 403):
        return ERR_INVALID_AUTH
    if status == 404:
        return ERR_NOT_FOUND
    if 500 <= status < 600:
        return ERR_SERVER
    return ERR_INVALID_RESPONSE


def classify_exception(exc: BaseException) -> str:
    """Map an exception to an error key.

    Order matters: most specific subclass first. Unknown exceptions fall
    through to :data:`ERR_UNKNOWN` so the caller still logs the type name.
    """
    if isinstance(exc, aiohttp.ClientResponseError):
        return classify_http_status(exc.status)
    if isinstance(exc, (asyncio.TimeoutError, aiohttp.ServerTimeoutError)):
        return ERR_TIMEOUT
    if isinstance(exc, aiohttp.ClientConnectorCertificateError):
        return ERR_SSL
    if isinstance(exc, aiohttp.ClientSSLError) or isinstance(exc, ssl.SSLError):
        return ERR_SSL
    if isinstance(exc, aiohttp.ClientConnectorError):
        # Covers DNS failure, connection refused and network unreachable.
        # aiohttp exposes the underlying OSError via ``os_error``; a gaierror
        # means DNS resolution failed, which points at an invalid hostname.
        os_err = getattr(exc, "os_error", None)
        if os_err is not None and os_err.__class__.__name__ == "gaierror":
            return ERR_INVALID_HOST
        return ERR_CANNOT_CONNECT
    if isinstance(exc, aiohttp.InvalidURL):
        return ERR_INVALID_HOST
    if isinstance(exc, aiohttp.ClientPayloadError):
        return ERR_INVALID_RESPONSE
    if isinstance(exc, aiohttp.ClientError):
        return ERR_CANNOT_CONNECT
    if isinstance(exc, ValueError):
        # Raised by json.loads on malformed payloads.
        return ERR_INVALID_RESPONSE
    return ERR_UNKNOWN


def log_api_error(
    logger: logging.Logger,
    operation: str,
    url: str,
    exc: BaseException,
    status: int | None = None,
    body_snippet: str | None = None,
    erp_code: str | None = None,
    erp_detail: str | None = None,
    level: int = logging.ERROR,
) -> ApiError:
    """Log a single structured line and return the :class:`ApiError` result.

    Format::

        <operation> failed: key=<k> url=<u> status=<s> type=<T> msg=<m>
            erp_code=<c> erp_detail=<d> body=<b>
    """
    key = classify_exception(exc)

    # If the caller already has an HTTP status (e.g. from ``response.status``
    # before we raised) prefer a status-based classification over whatever the
    # raw exception would suggest.
    if status is not None and isinstance(exc, aiohttp.ClientResponseError):
        key = classify_http_status(status)
    elif status is not None and key in (
        ERR_UNKNOWN,
        ERR_INVALID_RESPONSE,
        ERR_CANNOT_CONNECT,
    ):
        key = classify_http_status(status)

    safe_url = sanitize_url(url)
    safe_code = _truncate(erp_code, 64) or "-"
    safe_detail = _truncate(erp_detail, _ERP_DETAIL_LEN) or "-"

    logger.log(
        level,
        "%s failed: key=%s url=%s status=%s type=%s msg=%s "
        "erp_code=%s erp_detail=%s body=%s",
        operation,
        key,
        safe_url,
        status if status is not None else "-",
        type(exc).__name__,
        str(exc) or "-",
        safe_code,
        safe_detail,
        _truncate(body_snippet) or "-",
    )
    return ApiError(
        key=key,
        status=status,
        erp_code=erp_code or None,
        erp_detail=erp_detail or None,
        exception_type=type(exc).__name__,
    )


async def api_get_json(
    session: aiohttp.ClientSession,
    url: str,
    token: str,
    logger: logging.Logger,
    operation: str,
    timeout: int = 10,
) -> tuple[Any, ApiError | None]:
    """Authenticated GET that returns parsed JSON.

    Returns ``(data, None)`` on success and ``(None, ApiError)`` on failure.
    On failure exactly one structured error line is emitted via
    :func:`log_api_error`, so callers do not need their own ``try``/``except``.

    Reads the response body with ``response.text()`` once and then parses it
    with ``json.loads``. This guarantees that a JSON decode error still has
    the original body available for logging - calling ``response.json()``
    followed by a second read on failure would return an empty string because
    the stream has already been consumed.
    """
    headers = {"x-iw-jwt-token": token}
    try:
        async with session.get(url, headers=headers, timeout=timeout) as response:
            status = response.status
            erp_code, erp_detail = extract_erp_error_headers(response)

            if status >= 400:
                body = await read_body_snippet(response)
                # Build a synthetic ClientResponseError so the classifier takes
                # the same path as a real raise_for_status() failure.
                exc = aiohttp.ClientResponseError(
                    request_info=response.request_info,
                    history=response.history,
                    status=status,
                    message=response.reason or "",
                    headers=response.headers,
                )
                error = log_api_error(
                    logger,
                    operation,
                    url,
                    exc,
                    status=status,
                    body_snippet=body,
                    erp_code=erp_code,
                    erp_detail=erp_detail,
                )
                return None, error

            # Read the body as text first so we keep it for logging even if
            # JSON decoding fails. ``response.json()`` would consume the
            # stream and leave us with nothing to log.
            try:
                text = await response.text()
            except Exception as read_err:  # noqa: BLE001
                error = log_api_error(
                    logger,
                    operation,
                    url,
                    read_err,
                    status=status,
                    erp_code=erp_code,
                    erp_detail=erp_detail,
                )
                return None, error

            try:
                data = json.loads(text)
            except ValueError as json_err:
                error = log_api_error(
                    logger,
                    operation,
                    url,
                    json_err,
                    status=status,
                    body_snippet=text,
                    erp_code=erp_code,
                    erp_detail=erp_detail,
                )
                return None, error
            return data, None
    except Exception as exc:  # noqa: BLE001 - intentional catch-all, classified below
        error = log_api_error(logger, operation, url, exc)
        return None, error
