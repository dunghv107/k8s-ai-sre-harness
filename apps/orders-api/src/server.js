const express = require('express');
const { Pool } = require('pg');
const { log } = require('./log');

const PORT = Number(process.env.PORT || 8080);
const CATALOG_URL = process.env.CATALOG_URL || 'http://catalog-api:8080';
const INVOICE_URL = process.env.INVOICE_URL || 'http://invoice-api:8080';

const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 5, connectionTimeoutMillis: 2000 });
// without this an idle-client error would crash the process
pool.on('error', (err) => log('error', 'pg pool error', { error: err.message }));

const fault = { rate: 0, status: 500, until: 0 };
const leak = [];

const app = express();
app.disable('x-powered-by');
app.use(express.json());

// log after the response so route and status are final; the span is still active here
app.use((req, res, next) => {
  const start = process.hrtime.bigint();
  res.on('finish', () => {
    const route = req.route ? (req.baseUrl || '') + req.route.path : 'unmatched';
    log(res.statusCode >= 500 ? 'error' : 'info', 'request', {
      method: req.method, route, status: res.statusCode, duration_ms: Number(process.hrtime.bigint() - start) / 1e6,
    });
  });
  next();
});

app.get('/healthz', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'ok' });
  } catch (err) {
    log('error', 'healthz db check failed', { error: err.message, stack: err.stack });
    res.status(503).json({ status: 'db unavailable' });
  }
});

app.get('/fault/500', (req, res, next) => next(new Error('deliberate 500 from /fault/500')));
app.get('/fault/400', (req, res) => res.status(400).json({ error: 'deliberate 400' }));
app.get('/fault/slow', async (req, res) => {
  const ms = Math.min(Math.max(Number(req.query.ms) || 2000, 0), 30000);
  await new Promise((r) => setTimeout(r, ms));
  res.json({ slept_ms: ms });
});
app.post('/fault/error-rate', (req, res) => {
  const rate = Math.min(Math.max(Number(req.query.rate ?? 0.3), 0), 1);
  const status = Number(req.query.status ?? 500);
  const seconds = Number(req.query.seconds ?? 300);
  if (!Number.isInteger(status) || status < 400 || status > 599 || !Number.isFinite(seconds)) {
    return res.status(400).json({ error: 'bad rate, status or seconds' });
  }
  Object.assign(fault, { rate, status, until: Date.now() + seconds * 1000 });
  log('warn', 'error-rate fault armed', { rate, status, seconds });
  res.json({ rate, status, seconds });
});
app.post('/fault/crash', (req, res) => {
  log('error', 'crash requested via /fault/crash');
  res.on('finish', () => process.exit(1));
  res.json({ crashing: true });
});
app.post('/fault/oom', (req, res) => {
  log('error', 'oom requested via /fault/oom');
  res.json({ allocating: true });
  setImmediate(() => { for (;;) leak.push(Buffer.alloc(16 * 1024 * 1024, 1)); });
});

// business routes only: injected failure ahead of real work
app.use('/api', (req, res, next) => {
  if (Date.now() < fault.until && Math.random() < fault.rate) {
    return next(Object.assign(new Error('injected failure by /fault/error-rate'), { status: fault.status }));
  }
  next();
});

app.post('/api/orders', async (req, res, next) => {
  try {
    const { product_id, qty } = req.body || {};
    if (!Number.isInteger(product_id) || !Number.isInteger(qty) || qty < 1) {
      return res.status(400).json({ error: 'product_id and qty (>= 1) must be integers' });
    }
    const pr = await fetch(`${CATALOG_URL}/api/products/${product_id}`);
    if (!pr.ok) {
      return res.status(pr.status === 404 ? 404 : 502).json({ error: `catalog returned ${pr.status}` });
    }
    const amount_cents = (await pr.json()).price_cents * qty;
    const { rows } = await pool.query(
      'INSERT INTO orders.orders (product_id, qty, amount_cents) VALUES ($1, $2, $3) RETURNING *',
      [product_id, qty, amount_cents],
    );
    const order = rows[0];
    const ir = await fetch(`${INVOICE_URL}/api/invoices`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ order_id: order.id, amount_cents }),
    });
    if (!ir.ok) {
      // shortcut: the order row stays without an invoice, add a retry or outbox when this matters
      return res.status(502).json({ error: `invoice returned ${ir.status}`, order_id: order.id });
    }
    res.status(201).json({ ...order, invoice: await ir.json() });
  } catch (err) {
    next(err);
  }
});

app.get('/api/orders/:id', async (req, res, next) => {
  try {
    const id = Number(req.params.id);
    if (!Number.isInteger(id)) return res.status(400).json({ error: 'id must be an integer' });
    const { rows } = await pool.query('SELECT * FROM orders.orders WHERE id = $1', [id]);
    if (!rows[0]) return res.status(404).json({ error: 'order not found' });
    res.json(rows[0]);
  } catch (err) {
    next(err);
  }
});

app.use((req, res) => res.status(404).json({ error: 'not found' }));

// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  const status = err.status && err.status >= 400 ? err.status : 500;
  if (status >= 500) log('error', 'unhandled error', { error: err.message, stack: err.stack });
  res.status(status).json({ error: status >= 500 ? 'internal error' : err.message });
});

app.listen(PORT, () => log('info', 'listening', { port: PORT }));
