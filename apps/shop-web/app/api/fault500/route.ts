import { ORDERS_URL, proxy } from '@/lib/proxy';

export const dynamic = 'force-dynamic';

export function GET() {
  return proxy('/api/fault500', `${ORDERS_URL}/fault/500`);
}
