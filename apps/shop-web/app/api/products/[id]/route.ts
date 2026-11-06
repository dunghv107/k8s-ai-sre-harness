import { CATALOG_URL, proxy } from '@/lib/proxy';

export const dynamic = 'force-dynamic';

export async function GET(_req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  return proxy('/api/products/[id]', `${CATALOG_URL}/api/products/${encodeURIComponent(id)}`);
}
