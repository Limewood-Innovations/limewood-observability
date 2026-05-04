"""SQL exporter — writes runs, metrics, and external-call records to MSSQL
(or any SQLAlchemy-supported DB) via :mod:`limewood_observability_db`.

Construction is decoupled from import-time so the dependency on
``limewood-observability-db`` is **optional** (extras = ``[sql]``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

from .base import Exporter

if TYPE_CHECKING:
    from limewood_observability_db import ObservabilityConnector

    from ..run import ExternalCallSnapshot, MetricSnapshot, RunSnapshot

_LOGGER = logging.getLogger(__name__)


class SqlExporter(Exporter):
    """Persist observability events into ``observability.*`` tables.

    Args:
        connector: A constructed
            :class:`limewood_observability_db.ObservabilityConnector`.
        run_migrations: When True, call ``connector.run_migrations()`` on
            construction. Default True so first-time integrators (e.g. tests
            against in-memory SQLite) don't have to remember the step.
            **In production**, the schema is typically pre-created by an
            operator running with admin credentials — tools then connect
            with a least-privilege role that lacks DDL. Migration calls
            from a least-privilege role are caught and downgraded to an
            INFO log; other migration failures still surface as ERROR.
    """

    def __init__(
        self,
        connector: "ObservabilityConnector",
        *,
        run_migrations: bool = True,
    ) -> None:
        self._connector = connector
        if run_migrations:
            try:
                connector.run_migrations()
            except Exception as exc:
                # Common case in production: writer role has no DDL rights.
                # Schema is supposed to be pre-created by the operator (e.g.
                # via limewood-monitoring-infra/scripts/setup-postgres.sh).
                # Recognise the error by name (no hard import dep on psycopg
                # here — works for any DBAPI that surfaces "permission denied").
                exc_class = type(exc).__name__
                exc_msg = str(exc).lower()
                if "InsufficientPrivilege" in exc_class or "permission denied" in exc_msg:
                    _LOGGER.info(
                        "Skipping schema migration — writer role lacks DDL. "
                        "Assuming schema was pre-created by an operator. (%s)",
                        exc_class,
                    )
                else:
                    _LOGGER.error(
                        "limewood-observability SQL exporter: migration failed",
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # Exporter protocol
    # ------------------------------------------------------------------

    def export_run_started(self, snapshot: "RunSnapshot") -> None:
        from limewood_observability_db import RunRecord
        try:
            with self._connector.connect() as c:
                self._connector.runs.insert(
                    c,
                    RunRecord(
                        run_id=snapshot.run_id,
                        tool_name=snapshot.tool_name,
                        tool_version=snapshot.tool_version,
                        app_env=snapshot.app_env,
                        host=snapshot.host,
                        started_at=snapshot.started_at,
                        parent_run_id=snapshot.parent_run_id,
                    ),
                )
        except Exception:
            _LOGGER.error("SQL export of run_started failed", exc_info=True)

    def export_run_finished(self, snapshot: "RunSnapshot") -> None:
        from limewood_observability_db import RunFinish
        if snapshot.finished_at is None or snapshot.duration_ms is None:
            _LOGGER.warning(
                "SQL exporter: run_finished without finished_at/duration — skipped"
            )
            return
        try:
            with self._connector.connect() as c:
                self._connector.runs.finish(
                    c,
                    RunFinish(
                        run_id=snapshot.run_id,
                        finished_at=snapshot.finished_at,
                        duration_ms=snapshot.duration_ms,
                        success=bool(snapshot.success),
                        error_class=snapshot.error_class,
                        error_message=snapshot.error_message,
                        metrics=snapshot.metrics or {},
                    ),
                )
        except Exception:
            _LOGGER.error("SQL export of run_finished failed", exc_info=True)

    def export_metrics(self, samples: Iterable["MetricSnapshot"]) -> None:
        from limewood_observability_db import MetricSample
        records = [
            MetricSample(
                run_id=s.run_id,
                tool_name=s.tool_name,
                metric_name=s.metric_name,
                metric_value=s.metric_value,
                sampled_at=s.sampled_at,
                dims=s.dims,
            )
            for s in samples
        ]
        if not records:
            return
        try:
            with self._connector.connect() as c:
                self._connector.metrics.bulk_insert(c, records)
        except Exception:
            _LOGGER.error("SQL export of metrics failed", exc_info=True)

    def export_external_calls(self, calls: Iterable["ExternalCallSnapshot"]) -> None:
        from limewood_observability_db import ExternalCallRecord
        records = [
            ExternalCallRecord(
                run_id=c.run_id,
                tool_name=c.tool_name,
                target=c.target,
                operation=c.operation,
                http_status=c.http_status,
                duration_ms=c.duration_ms,
                success=c.success,
                error_message=c.error_message,
                started_at=c.started_at,
            )
            for c in calls
        ]
        if not records:
            return
        try:
            with self._connector.connect() as conn:
                self._connector.external_calls.bulk_insert(conn, records)
        except Exception:
            _LOGGER.error("SQL export of external_calls failed", exc_info=True)

    def shutdown(self) -> None:
        try:
            self._connector.dispose()
        except Exception:
            _LOGGER.warning("SQL exporter: dispose failed", exc_info=True)
