from __future__ import annotations

import threading
from typing import Any

from connector.logging import logger
from connector.runtimes.codex.sdk.native_events import NativeRequestResolved


class DeferredServerRequestReader:
    """Keep Codex responses flowing while a server request waits for user input."""

    def __init__(
        self, sync_client: Any, *, raw_tap=None, server_request=None, on_close=None
    ) -> None:
        self._client = sync_client
        self._raw_tap = raw_tap
        self._server_request = server_request
        self._on_close = on_close

    def run(self) -> None:
        try:
            while True:
                message = self._client._read_message()
                if "method" in message and self._raw_tap is not None:
                    self._raw_tap(message)
                if "method" in message and "id" in message:
                    threading.Thread(
                        target=self._respond_to_server_request,
                        args=(message,),
                        daemon=True,
                    ).start()
                    continue
                if "method" in message:
                    method = message["method"]
                    if isinstance(method, str):
                        self._client._router.route_notification(
                            self._client._coerce_notification(
                                method,
                                message.get("params"),
                            )
                        )
                    continue
                self._client._router.route_response(message)
        except BaseException as exc:  # noqa: BLE001 - fail all transport waiters
            self._client._router.fail_all(exc)
            if self._on_close is not None:
                self._on_close(exc)

    def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        method = message.get("method")
        try:
            result = (self._server_request or self._client._handle_server_request)(
                message
            )
            self._client._write_message({"id": request_id, "result": result})
        except NativeRequestResolved:
            return
        except BaseException as exc:  # noqa: BLE001 - fail all transport waiters
            logger.exception(
                "codex sdk server request failed method={} request_id={}",
                method,
                request_id,
            )
            try:
                self._client._write_message(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32603,
                            "message": str(exc) or exc.__class__.__name__,
                        },
                    }
                )
            except BaseException:  # noqa: BLE001 - best-effort error response
                logger.debug(
                    "codex sdk server request error response was not delivered "
                    "method={} request_id={}",
                    method,
                    request_id,
                )


def install_deferred_server_request_reader(client: Any, **callbacks) -> bool:
    nested_client = getattr(client, "_client", None)
    sync_client = getattr(nested_client, "_sync", None)
    if sync_client is None:
        return False
    required = (
        "_read_message",
        "_handle_server_request",
        "_write_message",
        "_router",
        "_coerce_notification",
    )
    if any(not hasattr(sync_client, name) for name in required):
        return False
    sync_client._reader_loop = DeferredServerRequestReader(sync_client, **callbacks).run
    return True
