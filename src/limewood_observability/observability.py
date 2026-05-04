"""``Observability`` — the entry-point class every tool constructs once."""

from __future__ import annotations

import logging
import os
import socket
from typing import Sequence

from ._ulid import new_ulid
from .exporters.base import Exporter, NoopExporter
from .logger import attach_json_handler
from .run import Run

_LOGGER = logging.getLogger(__name__)


class Observability:
    """Construct **once** at process start; produces :class:`Run` instances.

    Args:
        tool_name: Required. The string written to ``observability.runs.tool_name``
            and used as the ``tool_name`` attribute on every metric / external-call
            row. Must be unique per project (``"doc_search"``, ``"hermes"``, …).
        app_env: ``"dev"`` / ``"stage"`` / ``"prod"``. Defaults to ``$APP_ENV``.
        tool_version: Optional version string (commit SHA, semver, …).
            Defaults to ``$OBSERVABILITY_TOOL_VERSION``.
        host: Override for the host string. Defaults to ``$OBSERVABILITY_HOST``
            then ``socket.gethostname()``.
        exporters: Override the auto-discovered exporters. Mostly useful for
            tests. By default :meth:`auto_exporters` is called.
        attach_json_logger: When True, attach a :class:`JsonFormatter`
            stream handler to the root logger. Default True. Set to False
            if your application already configures structured logging.

    Adoption pattern:

    ::

        obs = Observability(tool_name="doc_search", app_env=os.environ["APP_ENV"])
        async with obs.run() as run:
            await main_logic(run)
    """

    def __init__(
        self,
        *,
        tool_name: str,
        app_env: str | None = None,
        tool_version: str | None = None,
        host: str | None = None,
        exporters: Sequence[Exporter] | None = None,
        attach_json_logger: bool = True,
        json_logger_level: int = logging.INFO,
    ) -> None:
        if not tool_name:
            raise ValueError("tool_name is required")
        self.tool_name = tool_name
        self.app_env = app_env or os.environ.get("APP_ENV", "dev")
        self.tool_version = tool_version or os.environ.get("OBSERVABILITY_TOOL_VERSION")
        self.host = host or os.environ.get("OBSERVABILITY_HOST") or socket.gethostname()
        self.exporters: list[Exporter] = list(
            exporters if exporters is not None else self.auto_exporters()
        )
        self._logger = logging.getLogger(self.tool_name)
        self._json_handler = None
        if attach_json_logger:
            self._json_handler = attach_json_handler(
                self._logger,
                static_fields={
                    "tool_name": self.tool_name,
                    "app_env": self.app_env,
                },
                level=json_logger_level,
            )

    # ------------------------------------------------------------------
    # exporter discovery
    # ------------------------------------------------------------------

    @staticmethod
    def auto_exporters() -> list[Exporter]:
        """Build the exporter list from environment variables.

        Discovery order:

        1. If ``OBSERVABILITY_SQL_URL`` or ``OBSERVABILITY_SQL_ODBC_CONNECT``
           is set **and** ``limewood-observability-db`` is installed →
           add :class:`SqlExporter`.
        2. If ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set **and**
           ``azure-monitor-opentelemetry`` is installed → add
           :class:`AppInsightsExporter`.
        3. If neither was added → add :class:`NoopExporter` (so calls don't NPE).
        """
        exporters: list[Exporter] = []

        sql_url = os.environ.get("OBSERVABILITY_SQL_URL")
        sql_odbc = os.environ.get("OBSERVABILITY_SQL_ODBC_CONNECT")
        if sql_url or sql_odbc:
            try:
                from limewood_observability_db import ObservabilityConnector

                from .exporters.sql import SqlExporter

                if sql_odbc:
                    connector = ObservabilityConnector.from_odbc(sql_odbc)
                else:
                    connector = ObservabilityConnector.from_url(sql_url)  # type: ignore[arg-type]
                exporters.append(SqlExporter(connector))
                _LOGGER.info("limewood-observability: SqlExporter enabled")
            except Exception:
                _LOGGER.error(
                    "Failed to construct SqlExporter — telemetry will skip SQL",
                    exc_info=True,
                )

        ai_conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
        if ai_conn:
            try:
                from .exporters.appinsights import AppInsightsExporter

                exporters.append(AppInsightsExporter(ai_conn))
                _LOGGER.info("limewood-observability: AppInsightsExporter enabled")
            except Exception:
                _LOGGER.error(
                    "Failed to construct AppInsightsExporter — telemetry will skip AppInsights",
                    exc_info=True,
                )

        if not exporters:
            exporters.append(NoopExporter())
        return exporters

    # ------------------------------------------------------------------
    # public api
    # ------------------------------------------------------------------

    def run(self, *, parent_run_id: str | None = None) -> Run:
        """Construct a fresh :class:`Run`. Use as ``async with obs.run() as run:``."""
        return Run(
            run_id=new_ulid(),
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            app_env=self.app_env,
            host=self.host,
            exporters=self.exporters,
            parent_run_id=parent_run_id,
            logger=self._logger,
        )

    def shutdown(self) -> None:
        """Shut down all exporters (best-effort flush)."""
        for ex in self.exporters:
            try:
                ex.shutdown()
            except Exception:
                _LOGGER.warning("exporter.shutdown failed", exc_info=True)
