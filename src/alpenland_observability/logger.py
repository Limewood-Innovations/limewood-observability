"""JSON log formatter with automatic ``run_id`` / ``tool_name`` injection."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Render each log record as a single JSON line.

    Standard fields are always present; anything passed via ``extra={...}``
    is merged into the same object. ``run_id`` / ``tool_name`` /
    ``app_env`` are *expected* to be set by the :class:`Run` context, but
    nothing breaks if they're missing.
    """

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "asctime", "taskName",
    }

    def __init__(self, *, static_fields: dict[str, str] | None = None) -> None:
        super().__init__()
        self._static = dict(static_fields or {})

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(self._static)

        # Merge user-supplied extra fields
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            if k in payload:
                continue
            payload[k] = v

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def attach_json_handler(
    logger: logging.Logger,
    *,
    static_fields: dict[str, str] | None = None,
    level: int = logging.INFO,
    stream=None,
) -> logging.Handler:
    """Attach a single :class:`logging.StreamHandler` with :class:`JsonFormatter`.

    Returns the handler so the caller can detach it later (test cleanup).
    """
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter(static_fields=static_fields))
    logger.addHandler(handler)
    return handler
