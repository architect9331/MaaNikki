import { useRef, useState } from 'react';
import { ClipboardPaste, FileInput, FolderOpen, Layers, Plus, Search, Sparkles, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import { revealItemInDir } from '@tauri-apps/plugin-opener';
import { primaryButton, secondaryButton, Segmented } from '@/components/ui/FeaturePage';
import type { RouteSummary } from '@/services/routeStudioService';
import type { MapInfo } from './RouteMapCanvas';
import { field } from './routeEditorShared';

/** Left column: find, open and create routes. Everything that starts a document lives here. */
export function RouteLibrary({ routes, maps, activeId, onOpen, onDelete, onNewRoute, onNewCollection, onImportFile, onPaste, onGenerate }: {
  routes: { user: RouteSummary[]; builtin: RouteSummary[] };
  maps: MapInfo[];
  activeId?: string;
  onOpen: (summary: RouteSummary) => void;
  onDelete: (summary: RouteSummary) => void;
  onNewRoute: () => void;
  onNewCollection: () => void;
  onImportFile: (file?: File) => void;
  onPaste: () => void;
  onGenerate: () => void;
}) {
  const [tab, setTab] = useState<'user' | 'builtin'>('user');
  const [query, setQuery] = useState('');
  const [mapFilter, setMapFilter] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const q = query.trim().toLowerCase();
  const matches = (r: RouteSummary) => (!q || `${r.title ?? ''} ${r.id}`.toLowerCase().includes(q))
    && (!mapFilter || r.kind === 'route_collection' || r.map === mapFilter);
  const list = (tab === 'user' ? routes.user : routes.builtin).filter(matches);
  const otherCount = (tab === 'user' ? routes.builtin : routes.user).filter(matches).length;
  const mapTitle = (id?: string) => maps.find((m) => m.id === id)?.title ?? id ?? '';

  return <aside className="flex w-64 shrink-0 flex-col border-r border-border bg-bg-secondary">
    <div className="space-y-2 p-2.5">
      <div className="grid grid-cols-2 gap-1.5">
        <button onClick={onNewRoute} className={clsx(primaryButton, 'justify-center')}><Plus className="h-3.5 w-3.5" />新建路线</button>
        <button onClick={onGenerate} title="按目标素材自动规划姜饼人采集路线" className={clsx(secondaryButton, 'justify-center')}><Sparkles className="h-3.5 w-3.5" />智能生成</button>
        <button onClick={onNewCollection} title="把多条路线打包按顺序运行" className={clsx(secondaryButton, 'justify-center px-2')}><Layers className="h-3.5 w-3.5" />新建清单</button>
        <div className="flex gap-1.5">
          <button onClick={() => fileInput.current?.click()} title="从路线文件导入" className={clsx(secondaryButton, 'flex-1 justify-center px-2')}><FileInput className="h-3.5 w-3.5" /></button>
          <button onClick={onPaste} title="从剪贴板粘贴路线" className={clsx(secondaryButton, 'flex-1 justify-center px-2')}><ClipboardPaste className="h-3.5 w-3.5" /></button>
        </div>
        <input ref={fileInput} type="file" accept=".json" className="hidden" onChange={(event) => { onImportFile(event.target.files?.[0]); event.target.value = ''; }} />
      </div>
      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text-muted" />
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索材料 / 区域 / 路线…" className={clsx(field, 'pl-8')} />
      </div>
      <div className="flex items-center gap-1.5">
        <Segmented label="路线来源" value={tab} onChange={setTab}
          options={[['user', `我的 ${routes.user.filter(matches).length}`], ['builtin', `内置 ${routes.builtin.filter(matches).length}`]]} />
        <select aria-label="按地图筛选" value={mapFilter} onChange={(e) => setMapFilter(e.target.value)}
          className="min-w-0 flex-1 rounded-lg border border-border bg-bg-primary px-1.5 py-1 text-xs outline-none">
          <option value="">全部地图</option>
          {maps.map((m) => <option key={m.id} value={m.id}>{m.title}</option>)}
        </select>
      </div>
    </div>
    <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
      {!list.length && <p className="px-2 py-4 text-center text-xs leading-5 text-text-muted">
        {tab === 'user' && !routes.user.length ? <>还没有自建路线。<br />新建、录制或从内置路线另存一条吧。</> : '没有匹配的路线'}
        {otherCount > 0 && <button onClick={() => setTab(tab === 'user' ? 'builtin' : 'user')} className="mt-1 block w-full text-accent hover:underline">
          {tab === 'user' ? '内置' : '我的'}路线里有 {otherCount} 条匹配
        </button>}
      </p>}
      {list.map((item) => <div key={item.id} className={clsx('group mb-0.5 flex items-center rounded-lg', activeId === item.id ? 'bg-accent/10 text-accent' : 'hover:bg-bg-hover')}>
        <button onClick={() => onOpen(item)} title={item.title || item.id} className="min-w-0 flex-1 px-2.5 py-1.5 text-left">
          <span className="flex items-center gap-1 truncate text-xs font-medium">
            {item.kind === 'route_collection' && <Layers className="h-3 w-3 shrink-0 text-accent-secondary" />}
            <span className="truncate">{item.title || item.id}</span>
          </span>
          <span className="block truncate text-[11px] text-text-muted">
            {item.kind === 'route_collection' ? `路线清单 · ${item.points} 条` : `${mapTitle(item.map)} · ${item.points} 点`}
          </span>
        </button>
        {!item.builtin && <>
          <button onClick={() => void revealItemInDir(item.path)} title="打开所在文件夹" className="hidden p-1.5 text-text-muted hover:text-text-primary group-hover:block"><FolderOpen className="h-3.5 w-3.5" /></button>
          <button onClick={() => onDelete(item)} title="删除" className="hidden p-1.5 pr-2 text-text-muted hover:text-error group-hover:block"><Trash2 className="h-3.5 w-3.5" /></button>
        </>}
      </div>)}
    </div>
  </aside>;
}
