import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, Mountain } from 'lucide-react';
import { actionMeta, type RoutePoint } from '@/services/routeDocument';
import type { ElevationProfile, ProfileSample } from './routeTerrain';

const PAD_L = 40;
const PAD_R = 12;
const PAD_T = 10;
const PAD_B = 16;

function cssColor(name: string, fallback: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

/** Steeper ground shades from green to red; purely visual, no pass/fail threshold. */
function slopeColor(slope: number) {
  const t = Math.max(0, Math.min(1, slope / 60));
  return `hsl(${Math.round(140 - 140 * t)}, 70%, 48%)`;
}

/**
 * Terrain height along the route, shown under the map. It answers the questions the free 3D
 * camera was meant to answer (where does the path climb, drop off a ledge or need a jump)
 * without leaving the top-down editing view: hover to locate the spot on the map, click to
 * select the nearest point.
 */
export function RouteElevationProfile({
  profile, status, points, selected, selectedSet, activeTrialIndex, collapsed, onToggle, onSelect, onProbe,
}: {
  profile: ElevationProfile | null;
  status: 'idle' | 'loading' | 'missing';
  points: RoutePoint[];
  selected: number | null;
  selectedSet: ReadonlySet<number>;
  activeTrialIndex: number | null;
  collapsed: boolean;
  onToggle: () => void;
  onSelect: (index: number) => void;
  onProbe: (spot: { x: number; y: number } | null) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<ProfileSample | null>(null);

  const layout = useCallback(() => {
    const el = canvas.current;
    if (!el || !profile) return null;
    const w = el.clientWidth;
    const h = el.clientHeight;
    const span = Math.max(4, profile.zMax - profile.zMin);
    const z0 = profile.zMin - span * 0.08;
    const z1 = profile.zMax + span * 0.08;
    const len = Math.max(1, profile.length);
    return {
      w, h,
      px: (d: number) => PAD_L + (d / len) * (w - PAD_L - PAD_R),
      py: (z: number) => PAD_T + (1 - (z - z0) / (z1 - z0)) * (h - PAD_T - PAD_B),
      dAt: (x: number) => Math.max(0, Math.min(len, ((x - PAD_L) / Math.max(1, w - PAD_L - PAD_R)) * len)),
    };
  }, [profile]);

  const draw = useCallback(() => {
    const el = canvas.current;
    const ctx = el?.getContext('2d');
    const geo = layout();
    if (!el || !ctx || !geo || !profile) return;
    const ratio = window.devicePixelRatio || 1;
    if (el.width !== Math.round(geo.w * ratio) || el.height !== Math.round(geo.h * ratio)) {
      el.width = Math.round(geo.w * ratio);
      el.height = Math.round(geo.h * ratio);
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, geo.w, geo.h);
    const accent = cssColor('--color-accent', '#b4536e');
    const muted = cssColor('--color-text-muted', '#94a3b8');
    const bottom = geo.h - PAD_B;

    // Height grid lines with labels.
    ctx.font = '10px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let k = 0; k <= 2; k++) {
      const z = profile.zMin + ((profile.zMax - profile.zMin) * k) / 2;
      const y = geo.py(z);
      ctx.strokeStyle = `${muted}33`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD_L, y);
      ctx.lineTo(geo.w - PAD_R, y);
      ctx.stroke();
      ctx.fillStyle = muted;
      ctx.fillText(z.toFixed(0), PAD_L - 5, y);
    }

    // Filled terrain area, split at gaps (teleports, unexplored ground).
    const runs: ProfileSample[][] = [];
    let run: ProfileSample[] = [];
    for (const s of profile.samples) {
      if (s.valid) run.push(s);
      else if (run.length) { runs.push(run); run = []; }
    }
    if (run.length) runs.push(run);
    for (const r of runs) {
      ctx.beginPath();
      ctx.moveTo(geo.px(r[0].d), bottom);
      for (const s of r) ctx.lineTo(geo.px(s.d), geo.py(s.z));
      ctx.lineTo(geo.px(r[r.length - 1].d), bottom);
      ctx.closePath();
      ctx.fillStyle = `${accent}1a`;
      ctx.fill();
      ctx.lineWidth = 2;
      for (let i = 1; i < r.length; i++) {
        ctx.beginPath();
        ctx.moveTo(geo.px(r[i - 1].d), geo.py(r[i - 1].z));
        ctx.lineTo(geo.px(r[i].d), geo.py(r[i].z));
        ctx.strokeStyle = slopeColor(Math.max(r[i - 1].slope, r[i].slope));
        ctx.stroke();
      }
    }

    // Teleport legs are not walked.
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = '#7c6cf0';
    ctx.lineWidth = 1.2;
    for (const d of profile.breaks) {
      ctx.beginPath();
      ctx.moveTo(geo.px(d), PAD_T);
      ctx.lineTo(geo.px(d), bottom);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // Waypoints: action colours, jumps in orange, selection with a guide line.
    for (const m of profile.markers) {
      const pt = points[m.index];
      if (!pt) continue;
      const isSel = m.index === selected || selectedSet.has(m.index);
      const isTrial = m.index === activeTrialIndex;
      const x = geo.px(m.d);
      const y = m.valid ? geo.py(m.z) : bottom - 4;
      if (isSel || isTrial) {
        ctx.strokeStyle = isTrial ? '#06b6d4' : accent;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, PAD_T);
        ctx.lineTo(x, bottom);
        ctx.stroke();
      }
      if (!pt.action && !pt.jump && !isSel && !isTrial && profile.markers.length > 60) continue;
      const r = isSel ? 5 : pt.action ? 4 : 2.5;
      ctx.beginPath();
      if (pt.jump && !pt.action) {
        ctx.moveTo(x, y - r - 1.5);
        ctx.lineTo(x + r + 1, y + r);
        ctx.lineTo(x - r - 1, y + r);
        ctx.closePath();
      } else {
        ctx.arc(x, y, r, 0, Math.PI * 2);
      }
      ctx.fillStyle = pt.action ? actionMeta(pt.action).color : pt.jump ? '#e8a33d' : '#ffffff';
      ctx.fill();
      ctx.strokeStyle = isTrial ? '#06b6d4' : isSel ? accent : pt.action || pt.jump ? '#ffffff' : accent;
      ctx.lineWidth = isSel || isTrial ? 2 : 1.2;
      ctx.stroke();
    }

    if (hover) {
      const x = geo.px(hover.d);
      ctx.strokeStyle = '#06b6d4';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, PAD_T);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      if (hover.valid) {
        ctx.beginPath();
        ctx.arc(x, geo.py(hover.z), 3.5, 0, Math.PI * 2);
        ctx.fillStyle = '#06b6d4';
        ctx.fill();
      }
    }
  }, [layout, profile, points, selected, selectedSet, activeTrialIndex, hover]);

  useEffect(() => {
    const id = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(id);
  }, [draw, collapsed]);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const observer = new ResizeObserver(() => draw());
    observer.observe(el);
    return () => observer.disconnect();
  }, [draw, collapsed]);

  const sampleAt = (clientX: number) => {
    const el = canvas.current;
    const geo = layout();
    if (!el || !geo || !profile?.samples.length) return null;
    const d = geo.dAt(clientX - el.getBoundingClientRect().left);
    let best = profile.samples[0];
    for (const s of profile.samples) if (Math.abs(s.d - d) < Math.abs(best.d - d)) best = s;
    return best;
  };

  const hoverPoint = hover ? hover.segment : null;
  return <section className="shrink-0 border-t border-border bg-bg-secondary">
    <div className="flex items-center gap-3 px-3 py-1.5 text-xs text-text-secondary">
      <button type="button" onClick={onToggle} className="flex items-center gap-1.5 font-medium text-text-primary hover:text-accent">
        <Mountain className="h-3.5 w-3.5" />高度剖面
        {collapsed ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
      </button>
      {status === 'loading' && <span className="text-text-muted">正在读取地形…</span>}
      {status === 'missing' && <span className="text-text-muted">这张地图暂无地形高度数据</span>}
      {status === 'idle' && !profile && <span className="text-text-muted">路线至少需要一个点</span>}
      {profile && <>
        <span className="tabular-nums">高度 {profile.zMin.toFixed(0)} ~ {profile.zMax.toFixed(0)}</span>
        <span className="tabular-nums">累计爬升 {profile.ascent.toFixed(0)} · 下降 {profile.descent.toFixed(0)}</span>
        {hover && <span className="ml-auto tabular-nums text-[#06b6d4]">
          {hover.valid ? `高度 ${hover.z.toFixed(1)} · 坡度 ${hover.slope.toFixed(0)}°` : '无地形数据'}
          {hoverPoint !== null && hoverPoint > 0 && ` · 第 ${hoverPoint} → ${hoverPoint + 1} 点之间`}
        </span>}
        {!hover && <span className="ml-auto text-text-muted">悬停在地图上定位 · 点击选中最近的点</span>}
      </>}
    </div>
    {!collapsed && profile && <canvas ref={canvas} className="block h-28 w-full cursor-crosshair"
      onPointerMove={(event) => {
        const s = sampleAt(event.clientX);
        setHover(s);
        onProbe(s ? { x: s.x, y: s.y } : null);
      }}
      onPointerLeave={() => { setHover(null); onProbe(null); }}
      onClick={(event) => {
        const s = sampleAt(event.clientX);
        if (!s || !profile) return;
        let best = profile.markers[0];
        for (const m of profile.markers) if (Math.abs(m.d - s.d) < Math.abs(best.d - s.d)) best = m;
        if (best) onSelect(best.index);
      }} />}
  </section>;
}
