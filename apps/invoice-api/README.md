# invoice-api

Go `net/http` service. Routes and fault endpoints: see `docs/contract.md`.

Expects a table created by the Postgres init script (this service does not migrate):

```sql
CREATE TABLE invoice.invoices (
  id serial PRIMARY KEY, order_id int, amount_cents int, created_at timestamptz DEFAULT now());
```

Build: `docker build -t homelab/invoice-api:0.1.0 .`
