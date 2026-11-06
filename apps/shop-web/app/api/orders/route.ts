import { ORDERS_URL, proxy } from '@/lib/proxy';

export const dynamic = 'force-dynamic';

export async function POST(req: Request) {
  return proxy('/api/orders', `${ORDERS_URL}/api/orders`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: await req.text(),
  });
}
