"""Application Insights / Azure Monitor exporter (hot path).

Thin wrapper over the OpenTelemetry distro. Constructed lazily so the
``azure-monitor-opentelemetry`` dependency is **optional** (extras = ``[appinsights]``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from .base import Exporter

if TYPE_CHECKING:
    from ..run import ExternalCallSnapshot, MetricSnapshot, RunSnapshot

_LOGGER = logging.getLogger(__name__)


class AppInsightsExporter(Exporter):
    """Export observability events as OTel logs / metrics to Application Insights.

    Args:
        connection_string: Azure Application Insights connection string
            (from the ``alpenland-monitoring-infra`` Bicep deployment).
    """

    def __init__(self, connection_string: str) -> None:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "AppInsightsExporter requires the [appinsights] extra: "
                "pip install 'alpenland-observability[appinsights]'"
            ) from exc
        configure_azure_monitor(connection_string=connection_string)
        # OTel global meter / logger are now configured. We map our domain
        # events to OTel log records + metric counters in the methods below.
        from opentelemetry import metrics, _logs as otel_logs
        self._meter = metrics.get_meter("alpenland.observability")
        self._logger = otel_logs.get_logger("alpenland.observability")
        self._counters: dict[str, object] = {}

    def export_run_started(self, snapshot: "RunSnapshot") -> None:
        self._emit_event("alpenland.run.started", snapshot, extra={})

    def export_run_finished(self, snapshot: "RunSnapshot") -> None:
        self._emit_event(
            "alpenland.run.finished",
            snapshot,
            extra={
                "duration_ms": snapshot.duration_ms,
                "success": snapshot.success,
                "error_class": snapshot.error_class,
            },
        )

    def export_metrics(self, samples: Iterable["MetricSnapshot"]) -> None:
        from opentelemetry import metrics as _m
        for s in samples:
            counter = self._counters.get(s.metric_name)
            if counter is None:
                counter = self._meter.create_counter(
                    name=f"alpenland.{s.metric_name}", unit="1"
                )
                self._counters[s.metric_name] = counter
            counter.add(
                s.metric_value,
                attributes={
                    "tool_name": s.tool_name,
                    "run_id": s.run_id,
                    **(s.dims or {}),
                },
            )

    def export_external_calls(self, calls: Iterable["ExternalCallSnapshot"]) -> None:
        for c in calls:
            self._emit_event(
                "alpenland.external_call",
                run=None,
                extra={
                    "tool_name": c.tool_name,
                    "run_id": c.run_id,
                    "target": c.target,
                    "operation": c.operation,
                    "duration_ms": c.duration_ms,
                    "http_status": c.http_status,
                    "success": c.success,
                    "error_message": c.error_message,
                },
            )

    def shutdown(self) -> None:
        # azure-monitor-opentelemetry registers atexit handlers that flush
        # the BatchLogRecordProcessor. Nothing to do here.
        pass

    # ------------------------------------------------------------------
    def _emit_event(
        self,
        name: str,
        run: "RunSnapshot | None",
        *,
        extra: dict[str, object],
    ) -> None:
        from opentelemetry._logs import LogRecord
        from opentelemetry._logs.severity import SeverityNumber
        attributes: dict[str, object] = {"event.name": name}
        if run is not None:
            attributes.update(
                {
                    "run_id": run.run_id,
                    "tool_name": run.tool_name,
                    "tool_version": run.tool_version,
                    "app_env": run.app_env,
                    "host": run.host,
                }
            )
        attributes.update({k: v for k, v in extra.items() if v is not None})
        self._logger.emit(
            LogRecord(
                severity_number=SeverityNumber.INFO,
                body=name,
                attributes=attributes,
            )
        )
