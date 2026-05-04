"""alpenland-observability — runs, metrics, and external-call ledger for all
Alpenland tools."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _v

from ._ulid import new_ulid
from .exporters.base import Exporter, NoopExporter
from .logger import JsonFormatter, attach_json_handler
from .observability import Observability
from .run import (
    ExternalCallSnapshot,
    ExternalCallTracker,
    MetricSnapshot,
    Run,
    RunSnapshot,
)

try:
    __version__ = _v("alpenland-observability")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+local"

__all__ = [
    "Exporter",
    "ExternalCallSnapshot",
    "ExternalCallTracker",
    "JsonFormatter",
    "MetricSnapshot",
    "NoopExporter",
    "Observability",
    "Run",
    "RunSnapshot",
    "attach_json_handler",
    "new_ulid",
    "__version__",
]
