# Sample app contract

Shared by the app code, the Helm chart and the observability stack. Change it here first.

## Services

Namespace `shop`, all in-cluster, plain HTTP inside the cluster.

| Service | Language | Port | Calls | Owns |
|---|---|---|---|---|
| `shop-web` | Next.js (App Router, standalone build) | 3000 | `catalog-api`, `orders-api` (server side only) | — |
| `orders-api` | Node.js + Express | 8080 | `catalog-api`, `invoice-api`, Postgres | schema `orders` |
| `catalog-api` | Python + FastAPI | 8080 | Postgres | schema `catalog` |
| `invoice-api` | Go (net/http) | 8080 | Postgres | schema `invoice` |
| `postgres` | PostgreSQL 17 | 5432 | — | database `shop` |

Images: `homelab/<service>:0.1.0`, built locally and loaded with `k3d image import` (no registry, no `latest`).

## Business endpoints

| Service | Route | Behaviour |
|---|---|---|
| catalog-api | `GET /api/products` | list products |
| catalog-api | `GET /api/products/{id}` | 404 if missing; 500 if the product has no variant (`variants` empty), the deliberate bug |
| orders-api | `POST /api/orders` `{"product_id":int,"qty":int}` | 400 on bad body or qty < 1; fetches price from catalog, inserts order, calls invoice-api, returns 201 |
| orders-api | `GET /api/orders/{id}` | 404 if missing |
| invoice-api | `POST /api/invoices` `{"order_id":int,"amount_cents":int}` | 400 if amount <= 0; inserts invoice, 201 |
| shop-web | `/` | page with buttons: list products, open product 1/2/3, place order, trigger 400, trigger 500 |
| shop-web | `/api/*` | route handlers that proxy to the backends so the browser never calls them directly |

Seed data (created by the Postgres init script): products 1–3 have variants, product 4 has none.

## Endpoints every backend exposes

| Route | Behaviour |
|---|---|
| `GET /healthz` | 200 when the DB answers `SELECT 1`, else 503 |
| `GET /fault/500` | always 500 with a logged error and stack |
| `GET /fault/400` | always 400 |
| `GET /fault/slow?ms=2000` | sleeps then 200 (cap 30000) |
| `POST /fault/error-rate?rate=0.3&status=500&seconds=300` | for `seconds`, business routes fail with `status` at `rate` |
| `POST /fault/crash` | process exits 1 (CrashLoop test) |
| `POST /fault/oom` | allocates memory until the container is OOMKilled |

## Environment

| Variable | Example |
|---|---|
| `PORT` | `8080` (`3000` for shop-web) |
| `DATABASE_URL` | `postgres://shop:<from Secret>@postgres:5432/shop` (from Secret `postgres-app`, key `DATABASE_URL`) |
| `CATALOG_URL` / `ORDERS_URL` / `INVOICE_URL` | `http://catalog-api:8080` … |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://alloy.observability.svc:4318` (OTLP/HTTP) |
| `OTEL_SERVICE_NAME` | the service name |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=production,service.namespace=shop` |
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` (100 % sampling) |

## Telemetry

- Traces: OTLP/HTTP to Alloy, 100 %, W3C `traceparent` propagated on every outbound call; DB calls have spans.
- Server spans carry `http.request.method`, `http.route`, `http.response.status_code`; 5xx sets span status ERROR.
- Logs: one JSON object per line on stdout: `ts`, `level`, `msg`, `service`, `trace_id`, `span_id`, and for requests
  `method`, `route`, `status`, `duration_ms`. Errors add `error` and `stack`.
- Alloy turns server spans into `traces_span_metrics_*` in Prometheus; the 5xx alert reads those.

## Kubernetes conventions

- Pod: `runAsNonRoot`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true` (writable `emptyDir` at `/tmp`
  where the runtime needs it), `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, requests and limits set.
- Labels: `app.kubernetes.io/name`, `app.kubernetes.io/part-of: shop`.
- NetworkPolicy: default deny in `shop`; allow only the call graph above, DNS, and egress to Alloy.
