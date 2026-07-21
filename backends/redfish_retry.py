"""Compatibility layer for python-redfish-library retry handling.

python-redfish-library 3.3.6 treats ``max_retry`` inconsistently: its request
loop performs ``max_retry + 1`` attempts, but its success check only accepts an
attempt number less than or equal to the client-level ``max_retry``. Passing
zero therefore raises ``RetriesExhaustedError`` even when the first request
succeeds. This module adapts one client instance without modifying global
library state.
"""

from __future__ import annotations

from types import MethodType


class RedfishTransportError(RuntimeError):
    """A Redfish request exhausted its configured attempts."""


def install_retry_compat(client, max_retries: int) -> None:
    """Give ``max_retries`` its documented extra-attempt semantics.

    The caller must construct ``client`` with its client-level ``max_retry``
    set to ``max_retries + 1`` and ``check_connectivity=False``. Each request
    is then made with a request-level limit of ``max_retries``, producing
    exactly one initial attempt plus the requested number of retries. The
    higher client-level value only compensates for the library's final
    off-by-one success check.
    """
    original_rest_request = client._rest_request
    original_session_request = client._session.request
    last_request_error: Exception | None = None

    def traced_session_request(*args, **kwargs):
        nonlocal last_request_error
        try:
            return original_session_request(*args, **kwargs)
        except Exception as error:
            last_request_error = error
            raise

    def fixed_rest_request(_client, *args, **kwargs):
        nonlocal last_request_error
        last_request_error = None
        kwargs["max_retry"] = max_retries
        response = original_rest_request(*args, **kwargs)
        if response is not None:
            return response

        path = kwargs.get("path", args[0] if args else "")
        method = str(kwargs.get("method", "GET")).upper()
        request_label = f"{method} {path}".strip()
        if last_request_error is None:
            raise RedfishTransportError(
                f"{request_label}: Redfish 请求失败，底层库未返回响应或异常。",
            )
        detail = str(last_request_error).strip()
        message = type(last_request_error).__name__
        if detail:
            message = f"{message}: {detail}"
        raise RedfishTransportError(
            f"{request_label}: {message}",
        ) from last_request_error

    client._session.request = traced_session_request
    client._rest_request = MethodType(fixed_rest_request, client)
