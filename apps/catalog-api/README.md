# catalog-api

FastAPI service. Routes and fault endpoints: see `docs/contract.md`.

Expects a table created by the Postgres init script (this service does not migrate):

```sql
CREATE TABLE catalog.products (id int PRIMARY KEY, name text, variants jsonb);
-- variants: [{"sku": "...", "price_cents": 1999}]; product price = first variant
```

`GET /api/products/{id}` returns 500 when `variants` is empty. This is the deliberate bug the demo diagnoses.

Build: `docker build -t homelab/catalog-api:0.1.0 .`
