import logging
import os
import random
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("catalog")

# auto_configure off: opentelemetry-instrument already sets the exporter, FastAPI would add a second one
app = FastAPI(title="catalog-api", telemetry={"auto_configure": False})

# open=False so startup never blocks on an unreachable DB
pool = ConnectionPool(os.getenv("DATABASE_URL", ""), min_size=1, max_size=5, timeout=3, open=False,
                      kwargs={"row_factory": dict_row, "connect_timeout": 3})
# shortcut: in-process fault state, per replica only, move to a shared store if replicas > 1
fault = {"until": 0.0, "rate": 0.0, "status": 500}


@app.on_event("startup")
def open_pool():
    pool.open(wait=False)


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    span = trace.get_current_span()
    try:
        injected = (request.url.path.startswith("/api/") and time.time() < fault["until"]
                    and random.random() < fault["rate"])
        if injected:
            response = JSONResponse({"error": "injected fault"}, status_code=fault["status"])
        else:
            response = await call_next(request)
    except Exception as exc:
        log.exception("unhandled error", exc_info=exc)
        span.record_exception(exc)
        response = JSONResponse({"error": "internal error"}, status_code=500)
    route = getattr(request.scope.get("route"), "path", request.url.path)
    if response.status_code >= 500:
        span.set_status(Status(StatusCode.ERROR))
    log.info("request", extra={"fields": {
        "method": request.method, "route": route, "status": response.status_code,
        "duration_ms": round((time.perf_counter() - start) * 1000, 1)}})
    return response


def price_of(product):
    # deliberate bug: no guard for an empty variants list
    return product["variants"][0]["price_cents"]


@app.get("/api/products")
def list_products():
    with pool.connection() as conn:
        return conn.execute("SELECT id, name FROM catalog.products ORDER BY id").fetchall()


@app.get("/api/products/{product_id}")
def get_product(product_id: int):
    with pool.connection() as conn:
        product = conn.execute("SELECT id, name, variants FROM catalog.products WHERE id = %s",
                               (product_id,)).fetchone()
    if product is None:
        raise HTTPException(404, "product not found")
    return {**product, "price_cents": price_of(product)}


@app.get("/healthz")
def healthz():
    try:
        with pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        log.warning("db unavailable: %s", exc)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@app.get("/fault/500")
def fault_500():
    raise RuntimeError("fault injection: /fault/500")


@app.get("/fault/400")
def fault_400():
    raise HTTPException(400, "fault injection: /fault/400")


@app.get("/fault/slow")
def fault_slow(ms: int = 2000):
    ms = min(max(ms, 0), 30000)
    time.sleep(ms / 1000)
    return {"slept_ms": ms}


@app.post("/fault/error-rate")
def fault_error_rate(rate: float = 0.3, status: int = 500, seconds: int = 300):
    fault.update(until=time.time() + seconds, rate=rate, status=status)
    return {"rate": rate, "status": status, "seconds": seconds}


@app.post("/fault/crash")
def fault_crash():
    threading.Timer(0.2, os._exit, args=(1,)).start()
    return JSONResponse({"status": "exiting"}, status_code=202)


@app.post("/fault/oom")
def fault_oom():
    def eat():
        hog = []
        while True:
            hog.append(bytearray(10 * 1024 * 1024))
            time.sleep(0.01)

    threading.Thread(target=eat, daemon=True).start()
    return JSONResponse({"status": "allocating"}, status_code=202)
