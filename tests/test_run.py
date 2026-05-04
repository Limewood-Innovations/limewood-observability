"""Tests for Run lifecycle, metric buffering, external-call tracking."""

from __future__ import annotations

import asyncio
from typing import Iterable

import pytest

from alpenland_observability import (
    ExternalCallSnapshot,
    MetricSnapshot,
    Observability,
    RunSnapshot,
)
from alpenland_observability.exporters.base import Exporter


class RecordingExporter(Exporter):
    """Capture every event for assertion."""

    def __init__(self) -> None:
        self.run_started: list[RunSnapshot] = []
        self.run_finished: list[RunSnapshot] = []
        self.metrics: list[MetricSnapshot] = []
        self.calls: list[ExternalCallSnapshot] = []
        self.shutdown_calls: int = 0

    def export_run_started(self, snap: RunSnapshot) -> None:
        self.run_started.append(snap)

    def export_run_finished(self, snap: RunSnapshot) -> None:
        self.run_finished.append(snap)

    def export_metrics(self, samples: Iterable[MetricSnapshot]) -> None:
        self.metrics.extend(samples)

    def export_external_calls(self, calls: Iterable[ExternalCallSnapshot]) -> None:
        self.calls.extend(calls)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.fixture
def rec() -> RecordingExporter:
    return RecordingExporter()


@pytest.fixture
def obs(rec: RecordingExporter) -> Observability:
    return Observability(
        tool_name="test_tool",
        app_env="test",
        exporters=[rec],
        attach_json_logger=False,
    )


def test_observability_requires_tool_name() -> None:
    with pytest.raises(ValueError):
        Observability(tool_name="", attach_json_logger=False)


def test_sync_run_writes_started_then_finished(
    obs: Observability, rec: RecordingExporter
) -> None:
    with obs.run() as run:
        assert run.run_id == rec.run_started[0].run_id
    assert len(rec.run_started) == 1
    assert len(rec.run_finished) == 1
    assert rec.run_finished[0].success is True
    assert rec.run_finished[0].duration_ms is not None
    assert rec.run_finished[0].duration_ms >= 0


def test_run_id_is_ulid_shape(obs: Observability) -> None:
    with obs.run() as run:
        assert len(run.run_id) == 26
        assert run.run_id.isalnum()


def test_exception_marks_failure(obs: Observability, rec: RecordingExporter) -> None:
    with pytest.raises(RuntimeError):
        with obs.run():
            raise RuntimeError("boom")
    assert rec.run_finished[0].success is False
    assert rec.run_finished[0].error_class == "RuntimeError"
    assert rec.run_finished[0].error_message == "boom"


def test_explicit_fail_overrides_success(obs: Observability, rec: RecordingExporter) -> None:
    with obs.run() as run:
        run.fail(error_class="DomainError", error_message="rejected")
    assert rec.run_finished[0].success is False
    assert rec.run_finished[0].error_class == "DomainError"


def test_record_metric_buffers_until_finish(
    obs: Observability, rec: RecordingExporter
) -> None:
    with obs.run() as run:
        run.record_metric("docs_added", 5, dims={"pipeline": "rebe"})
        run.record_metric("docs_added", 12, dims={"pipeline": "cn"})
        # Should not be flushed until __exit__:
        assert rec.metrics == []
    assert len(rec.metrics) == 2
    assert {m.dims["pipeline"] for m in rec.metrics} == {"rebe", "cn"}


def test_set_metrics_lands_in_run_finished(
    obs: Observability, rec: RecordingExporter
) -> None:
    with obs.run() as run:
        run.set_metrics({"docs_added": 17, "duration_seconds": 4.2})
    assert rec.run_finished[0].metrics == {"docs_added": 17, "duration_seconds": 4.2}


def test_track_external_call_success(
    obs: Observability, rec: RecordingExporter
) -> None:
    with obs.run() as run:
        with run.track_external_call(target="enaio", operation="documents/search") as call:
            call.set_status(200)
    assert len(rec.calls) == 1
    c = rec.calls[0]
    assert c.target == "enaio"
    assert c.operation == "documents/search"
    assert c.success is True
    assert c.http_status == 200
    assert c.duration_ms >= 0


def test_track_external_call_5xx_marked_failure(
    obs: Observability, rec: RecordingExporter
) -> None:
    with obs.run() as run:
        with run.track_external_call(target="enaio", operation="x") as call:
            call.set_status(500)
    assert rec.calls[0].success is False
    assert rec.calls[0].http_status == 500


def test_track_external_call_exception_marked_failure(
    obs: Observability, rec: RecordingExporter
) -> None:
    with pytest.raises(ValueError):
        with obs.run() as run:
            with run.track_external_call(target="enaio", operation="x"):
                raise ValueError("network down")
    assert rec.calls[0].success is False
    assert "network down" in rec.calls[0].error_message


def test_async_run() -> None:
    rec = RecordingExporter()
    obs = Observability(
        tool_name="async_tool", app_env="test", exporters=[rec], attach_json_logger=False
    )

    async def go():
        async with obs.run() as run:
            async with run.track_external_call_async(target="sap", operation="ih.create") as call:
                call.set_status(201)
            run.record_metric("requests_handled", 1)

    asyncio.run(go())
    assert len(rec.run_finished) == 1
    assert rec.run_finished[0].success is True
    assert len(rec.calls) == 1
    assert len(rec.metrics) == 1


def test_logger_adapter_injects_run_id(obs: Observability, caplog) -> None:
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="test_tool")
    with obs.run() as run:
        run.logger.info("hello")
    matching = [r for r in caplog.records if r.message == "hello"]
    assert matching, f"no 'hello' record captured (got {[r.message for r in caplog.records]})"
    record = matching[-1]
    assert getattr(record, "run_id") == run.run_id
    assert getattr(record, "tool_name") == "test_tool"
    assert getattr(record, "app_env") == "test"


def test_shutdown_propagates_to_exporters(
    obs: Observability, rec: RecordingExporter
) -> None:
    obs.shutdown()
    assert rec.shutdown_calls == 1


def test_auto_exporters_falls_back_to_noop(monkeypatch) -> None:
    monkeypatch.delenv("OBSERVABILITY_SQL_URL", raising=False)
    monkeypatch.delenv("OBSERVABILITY_SQL_ODBC_CONNECT", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    obs = Observability(tool_name="x", app_env="test", attach_json_logger=False)
    assert len(obs.exporters) == 1
    from alpenland_observability.exporters.base import NoopExporter
    assert isinstance(obs.exporters[0], NoopExporter)


def test_auto_exporters_picks_up_sql_url(monkeypatch) -> None:
    pytest.importorskip("alpenland_observability_db")
    monkeypatch.setenv("OBSERVABILITY_SQL_URL", "sqlite:///:memory:")
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    obs = Observability(tool_name="x", app_env="test", attach_json_logger=False)
    types = {type(e).__name__ for e in obs.exporters}
    assert "SqlExporter" in types
