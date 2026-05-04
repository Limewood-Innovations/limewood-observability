"""Exporter protocol + a no-op default implementation.

An exporter is the *transport* tier — it takes finished events and pushes
them to a sink (SQL DB, AppInsights, …). Exporters are intentionally
**fail-soft**: they log on error and swallow. Telemetry must never break
business workflows.

The library can run with N exporters; each event is fanned out to all of
them in order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..run import ExternalCallSnapshot, MetricSnapshot, RunSnapshot


@runtime_checkable
class Exporter(Protocol):
    """The contract every exporter must implement."""

    def export_run_started(self, snapshot: "RunSnapshot") -> None: ...

    def export_run_finished(self, snapshot: "RunSnapshot") -> None: ...

    def export_metrics(self, samples: Iterable["MetricSnapshot"]) -> None: ...

    def export_external_calls(self, calls: Iterable["ExternalCallSnapshot"]) -> None: ...

    def shutdown(self) -> None: ...


class NoopExporter:
    """Default exporter — discards every event.

    Used in tests and when no env vars are set. Means a tool that's wired
    for observability still works on a developer laptop without any infra.
    """

    def export_run_started(self, snapshot: "RunSnapshot") -> None:
        pass

    def export_run_finished(self, snapshot: "RunSnapshot") -> None:
        pass

    def export_metrics(self, samples: Iterable["MetricSnapshot"]) -> None:
        pass

    def export_external_calls(self, calls: Iterable["ExternalCallSnapshot"]) -> None:
        pass

    def shutdown(self) -> None:
        pass
