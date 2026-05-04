"""End-to-end test: Observability + SqlExporter against in-memory SQLite.

Proves that a Run lifecycle ends up as actual rows in the
``observability.runs`` / ``metric_samples`` / ``external_calls`` tables.
"""

from __future__ import annotations

import json

import pytest

# Whole module skips when the optional alpenland-observability-db dep is missing
# (e.g. CI without the cross-repo PAT for the private dependency).
pytest.importorskip("alpenland_observability_db")

from alpenland_observability_db import ObservabilityConnector  # noqa: E402
from sqlalchemy import select  # noqa: E402

from alpenland_observability import Observability  # noqa: E402
from alpenland_observability.exporters.sql import SqlExporter  # noqa: E402


@pytest.fixture
def connector() -> ObservabilityConnector:
    return ObservabilityConnector.from_url("sqlite:///:memory:", schema=None)


def test_full_run_lifecycle_writes_to_sql(connector: ObservabilityConnector) -> None:
    obs = Observability(
        tool_name="doc_search",
        app_env="test",
        exporters=[SqlExporter(connector)],
        attach_json_logger=False,
    )

    with obs.run() as run:
        with run.track_external_call(
            target="enaio", operation="documents/search"
        ) as call:
            call.set_status(200)
        run.record_metric("docs_added", 17, dims={"pipeline": "rebe"})
        run.record_metric("docs_added", 12, dims={"pipeline": "cn"})
        run.set_metrics({"docs_added_total": 29})

    # Inspect the DB.
    runs = connector.metadata.tables["runs"]
    metrics = connector.metadata.tables["metric_samples"]
    calls = connector.metadata.tables["external_calls"]

    with connector.connect() as c:
        run_row = c.execute(select(runs)).one()._mapping
        metric_rows = list(c.execute(select(metrics)))
        call_rows = list(c.execute(select(calls)))

    assert run_row["tool_name"] == "doc_search"
    assert run_row["app_env"] == "test"
    assert run_row["success"] is True
    assert json.loads(run_row["metrics_json"]) == {"docs_added_total": 29}
    assert run_row["duration_ms"] is not None and run_row["duration_ms"] >= 0

    assert len(metric_rows) == 2
    assert all(r._mapping["metric_name"] == "docs_added" for r in metric_rows)

    assert len(call_rows) == 1
    assert call_rows[0]._mapping["target"] == "enaio"
    assert call_rows[0]._mapping["http_status"] == 200
    assert call_rows[0]._mapping["success"] is True


def test_failed_run_persists_error_class_and_message(
    connector: ObservabilityConnector,
) -> None:
    obs = Observability(
        tool_name="doc_search",
        app_env="test",
        exporters=[SqlExporter(connector)],
        attach_json_logger=False,
    )

    with pytest.raises(RuntimeError):
        with obs.run():
            raise RuntimeError("nope")

    runs = connector.metadata.tables["runs"]
    with connector.connect() as c:
        row = c.execute(select(runs)).one()._mapping
    assert row["success"] is False
    assert row["error_class"] == "RuntimeError"
    assert row["error_message"] == "nope"
