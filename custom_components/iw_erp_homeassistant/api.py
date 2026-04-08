"""Shared HTTP client helpers and error classification for iw_erp_homeassistant.

Every API failure in this integration should flow through :func:`log_api_error`
so that a single structured log line is emitted containing every useful
diagnostic field (URL, HTTP status, exception type, exception message and a
truncated response body). :func:`api_get_json` is the preferred entry point for
authenticated GET requests that return JSON - it reads the server's own error
body *before* raising, so the line in the log contains the real reason.

The :data:`ERR_*` constants double as config-flow error keys (``errors["base"]``)
and map 1:1 to translation strings in ``strings.json`` / ``translations/*.json``.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

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

# Truncate response bodies so one-line log entries stay readable and
# HTML error pages or binary payloads cannot flood the log.
_BODY_SNIPPET_LEN = 500


def _truncate(text: str | None, limit: int = _BODY_SNIPPET_LEN) -> str:
    """Return ``text`` trimmed to ``limit`` chars, single-lined, never ``None``."""
    if not text:
        return ""
    text = text.strip().replace("\n", " ").replace("\r", " ")
    if len(text) > limit:
        return text[:limit] + f"... [+{len(text) - limit} bytes]"
    return text


async def read_body_snippet(response: aiohttp.ClientResponse) -> str:
    """Read up to ~1 KB of the response body safely.

    Called BEFORE :meth:`aiohttp.ClientResponse.raise_for_status` so the
    server's own diagnostic message ends up in our log line. Decoding uses
    ``errors='replace'`` so binary payloads do not blow up.
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
        # Raised by json.loads / response.json() on malformed payloads.
        return ERR_INVALID_RESPONSE
    return ERR_UNKNOWN


def log_api_error(
    logger: logging.Logger,
    operation: str,
    url: str,
    exc: BaseException,
    status: int | None = None,
    body_snippet: str | None = None,
    level: int = logging.ERROR,
) -> str:
    """Log a single structured line and return the matching error key.

    Format::

        <operation> failed: key=<k> url=<u> status=<s> type=<T> msg=<m> body=<b>
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

    logger.log(
        level,
        "%s failed: key=%s url=%s status=%s type=%s msg=%s body=%s",
        operation,
        key,
        url,
        status if status is not None else "-",
        type(exc).__name__,
        str(exc) or "-",
        _truncate(body_snippet) or "-",
    )
    return key


async def api_get_json(
    session: aiohttp.ClientSession,
    url: str,
    token: str,
    logger: logging.Logger,
    operation: str,
    timeout: int = 10,
) -> tuple[Any, str | None]:
    """Authenticated GET that returns parsed JSON.

    Returns ``(data, None)`` on success and ``(None, error_key)`` on failure.
    On failure exactly one structured error line is emitted via
    :func:`log_api_error`, so callers do not need their own ``try``/``except``.
    """
    headers = {"x-iw-jwt-token": token}
    try:
        async with session.get(url, headers=headers, timeout=timeout) as response:
            status = response.status
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
                key = log_api_error(
                    logger, operation, url, exc, status=status, body_snippet=body
                )
                return None, key
            try:
                data = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as json_err:
                body = await read_body_snippet(response)
                key = log_api_error(
                    logger,
                    operation,
                    url,
                    json_err,
                    status=status,
                    body_snippet=body,
                )
                return None, key
            return data, None
    except Exception as exc:  # noqa: BLE001 - intentional catch-all, classified below
        key = log_api_error(logger, operation, url, exc)
        return None, key
