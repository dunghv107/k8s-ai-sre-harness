import { CATALOG_URL, proxy } from '@/lib/proxy';

export const dynamic = 'force-dynamic';

export function GET() {
  return proxy('/api/products', `${CATALOG_URL}/api/products`);
}
