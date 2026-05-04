# limewood-observability

[![ci](https://github.com/Limewood-Innovations/limewood-observability/actions/workflows/ci.yml/badge.svg)](https://github.com/Limewood-Innovations/limewood-observability/actions/workflows/ci.yml)

Three-line observability for all 24 Alpenland tools.

Implements the consumer-facing API of [[Alpenland — Observability Concept]].
Pairs with:

- [`limewood-observability-db`](https://github.com/Limewood-Innovations/limewood-observability-db) — DB tier
- [`limewood-monitoring-infra`](https://github.com/Limewood-Innovations/limewood-monitoring-infra) — Bicep IaC

## What it does

For each tool invocation:

* **runs**       — one row per process invocation (`tool_name`, `app_env`, `success`, `duration_ms`, `metrics_json`)
* **metrics**    — counter time-series (`docs_added`, `requests_handled`, …)
* **external**   — outbound HTTP/DB call ledger (target, operation, status, duration)
* **logging**    — JSON formatter that auto-injects `run_id` into every record

All four feed the same sinks (SQL via `limewood-observability-db`, optional
Application Insights). Drop-in for batch jobs (one run per process) and
long-running services (one run per business operation).

## Install

```bash
# minimal: no exporters, every call is a no-op (useful in tests)
pip install limewood-observability

# with the SQL exporter for the cold-path MSSQL store
pip install "limewood-observability[sql]"

# with the AppInsights/OTLP exporter for the hot-path
pip install "limewood-observability[appinsights]"

# everything
pip install "limewood-observability[all]"
```

## Three-line adoption

```python
from limewood_observability import Observability
obs = Observability(tool_name="hermes", app_env=os.environ["APP_ENV"])
async with obs.run() as run:
    await existing_main()
```

That's it. `run` is the handle for everything else:

```python
async with obs.run() as run:
    logger = run.logger          # JSON formatter, run_id auto-injected
    logger.info("starting batch")

    run.record_metric("documents_processed", 42, dims={"pipeline": "rebe"})

    async with run.track_external_call(target="enaio", operation="documents/search") as call:
        result = await client.post_json(...)
        call.set_status(200)

    run.set_metrics({"docs_added": 42, "duration_seconds": 17.3})
    # success=True implicit on clean exit; explicit on failure:
    # run.fail(error_class="EnaioAuthError", error_message=str(exc))
```

## Configuration

| Env var | Purpose | Default |
|---------|---------|---------|
| `APP_ENV` | dev / stage / prod | (required if not passed) |
| `OBSERVABILITY_SQL_URL` | SQLAlchemy URL for the cold-path DB | unset → SQL exporter disabled |
| `OBSERVABILITY_SQL_ODBC_CONNECT` | Same DB but as ODBC connect string (MSSQL convenience) | unset |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | AppInsights hot-path | unset → AppInsights disabled |
| `OBSERVABILITY_HOST` | Override the host string written into runs | `socket.gethostname()` |
| `OBSERVABILITY_TOOL_VERSION` | Override the tool_version field | unset |

Exporters are **fail-soft**: if the SQL DB is unreachable, the run continues
and a single ERROR is logged. Telemetry must never break business workflows.

## Architecture

```
┌────────────┐   start_run() / record_metric() / track_external_call() / fail()
│ Your tool  │ ────────────────────────────────────────┐
└────────────┘                                          │
                                                        ▼
                                              ┌──────────────────┐
                                              │  Observability   │
                                              │      + Run       │
                                              └────┬─────────┬───┘
                                            buffer │         │ JSON-logger
                              ┌──────────────────┐ │         │ (stdlib)
                              │  exporters[*]    │◀┘         ▼
                              └────┬───────┬─────┘     stdout / *.log
                                   │       │
                ┌──────────────────┘       └──────────────────┐
                ▼                                              ▼
  limewood-observability-db                         azure-monitor-opentelemetry
  (MSSQL observability schema)                       (Application Insights)
```

## Testing

```bash
pip install -e ".[dev]"
pytest -q
```

Tests use the `noop` exporter (default when no env vars are set) plus the
`sql` exporter against in-memory SQLite via `limewood-observability-db`.
