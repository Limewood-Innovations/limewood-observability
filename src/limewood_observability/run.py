"""The :class:`Run` context manager + supporting snapshot dataclasses.

A :class:`Run` represents one tool invocation:

* enters → exporters get ``export_run_started`` (the row in
  ``observability.runs`` is inserted with ``success=NULL``)
* in-flight → caller emits metrics and tracks external calls
* exits → exporters get ``export_run_finished`` (the same row is updated
  with ``success`` / ``duration_ms`` / ``metrics_json``)

Snapshots are passed to exporters by value (frozen dataclasses) so an
exporter can't accidentally mutate the run state.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Iterator, Mapping, Sequence

from .exporters.base import Exporter

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot dataclasses (passed to exporters)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    tool_name: str
    tool_version: str | None
    app_env: str
    host: str | None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    success: bool | None = None
    error_class: str | None = None
    error_message: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    parent_run_id: str | None = None


@dataclass(frozen=True)
class MetricSnapshot:
    run_id: str
    tool_name: str
    metric_name: str
    metric_value: float
    sampled_at: datetime
    dims: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExternalCallSnapshot:
    run_id: str
    tool_name: str
    target: str
    operation: str
    started_at: datetime
    duration_ms: int
    success: bool
    http_status: int | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Run + tracker
# ---------------------------------------------------------------------------


class Run:
    """Handle for one tool invocation. Construct via :meth:`Observability.run`.

    Acts as both a sync and async context manager. Inside the ``with`` block
    you can:

    * call :meth:`record_metric` to emit a counter
    * call :meth:`set_metrics` to bulk-set the run's final metrics dict
    * use :meth:`track_external_call` to record an outbound HTTP/DB call
    * read :attr:`logger` for a stdlib logger that auto-injects ``run_id``
    """

    def __init__(
        self,
        *,
        run_id: str,
        tool_name: str,
        tool_version: str | None,
        app_env: str,
        host: str | None,
        exporters: Sequence[Exporter],
        parent_run_id: str | None,
        logger: logging.Logger,
    ) -> None:
        self.run_id = run_id
        self.tool_name = tool_name
        self.tool_version = tool_version
        self.app_env = app_env
        self.host = host
        self.parent_run_id = parent_run_id
        self.logger = logging.LoggerAdapter(
            logger,
            extra={
                "run_id": run_id,
                "tool_name": tool_name,
                "app_env": app_env,
            },
        )
        self._exporters = list(exporters)
        self._started_at: datetime | None = None
        self._started_perf: float | None = None
        self._metrics: dict[str, Any] = {}
        self._metric_buffer: list[MetricSnapshot] = []
        self._call_buffer: list[ExternalCallSnapshot] = []
        self._failed: bool = False
        self._error_class: str | None = None
        self._error_message: str | None = None

    # ------------------------------------------------------------------
    # public api
    # ------------------------------------------------------------------

    def record_metric(
        self,
        name: str,
        value: float,
        *,
        dims: Mapping[str, Any] | None = None,
    ) -> None:
        """Emit a single metric counter sample. Buffered; flushed on run exit."""
        self._metric_buffer.append(
            MetricSnapshot(
                run_id=self.run_id,
                tool_name=self.tool_name,
                metric_name=name,
                metric_value=float(value),
                sampled_at=datetime.now(tz=timezone.utc),
                dims=dict(dims) if dims else None,
            )
        )

    def set_metrics(self, metrics: Mapping[str, Any]) -> None:
        """Set the final summary metrics dict (written into ``runs.metrics_json``)."""
        self._metrics.update(metrics)

    def fail(self, *, error_class: str, error_message: str) -> None:
        """Mark this run as failed even before context exit."""
        self._failed = True
        self._error_class = error_class
        self._error_message = error_message

    @contextlib.contextmanager
    def track_external_call(
        self,
        *,
        target: str,
        operation: str,
    ) -> Iterator["ExternalCallTracker"]:
        """Sync context: time an outbound call. Use the async version for asyncio."""
        tracker = ExternalCallTracker(target=target, operation=operation)
        tracker._start()
        try:
            yield tracker
            if tracker._success is None:
                tracker._success = True
        except Exception as exc:
            tracker._success = False
            tracker._error_message = str(exc)
            raise
        finally:
            self._call_buffer.append(tracker._snapshot(self.run_id, self.tool_name))

    @contextlib.asynccontextmanager
    async def track_external_call_async(
        self,
        *,
        target: str,
        operation: str,
    ):
        """Async variant of :meth:`track_external_call`."""
        tracker = ExternalCallTracker(target=target, operation=operation)
        tracker._start()
        try:
            yield tracker
            if tracker._success is None:
                tracker._success = True
        except Exception as exc:
            tracker._success = False
            tracker._error_message = str(exc)
            raise
        finally:
            self._call_buffer.append(tracker._snapshot(self.run_id, self.tool_name))

    # ------------------------------------------------------------------
    # context manager protocol (sync + async)
    # ------------------------------------------------------------------

    def __enter__(self) -> "Run":
        self._begin()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None and not self._failed:
            self._failed = True
            self._error_class = exc_type.__name__ if exc_type else "Exception"
            self._error_message = str(exc)
        self._finish()
        # Don't swallow the exception
        return None

    async def __aenter__(self) -> "Run":
        self._begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None and not self._failed:
            self._failed = True
            self._error_class = exc_type.__name__ if exc_type else "Exception"
            self._error_message = str(exc)
        self._finish()
        return None

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _begin(self) -> None:
        self._started_at = datetime.now(tz=timezone.utc)
        self._started_perf = time.perf_counter()
        snapshot = self._snapshot()
        for ex in self._exporters:
            ex.export_run_started(snapshot)

    def _finish(self) -> None:
        finished = datetime.now(tz=timezone.utc)
        duration_ms = int((time.perf_counter() - (self._started_perf or 0)) * 1000)
        snapshot = self._snapshot(
            finished_at=finished,
            duration_ms=duration_ms,
            success=not self._failed,
        )
        for ex in self._exporters:
            ex.export_run_finished(snapshot)
        if self._metric_buffer:
            for ex in self._exporters:
                ex.export_metrics(self._metric_buffer)
        if self._call_buffer:
            for ex in self._exporters:
                ex.export_external_calls(self._call_buffer)

    def _snapshot(
        self,
        *,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        success: bool | None = None,
    ) -> RunSnapshot:
        return RunSnapshot(
            run_id=self.run_id,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            app_env=self.app_env,
            host=self.host,
            started_at=self._started_at or datetime.now(tz=timezone.utc),
            finished_at=finished_at,
            duration_ms=duration_ms,
            success=success,
            error_class=self._error_class,
            error_message=self._error_message,
            metrics=self._metrics,
            parent_run_id=self.parent_run_id,
        )


class ExternalCallTracker:
    """Returned by :meth:`Run.track_external_call`. Caller may set status/error."""

    def __init__(self, *, target: str, operation: str) -> None:
        self.target = target
        self.operation = operation
        self._http_status: int | None = None
        self._success: bool | None = None
        self._error_message: str | None = None
        self._started_at: datetime | None = None
        self._started_perf: float | None = None

    def set_status(self, http_status: int) -> None:
        """Caller-provided HTTP status. Sets ``success`` automatically based on 2xx/3xx."""
        self._http_status = http_status
        if self._success is None:
            self._success = http_status < 400

    def set_failed(self, error_message: str) -> None:
        self._success = False
        self._error_message = error_message

    def _start(self) -> None:
        self._started_at = datetime.now(tz=timezone.utc)
        self._started_perf = time.perf_counter()

    def _snapshot(self, run_id: str, tool_name: str) -> ExternalCallSnapshot:
        duration_ms = int((time.perf_counter() - (self._started_perf or 0)) * 1000)
        return ExternalCallSnapshot(
            run_id=run_id,
            tool_name=tool_name,
            target=self.target,
            operation=self.operation,
            started_at=self._started_at or datetime.now(tz=timezone.utc),
            duration_ms=duration_ms,
            success=bool(self._success),
            http_status=self._http_status,
            error_message=self._error_message,
        )


def _new_uuid_run_id() -> str:
    """Fallback ID generator. Production uses :func:`limewood_observability._ulid.new_ulid`."""
    return uuid.uuid4().hex[:26].upper()
