import { useEffect, useMemo, useState } from 'react';
import { Check, Loader2, Search, Sparkles, X } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { primaryButton, secondaryButton } from '@/components/ui/FeaturePage';
import { generateGingerbreadRoutes, type GenerateGingerbreadResponse } from '@/services/routeStudioService';
import { loadMaterialCatalog3D } from './Route3DCanvas';

interface SelectableMaterial {
  name: string;
  kind: 'plant' | 'insect' | 'animal' | 'fish';
  count: number;
  legendary?: boolean;
}

const LEGENDARY_SET = new Set([
  '曦光毛团',
  '天星之羽',
  '盛宴毛毡',
  '围兜毛球',
  '玫色天鹅绒',
  '夜色天鹅绒',
  '晴日蝶兰/雨滴蝶兰',
  '烛融花/烛荧花',
  '日光果/星光果',
  '纱裙鱼',
  '拉夫领鱼',
  '贵族卷毛',
  '鹿冠毛团',
  '涅拉羽',
  '裙撑萤',
]);

const MAP_OPTIONS = [
  ['', '全地图智能优选'],
  ['miraland', '奇迹大陆（心愿原野 / 伊赞之土）'],
  ['wanxiang', '万相境'],
  ['danqing', '丹青屿'],
  ['firework', '花焰群岛'],
  ['serenity', '无忧岛'],
] as const;

const MIRALAND_REGIONS = [
  '',
  '花愿镇',
  '微风绿野',
  '小石树田村',
  '石树田无人区',
  '祈愿树林',
  '巨木之森',
  '蜗牛城',
  '埋骨地',
] as const;

