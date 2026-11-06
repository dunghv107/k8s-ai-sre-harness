import json
import logging
import os
import time
import traceback

from opentelemetry import trace

SERVICE = os.getenv("OTEL_SERVICE_NAME", "catalog-api")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        ctx = trace.get_current_span().get_span_context()
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            "service": SERVICE,
        }
        if ctx.is_valid:
            out["trace_id"] = format(ctx.trace_id, "032x")
            out["span_id"] = format(ctx.span_id, "016x")
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["error"] = str(record.exc_info[1])
            out["stack"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(out)
