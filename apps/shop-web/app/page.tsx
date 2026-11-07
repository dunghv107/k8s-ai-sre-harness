'use client';

import { useState } from 'react';
import styles from './page.module.css';

type Result = { label: string; status: number; body: string } | null;

const post = (b: object): RequestInit => ({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b) });

const actions: { label: string; path: string; init?: RequestInit }[] = [
  { label: 'List products', path: '/api/products' },
  { label: 'Product 1', path: '/api/products/1' },
  { label: 'Product 2', path: '/api/products/2' },
  { label: 'Product 3', path: '/api/products/3' },
  { label: 'Product 4 (500)', path: '/api/products/4' },
  { label: 'Place order (product 1, qty 1)', path: '/api/orders', init: post({ product_id: 1, qty: 1 }) },
  { label: 'Trigger 400 (qty 0)', path: '/api/orders', init: post({ product_id: 1, qty: 0 }) },
  { label: 'Trigger 500 (orders-api)', path: '/api/fault500' },
];

export default function Home() {
  const [result, setResult] = useState<Result>(null);
  const [busy, setBusy] = useState(false);

  async function run(a: (typeof actions)[number]) {
    setBusy(true);
    try {
      const res = await fetch(a.path, a.init);
      const text = await res.text();
      let body = text;
      try { body = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw text */ }
      setResult({ label: a.label, status: res.status, body });
    } catch (err) {
      setResult({ label: a.label, status: 0, body: String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className={styles.main}>
      <h1>Demo shop</h1>
      <p className={styles.hint}>Each button calls a Next route handler that proxies to the backends.</p>
      <div className={styles.buttons}>
        {actions.map((a) => (
          <button key={a.label} disabled={busy} onClick={() => run(a)}>{a.label}</button>
        ))}
      </div>
      {result && (
        <section className={styles.result}>
          <h2>{result.label}</h2>
          <p className={result.status >= 400 || result.status === 0 ? styles.bad : styles.ok}>Status {result.status}</p>
          <pre>{result.body}</pre>
        </section>
      )}
    </main>
  );
}