export function RouteGeneratorModal({
  open,
  onClose,
  onGenerated,
}: {
  open: boolean;
  onClose: () => void;
  onGenerated: (res: GenerateGingerbreadResponse) => void;
}) {
  const [mode, setMode] = useState<'target' | 'max-yield'>('target');
  const [materialsList, setMaterialsList] = useState<SelectableMaterial[]>([]);
  const [selectedMats, setSelectedMats] = useState<string[]>(['晴日蝶兰/雨滴蝶兰']);
  const [kindTab, setKindTab] = useState<'all' | 'legendary' | 'plant' | 'insect' | 'animal' | 'fish'>('all');
  const [query, setQuery] = useState('');
  const [categorySweep, setCategorySweep] = useState<'all' | 'plant' | 'insect' | 'animal' | 'fish'>('all');
  const [mapId, setMapId] = useState('');
  const [region, setRegion] = useState('');
  const [topK, setTopK] = useState(3);
  const [quota, setQuota] = useState(0);
  const [radius, setRadius] = useState(13.5);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || materialsList.length > 0) return;
    loadMaterialCatalog3D()
      .then(({ catalogs, spawnersByMap }) => {
        const list: SelectableMaterial[] = [];
        for (const [cid, info] of Object.entries(catalogs)) {
          if (cid === '99' || !info.count) continue;
          list.push({
            name: info.name,
            kind: info.kind,
            count: info.count,
            legendary: LEGENDARY_SET.has(info.name),
          });
        }
        const fishCounts = new Map<string, number>();
        for (const pts of Object.values(spawnersByMap)) {
          for (const p of pts) {
            if (p.kind === 'fish' && p.cond) {
              const cleaned = p.cond.replace(/可能在该钓鱼点出现的鱼类[：:]/g, '');
              for (const sp of cleaned.split(/[、，,/]/)) {
                const name = sp.trim();
                if (name) fishCounts.set(name, (fishCounts.get(name) ?? 0) + 1);
              }
            }
          }
        }
        for (const [fName, cnt] of fishCounts.entries()) {
          list.push({
            name: fName,
            kind: 'fish',
            count: cnt,
            legendary: LEGENDARY_SET.has(fName),
          });
        }
        list.sort((a, b) => Number(Boolean(b.legendary)) - Number(Boolean(a.legendary)) || b.count - a.count);
        setMaterialsList(list);
      })
      .catch(() => {});
  }, [open, materialsList.length]);

  const filteredMats = useMemo(() => {
    const q = query.trim().toLowerCase();
    return materialsList.filter((m) => {
      if (kindTab === 'legendary' && !m.legendary) return false;
      if (kindTab !== 'all' && kindTab !== 'legendary' && m.kind !== kindTab) return false;
      if (q && !m.name.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [materialsList, kindTab, query]);

  if (!open) return null;

  const toggleMat = (name: string) => {
    setSelectedMats((prev) => (prev.includes(name) ? prev.filter((item) => item !== name) : [...prev, name]));
  };

  const handleGenerate = async () => {
    if (mode === 'target' && selectedMats.length === 0) {
      toast.error('请至少选择一种目标材料');
      return;
    }
    setLoading(true);
    try {
      const res = await generateGingerbreadRoutes({
        mode,
        materials: mode === 'target' ? selectedMats : [],
        category: categorySweep,
        maps: mapId ? [mapId] : [],
        regions: mapId === 'miraland' && region ? [region] : [],
        top_k: topK,
        quota: quota > 0 ? quota : null,
        radius,
        prefix: `gb_custom_${Date.now().toString(36).slice(-4)}`,
      });
      if (!res.routes.length) {
        toast.error('所选区域内没有找到可覆盖该材料的流转之柱路线，请放宽地图或区域限制');
        return;
      }
      toast.success(`已结合 3D 地形生成 ${res.routes.length} 条最优姜饼人路线！`);
      onGenerated(res);
      onClose();
    } catch (error) {
      toast.error(String(error instanceof Error ? error.message : error));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs">
      <div className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-border bg-bg-secondary shadow-2xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-accent" />
            <div>
              <h2 className="text-sm font-semibold text-text-primary">3D 地形智能路线生成器（姜饼人 15m 光环）</h2>
              <p className="text-xs text-text-muted">结合解包 3D 海拔坡度、湖泊水体与美鸭梨全量刷新点，实时规划最优采集路线并在 3D 视口预览</p>
            </div>
          </div>
          <button onClick={onClose} className="rounded-lg p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5 text-xs">
          {/* Generation Mode */}
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => setMode('target')}
              className={clsx(
                'rounded-xl border p-3 text-left transition-colors',
                mode === 'target' ? 'border-accent bg-accent/10 text-text-primary' : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover',
              )}
            >
              <div className="font-semibold">指定材料按需速刷</div>
              <div className="mt-0.5 text-[11px] text-text-muted">任选 1 种或多种植物/昆虫/动物/鱼类，自动寻找最高密度的流转之柱并顺路兼收植物</div>
            </button>
            <button
              type="button"
              onClick={() => setMode('max-yield')}
              className={clsx(
                'rounded-xl border p-3 text-left transition-colors',
                mode === 'max-yield' ? 'border-accent bg-accent/10 text-text-primary' : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover',
              )}
            >
              <div className="font-semibold">区域全材料大丰收</div>
              <div className="mt-0.5 text-[11px] text-text-muted">不限单一材料，按单位时间收益最大化一趟横扫选定区域的全部采集物</div>
            </button>
          </div>

          {mode === 'target' ? (
            <div className="space-y-2.5 rounded-xl border border-border bg-bg-primary p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap gap-1">
                  {([
                    ['all', '全部'],
                    ['legendary', '传奇/稀有'],
                    ['plant', '植物'],
                    ['insect', '昆虫'],
                    ['animal', '动物'],
                    ['fish', '鱼类'],
                  ] as const).map(([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      onClick={() => setKindTab(id)}
                      className={clsx(
                        'rounded-lg px-2.5 py-1 text-xs transition-colors',
                        kindTab === id ? 'bg-accent text-white font-medium' : 'bg-bg-secondary text-text-secondary hover:bg-bg-hover',
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <div className="relative w-48">
                  <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-text-muted" />
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="搜索材料名称…"
                    className="w-full rounded-lg border border-border bg-bg-secondary py-1 pl-7 pr-2 text-xs outline-none focus:border-accent"
                  />
                </div>
              </div>

              {selectedMats.length > 0 && (
                <div className="flex flex-wrap items-center gap-1.5 pt-1">
                  <span className="text-text-muted">已选 ({selectedMats.length})：</span>
                  {selectedMats.map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => toggleMat(m)}
                      className="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2.5 py-0.5 text-xs font-medium text-accent hover:bg-accent/25"
                    >
                      {m}
                      <X className="h-3 w-3" />
                    </button>
                  ))}
                  <button type="button" onClick={() => setSelectedMats([])} className="ml-auto text-[11px] text-text-muted hover:text-text-primary">
                    清空
                  </button>
                </div>
              )}

              <div className="grid max-h-52 grid-cols-3 gap-1.5 overflow-y-auto pt-1 sm:grid-cols-4">
                {filteredMats.map((m) => {
                  const active = selectedMats.includes(m.name);
                  return (
                    <button
                      key={m.name}
                      type="button"
                      onClick={() => toggleMat(m.name)}
                      className={clsx(
                        'flex items-center justify-between rounded-lg border px-2.5 py-1.5 text-left transition-colors',
                        active ? 'border-accent bg-accent/15 font-medium text-accent' : 'border-border bg-bg-secondary text-text-secondary hover:bg-bg-hover',
                      )}
                    >
                      <span className="truncate">{m.name}</span>
                      <span className="ml-1 shrink-0 text-[10px] text-text-muted">{ active ? <Check className="h-3 w-3 text-accent" /> : m.count }</span>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : (
            <div className="space-y-2 rounded-xl border border-border bg-bg-primary p-3">
              <span className="block text-text-secondary">大丰收品类偏好</span>
              <div className="grid grid-cols-5 gap-1.5">
                {([
                  ['all', '全品类综合'],
                  ['plant', '仅植物零停顿'],
                  ['insect', '仅昆虫捕捉'],
                  ['animal', '仅动物清洁'],
                  ['fish', '仅鱼类垂钓'],
                ] as const).map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setCategorySweep(id)}
                    className={clsx(
                      'rounded-lg border px-2 py-2 text-center transition-colors',
                      categorySweep === id ? 'border-accent bg-accent/15 font-medium text-accent' : 'border-border bg-bg-secondary text-text-secondary hover:bg-bg-hover',
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Map, Region, Quota, TopK, Aura Radius */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <label className="block">
              <span className="mb-1 block text-text-secondary">限定地图</span>
              <select
                value={mapId}
                onChange={(e) => {
                  setMapId(e.target.value);
                  setRegion('');
                }}
                className="w-full rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
              >
                {MAP_OPTIONS.map(([id, label]) => (
                  <option key={id} value={id}>{label}</option>
                ))}
              </select>
            </label>

            {mapId === 'miraland' && (
              <label className="block">
                <span className="mb-1 block text-text-secondary">限定区域</span>
                <select
                  value={region}
                  onChange={(e) => setRegion(e.target.value)}
                  className="w-full rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
                >
                  {MIRALAND_REGIONS.map((r) => (
                    <option key={r} value={r}>{r || '全部区域自动优选'}</option>
                  ))}
                </select>
              </label>
            )}

            <label className="block">
              <span className="mb-1 block text-text-secondary">生成路线条数（1~10）</span>
              <input
                type="number"
                min={1}
                max={10}
                value={topK}
                onChange={(e) => setTopK(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
                className="w-full rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
              />
            </label>

            {mode === 'target' && (
              <label className="block">
                <span className="mb-1 block text-text-secondary">目标收集数量（0 为不限）</span>
                <input
                  type="number"
                  min={0}
                  max={500}
                  step={5}
                  value={quota}
                  onChange={(e) => setQuota(Math.max(0, Number(e.target.value) || 0))}
                  className="w-full rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
                />
              </label>
            )}

            <label className="block">
              <span className="mb-1 block text-text-secondary">姜饼人覆盖半径</span>
              <select
                value={radius}
                onChange={(e) => setRadius(Number(e.target.value))}
                className="w-full rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
              >
                <option value={13.5}>13.5 米（推荐稳妥）</option>
                <option value={14.2}>14.2 米（标准范围）</option>
                <option value={15.0}>15.0 米（极限边缘）</option>
              </select>
            </label>
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border bg-bg-primary px-5 py-3">
          <button type="button" onClick={onClose} className={secondaryButton}>
            取消
          </button>
          <button type="button" onClick={() => void handleGenerate()} disabled={loading} className={primaryButton}>
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            {loading ? '正在结合 3D 地形规划…' : '立即生成并在 3D 预览'}
          </button>
        </div>
      </div>
    </div>
  );
}
