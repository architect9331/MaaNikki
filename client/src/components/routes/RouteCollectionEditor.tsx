import { ArrowDown, ArrowUp, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { SwitchTrack } from '@/components/ui/SwitchTrack';
import type { RouteCollectionDocument } from '@/services/routeDocument';
import type { RouteSummary } from '@/services/routeStudioService';

/** A playlist runs several routes in order; it has no map, so it replaces the canvas. */
export function RouteCollectionEditor({ collection, routes, readOnly, onChange }: {
  collection: RouteCollectionDocument;
  routes: { user: RouteSummary[]; builtin: RouteSummary[] };
  readOnly: boolean;
  onChange: (next: RouteCollectionDocument) => void;
}) {
  const swap = (a: number, b: number) => {
    if (b < 0 || b >= collection.routes.length) return;
    const list = [...collection.routes];
    [list[a], list[b]] = [list[b], list[a]];
    onChange({ ...collection, routes: list });
  };
  return <div className="flex h-full flex-col gap-4 overflow-auto p-6">
    <fieldset disabled={readOnly} className="max-w-2xl space-y-4">
      <div className="flex items-center justify-between rounded-xl border border-border bg-bg-secondary p-4">
        <div>
          <h2 className="text-sm font-semibold">多路线顺序清单</h2>
          <p className="mt-0.5 text-xs text-text-muted">按顺序自动执行多条路线；某条受阻时可自动恢复主界面并继续下一条。</p>
        </div>
        <button type="button" role="switch" aria-checked={collection.continue_on_failure}
          onClick={() => onChange({ ...collection, continue_on_failure: !collection.continue_on_failure })}
          className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs">
          单条失败继续下一条<SwitchTrack checked={collection.continue_on_failure} compact />
        </button>
      </div>
      <div className="rounded-xl border border-border bg-bg-secondary p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-xs font-semibold text-text-secondary">已包含路线（{collection.routes.length}）</h3>
          <select className="rounded-lg border border-border bg-bg-primary px-2.5 py-1 text-xs outline-none" value=""
            onChange={(event) => {
              const found = [...routes.user, ...routes.builtin].find((r) => r.id === event.target.value && r.kind !== 'route_collection');
              if (!found) return;
              if (collection.routes.some((r) => r.route === found.id)) {
                toast.error('该路线已在清单中');
                return;
              }
              onChange({
                ...collection,
                routes: [...collection.routes, { route: found.id, title: found.title || found.id, ...(found.builtin ? {} : { file: found.path }) }],
              });
            }}>
            <option value="">+ 添加一条路线到清单…</option>
            {routes.user.filter((r) => r.kind !== 'route_collection').map((r) => <option key={r.id} value={r.id}>{r.title || r.id}</option>)}
            {routes.builtin.filter((r) => r.runnable !== false && r.kind !== 'route_collection').map((r) => <option key={r.id} value={r.id}>[内置] {r.title || r.id}</option>)}
          </select>
        </div>
        {!collection.routes.length ? <p className="py-6 text-center text-xs text-text-muted">还没有添加路线，从右上角下拉框选择要串联执行的路线。</p>
          : <ol className="space-y-1.5">
            {collection.routes.map((entry, idx) => <li key={entry.route} className="flex items-center gap-2 rounded-lg border border-border bg-bg-primary px-3 py-2 text-xs">
              <span className="w-5 tabular-nums text-text-muted">{idx + 1}.</span>
              <span className="flex-1 truncate font-medium">{entry.title || entry.route}</span>
              <button onClick={() => swap(idx, idx - 1)} title="上移" className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text-primary"><ArrowUp className="h-3.5 w-3.5" /></button>
              <button onClick={() => swap(idx, idx + 1)} title="下移" className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text-primary"><ArrowDown className="h-3.5 w-3.5" /></button>
              <button onClick={() => onChange({ ...collection, routes: collection.routes.filter((_, i) => i !== idx) })} title="移出清单"
                className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-error"><Trash2 className="h-3.5 w-3.5" /></button>
            </li>)}
          </ol>}
      </div>
    </fieldset>
  </div>;
}
