# orders-api

Node.js 22 + Express. Routes and fault endpoints follow `docs/contract.md`.

## Expected table

Created by the Postgres init script, this service runs no migrations:

```sql
CREATE SCHEMA IF NOT EXISTS orders;
CREATE TABLE orders.orders (
  id           serial PRIMARY KEY,
  product_id   int NOT NULL,
  qty          int NOT NULL,
  amount_cents int NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
```

## Env

`PORT`, `DATABASE_URL`, `CATALOG_URL`, `INVOICE_URL`, plus the standard `OTEL_*` variables.
Tracing is loaded by `NODE_OPTIONS=--require @opentelemetry/auto-instrumentations-node/register` set in the Dockerfile.

## Build

```sh
docker build -t homelab/orders-api:0.1.0 apps/orders-api
```
