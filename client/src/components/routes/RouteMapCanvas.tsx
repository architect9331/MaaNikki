import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import {
  actionMeta, usesFacingHeading,
  type Checkpoint, type RoutePoint, type TrialWaypointReport,
} from '@/services/routeDocument';

export interface MapInfo { id: string; title: string; width: number; height: number; image: string }
export interface MapCanvasHandle { fit: () => void; zoom: (factor: number) => void; focus: (x: number, y: number) => void }

const HIT_POINT = 10;
const HIT_SEGMENT = 7;
const HEADING_HANDLE_DIST = 26;

const OUTCOME_COLOR: Record<string, string> = {
  turnback: '#f59e0b',
  recovered: '#f97316',
  action_missed: '#eab308',
  failed: '#ef4444',
};

function cssColor(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#b4536e';
}

function distanceToSegment(px: number, py: number, a: { x: number; y: number }, b: { x: number; y: number }) {
  const dx = b.x-a.x;
  const dy = b.y-a.y;
  const t = Math.max(0, Math.min(1, ((px-a.x)*dx+(py-a.y)*dy)/(dx*dx+dy*dy || 1)));
  return Math.hypot(px-a.x-t*dx, py-a.y-t*dy);
}

/** The map view: drag to pan, wheel to zoom, points and heading handles are dragged directly. */
export const RouteMapCanvas = forwardRef<MapCanvasHandle, {
  map: MapInfo | null;
  image: HTMLCanvasElement | null;
  points: RoutePoint[];
  preview?: RoutePoint[] | null;
  samples?: [number, number][] | null;
  pose?: { x: number; y: number; heading: number | null } | null;
  teleports?: Checkpoint[];
  startTeleport?: Checkpoint;
  selected: number | null;
  selectedSet?: ReadonlySet<number>;
  activeTrialIndex?: number | null;
  trialWaypoints?: TrialWaypointReport[] | null;
  tool: 'select' | 'add' | 'box';
  readOnly: boolean;
  onSelect: (index: number | null, toggle?: boolean) => void;
  onBoxSelect?: (indices: number[], additive: boolean) => void;
  /** Called while dragging; commit is true once on release. */
  onMove: (index: number, x: number, y: number, commit: boolean) => void;
  onHeadingChange?: (index: number, heading: number, commit: boolean) => void;
  onInsert: (afterIndex: number, x: number, y: number) => void;
  onPickTeleport?: (checkpoint: Checkpoint) => void;
}>(function RouteMapCanvas({
  map, image, points, preview, samples, pose, teleports, startTeleport,
  selected, selectedSet, activeTrialIndex, trialWaypoints,
  tool, readOnly, onSelect, onBoxSelect, onMove, onHeadingChange, onInsert, onPickTeleport,
}, ref) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const view = useRef({ scale: 1, ox: 0, oy: 0 });
  const drag = useRef<{
    kind: 'pan' | 'point' | 'heading' | 'box';
    index: number;
    x: number;
    y: number;
    curX: number;
    curY: number;
    moved: boolean;
    shift: boolean;
  } | null>(null);
  const frame = useRef(0);
  const [cursor, setCursor] = useState<{ x: number; y: number; teleport?: Checkpoint } | null>(null);
  const shown = preview ?? points;

  const draw = useCallback(() => {
    const element = canvas.current;
    const context = element?.getContext('2d');
    if (!element || !context) return;
    const ratio = window.devicePixelRatio || 1;
    const width = element.clientWidth;
    const height = element.clientHeight;
    if (element.width !== Math.round(width*ratio) || element.height !== Math.round(height*ratio)) {
      element.width = Math.round(width*ratio);
      element.height = Math.round(height*ratio);
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const { scale, ox, oy } = view.current;
    const sx = (x: number) => ox+x*scale;
    const sy = (y: number) => oy+y*scale;
    const dark = document.documentElement.classList.contains('dark');
    if (map && image) {
      const isColor = Boolean((image as HTMLCanvasElement & { __isColorMap?: boolean }).__isColorMap);
      if (!isColor) {
        context.filter = dark ? 'invert(0.86) hue-rotate(180deg) brightness(0.9)' : 'sepia(0.28) brightness(1.04)';
      }
      context.drawImage(image, ox, oy, map.width*scale, map.height*scale);
      context.filter = 'none';
    }
    const accent = cssColor('--color-accent');
    if (samples?.length) {
      context.beginPath();
      samples.forEach(([x, y], index) => index ? context.lineTo(sx(x), sy(y)) : context.moveTo(sx(x), sy(y)));
      context.strokeStyle = `${accent}66`;
      context.lineWidth = 2;
      context.stroke();
    }
    // Teleporter markers layer.
    if (teleports?.length) {
      for (const tp of teleports) {
        const cx = sx(tp.x);
        const cy = sy(tp.y);
        if (cx < -18 || cy < -18 || cx > width + 18 || cy > height + 18) continue;
        const isStart = startTeleport?.name === tp.name;
        if (isStart) {
          context.beginPath();
          context.arc(cx, cy, 12, 0, Math.PI * 2);
          context.fillStyle = '#7c6cf033';
          context.fill();
          context.strokeStyle = '#7c6cf0';
          context.lineWidth = 1.8;
          context.stroke();
        }
        context.save();
        context.translate(cx, cy);
        context.rotate(Math.PI / 4);
        context.fillStyle = isStart ? '#6353e8' : '#7c6cf0cc';
        context.strokeStyle = '#ffffff';
        context.lineWidth = 1.5;
        const sz = isStart ? 6.5 : 5;
        context.beginPath();
        context.rect(-sz, -sz, sz * 2, sz * 2);
        context.fill();
        context.stroke();
        context.restore();
      }
    }
    // Line first, so points stay readable where the route crosses itself.
    for (let index = 1; index < shown.length; index++) {
      const a = shown[index-1];
      const b = shown[index];
      const teleport = b.action === 'teleport';
      context.beginPath();
      context.moveTo(sx(a.x), sy(a.y));
      context.lineTo(sx(b.x), sy(b.y));
      context.setLineDash(teleport ? [5, 6] : []);
      context.lineWidth = teleport ? 1.5 : 3;
      context.strokeStyle = teleport ? '#7c6cf0' : accent;
      context.globalAlpha = preview ? 0.85 : 1;
      context.stroke();
      context.setLineDash([]);
      context.globalAlpha = 1;
      const length = Math.hypot(sx(b.x)-sx(a.x), sy(b.y)-sy(a.y));
      if (!teleport && length > 46) {
        const mx = (sx(a.x)+sx(b.x))/2;
        const my = (sy(a.y)+sy(b.y))/2;
        const angle = Math.atan2(sy(b.y)-sy(a.y), sx(b.x)-sx(a.x));
        context.save();
        context.translate(mx, my);
        context.rotate(angle);
        context.beginPath();
        context.moveTo(5, 0);
        context.lineTo(-4, -4.5);
        context.lineTo(-4, 4.5);
        context.closePath();
        context.fillStyle = '#fff';
        context.fill();
        context.restore();
      }
    }

    const wpReportByIndex = new Map<number, TrialWaypointReport>();
    if (trialWaypoints?.length && !preview) {
      for (const wp of trialWaypoints) wpReportByIndex.set(wp.index, wp);
    }
    const crowded = shown.length > 1 && scale < 0.5 && shown.length > 150;
    shown.forEach((point, index) => {
      const special = Boolean(point.action);
      const isSelected = !preview && (index === selected || Boolean(selectedSet?.has(index)));
      const isTrialTarget = !preview && index === activeTrialIndex;
      const wpReport = wpReportByIndex.get(index);
      const hasProblem = Boolean(wpReport && wpReport.outcome !== 'clean');
      if (crowded && !special && !point.jump && !isSelected && !isTrialTarget && !hasProblem && index && index !== shown.length-1) return;
      const px = sx(point.x);
      const py = sy(point.y);
      if (wpReport && wpReport.actual && wpReport.outcome !== 'clean') {
        const ax = sx(wpReport.actual[0]);
        const ay = sy(wpReport.actual[1]);
        const col = OUTCOME_COLOR[wpReport.outcome] ?? '#f59e0b';
        context.beginPath();
        context.setLineDash([3, 3]);
        context.moveTo(px, py);
        context.lineTo(ax, ay);
        context.strokeStyle = col;
        context.lineWidth = 1.5;
        context.stroke();
        context.setLineDash([]);
        context.beginPath();
        context.arc(ax, ay, 4, 0, Math.PI * 2);
        context.fillStyle = col;
        context.fill();
      }
      if (isSelected || isTrialTarget || hasProblem) {
        const ringColor = isTrialTarget ? '#06b6d4' : hasProblem ? (OUTCOME_COLOR[wpReport!.outcome] ?? accent) : accent;
        context.beginPath();
        context.arc(px, py, Math.max(point.radius*scale, isTrialTarget ? 16 : 13), 0, Math.PI*2);
        context.fillStyle = `${ringColor}26`;
        context.fill();
        context.strokeStyle = ringColor;
        context.lineWidth = isTrialTarget ? 2.2 : 1.5;
        context.stroke();
      }
      // Draw facing_heading arrow for fishing / zhaoxi_attack / attack key.
      const hasHeading = usesFacingHeading(point) && (typeof point.params?.facing_heading === 'number' || (index === selected && !preview));
      if (hasHeading) {
        const deg = typeof point.params?.facing_heading === 'number' ? Number(point.params.facing_heading) : 0;
        const rad = (deg * Math.PI) / 180;
        const tx = px + Math.sin(rad) * HEADING_HANDLE_DIST;
        const ty = py - Math.cos(rad) * HEADING_HANDLE_DIST;
        const arrowColor = point.action === 'fishing' ? '#2f8fc4' : '#d0567a';
        context.beginPath();
        context.moveTo(px, py);
        context.lineTo(tx, ty);
        context.strokeStyle = arrowColor;
        context.lineWidth = 2.2;
        context.stroke();
        context.save();
        context.translate(tx, ty);
        context.rotate(rad);
        context.beginPath();
        context.moveTo(0, -4);
        context.lineTo(4.5, 4);
        context.lineTo(-4.5, 4);
        context.closePath();
        context.fillStyle = arrowColor;
        context.fill();
        context.restore();
        if (index === selected && !preview && !readOnly) {
          context.beginPath();
          context.arc(tx, ty, 5.5, 0, Math.PI * 2);
          context.fillStyle = '#ffffff';
          context.fill();
          context.strokeStyle = arrowColor;
          context.lineWidth = 2;
          context.stroke();
        }
      }
      const size = special ? 9 : index === 0 || index === shown.length-1 ? 7 : point.jump ? 6 : 4;
      context.beginPath();
      context.arc(px, py, size, 0, Math.PI*2);
      context.fillStyle = special ? actionMeta(point.action).color : point.jump ? '#e8a33d' : '#fff';
      context.fill();
      context.lineWidth = 2;
      context.strokeStyle = special || point.jump ? '#fff' : accent;
      context.stroke();
      if (special) {
        context.fillStyle = '#fff';
        context.font = '600 10px system-ui, sans-serif';
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(actionMeta(point.action).label.slice(0, 1), px, py+0.5);
      }
      if (index === 0 && !special) {
        context.fillStyle = accent;
        context.font = '600 11px system-ui, sans-serif';
        context.textAlign = 'left';
        context.textBaseline = 'middle';
        context.fillText('起点', px+11, py);
      }
    });

    if (pose) {
      const px = sx(pose.x);
      const py = sy(pose.y);
      context.save();
      context.translate(px, py);
      context.rotate(((pose.heading ?? 0)*Math.PI)/180);
      context.beginPath();
      context.moveTo(0, -11);
      context.lineTo(7.5, 8);
      context.lineTo(0, 4);
      context.lineTo(-7.5, 8);
      context.closePath();
      context.fillStyle = '#e5484d';
      context.strokeStyle = '#fff';
      context.lineWidth = 2;
      context.fill();
      context.stroke();
      context.restore();
    }
    if (drag.current?.kind === 'box' && drag.current.moved) {
      const { x, y, curX, curY } = drag.current;
      const rx = Math.min(x, curX);
      const ry = Math.min(y, curY);
      const rw = Math.abs(curX - x);
      const rh = Math.abs(curY - y);
      context.fillStyle = `${accent}1f`;
      context.fillRect(rx, ry, rw, rh);
      context.strokeStyle = accent;
      context.lineWidth = 1.5;
      context.setLineDash([4, 3]);
      context.strokeRect(rx, ry, rw, rh);
      context.setLineDash([]);
    }
  }, [map, image, shown, preview, samples, pose, teleports, startTeleport, selected, selectedSet, activeTrialIndex, trialWaypoints, readOnly]);

  const schedule = useCallback(() => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(draw);
  }, [draw]);

  const fit = useCallback(() => {
    const element = canvas.current;
    if (!element || !map) return;
    const list = shown.length ? shown : null;
    const left = list ? Math.min(...list.map((p) => p.x)) : 0;
    const right = list ? Math.max(...list.map((p) => p.x)) : map.width;
    const top = list ? Math.min(...list.map((p) => p.y)) : 0;
    const bottom = list ? Math.max(...list.map((p) => p.y)) : map.height;
    const padding = 70;
    const scale = Math.min((element.clientWidth-padding*2)/Math.max(right-left, 60), (element.clientHeight-padding*2)/Math.max(bottom-top, 60));
    view.current = { scale, ox: element.clientWidth/2-((left+right)/2)*scale, oy: element.clientHeight/2-((top+bottom)/2)*scale };
    schedule();
  }, [map, shown, schedule]);

  const zoomAt = useCallback((factor: number, x: number, y: number) => {
    const { scale, ox, oy } = view.current;
    const next = Math.max(0.02, Math.min(40, scale*factor));
    view.current = { scale: next, ox: x-(x-ox)*(next/scale), oy: y-(y-oy)*(next/scale) };
    schedule();
  }, [schedule]);

  useImperativeHandle(ref, () => ({
    fit,
    zoom: (factor) => {
      const element = canvas.current;
      if (element) zoomAt(factor, element.clientWidth/2, element.clientHeight/2);
    },
    focus: (x, y) => {
      const element = canvas.current;
      if (!element) return;
      const { scale } = view.current;
      view.current = { scale, ox: element.clientWidth/2-x*scale, oy: element.clientHeight/2-y*scale };
      schedule();
    },
  }), [fit, zoomAt, schedule]);

  useEffect(schedule, [schedule]);
  const fitKey = `${map?.id}:${image ? 1 : 0}`;
  useEffect(() => { fit(); }, [fitKey]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const observer = new ResizeObserver(schedule);
    observer.observe(element);
    return () => observer.disconnect();
  }, [schedule]);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const box = element.getBoundingClientRect();
      zoomAt(event.deltaY < 0 ? 1.18 : 1/1.18, event.clientX-box.left, event.clientY-box.top);
    };
    element.addEventListener('wheel', wheel, { passive: false });
    return () => element.removeEventListener('wheel', wheel);
  }, [zoomAt]);

  const hitPoint = (x: number, y: number) => {
    const { scale, ox, oy } = view.current;
    let best = -1;
    let distance = HIT_POINT;
    points.forEach((point, index) => {
      const current = Math.hypot(ox+point.x*scale-x, oy+point.y*scale-y);
      if (current <= distance) {
        distance = current;
        best = index;
      }
    });
    return best;
  };
  const hitHeadingHandle = (x: number, y: number) => {
    if (selected === null || readOnly || preview) return -1;
    const point = points[selected];
    if (!point || !usesFacingHeading(point)) return -1;
    const { scale, ox, oy } = view.current;
    const px = ox + point.x * scale;
    const py = oy + point.y * scale;
    const deg = typeof point.params?.facing_heading === 'number' ? Number(point.params.facing_heading) : 0;
    const rad = (deg * Math.PI) / 180;
    const tx = px + Math.sin(rad) * HEADING_HANDLE_DIST;
    const ty = py - Math.cos(rad) * HEADING_HANDLE_DIST;
    return Math.hypot(x - tx, y - ty) <= 9 ? selected : -1;
  };
  const hitSegment = (x: number, y: number) => {
    const { scale, ox, oy } = view.current;
    for (let index = 1; index < points.length; index++) {
      const a = { x: ox+points[index-1].x*scale, y: oy+points[index-1].y*scale };
      const b = { x: ox+points[index].x*scale, y: oy+points[index].y*scale };
      if (distanceToSegment(x, y, a, b) <= HIT_SEGMENT) return index-1;
    }
    return -1;
  };
  const toMap = (x: number, y: number) => {
    const { scale, ox, oy } = view.current;
    return { x: Math.round(((x-ox)/scale)*100)/100, y: Math.round(((y-oy)/scale)*100)/100 };
  };
  const hitTeleport = (x: number, y: number) => {
    if (!teleports?.length) return null;
    const { scale, ox, oy } = view.current;
    let best: Checkpoint | null = null;
    let bestDist = 11;
    for (const tp of teleports) {
      const d = Math.hypot(ox + tp.x * scale - x, oy + tp.y * scale - y);
      if (d <= bestDist) {
        bestDist = d;
        best = tp;
      }
    }
    return best;
  };
  const headingFromScreen = (index: number, x: number, y: number) => {
    const point = points[index];
    if (!point) return 0;
    const { scale, ox, oy } = view.current;
    const px = ox + point.x * scale;
    const py = oy + point.y * scale;
    const deg = (Math.atan2(x - px, -(y - py)) * 180) / Math.PI;
    return Math.round(((deg % 360) + 360) % 360);
  };
  const local = (event: React.PointerEvent | React.MouseEvent) => {
    const box = canvas.current!.getBoundingClientRect();
    return { x: event.clientX-box.left, y: event.clientY-box.top };
  };

  return <div className="relative h-full w-full overflow-hidden bg-bg-tertiary">
    <canvas ref={canvas} className="h-full w-full touch-none" style={{ cursor: (tool === 'add' || tool === 'box') && !readOnly ? 'crosshair' : 'grab' }}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        const { x, y } = local(event);
        const hIdx = hitHeadingHandle(x, y);
        canvas.current?.setPointerCapture(event.pointerId);
        if (hIdx >= 0) {
          drag.current = { kind: 'heading', index: hIdx, x, y, curX: x, curY: y, moved: false, shift: event.shiftKey };
          return;
        }
        const index = preview ? -1 : hitPoint(x, y);
        if (index >= 0) {
          drag.current = { kind: !readOnly ? 'point' : 'pan', index, x, y, curX: x, curY: y, moved: false, shift: event.shiftKey };
          onSelect(index, event.shiftKey);
          return;
        }
        if ((tool === 'box' || event.shiftKey) && !preview) {
          drag.current = { kind: 'box', index: -1, x, y, curX: x, curY: y, moved: false, shift: event.shiftKey };
          return;
        }
        drag.current = { kind: 'pan', index: -1, x, y, curX: x, curY: y, moved: false, shift: event.shiftKey };
      }}
      onPointerMove={(event) => {
        const { x, y } = local(event);
        const hoveredTp = hitTeleport(x, y);
        setCursor({ ...toMap(x, y), teleport: hoveredTp ?? undefined });
        const active = drag.current;
        if (!active) return;
        if (Math.hypot(x-active.x, y-active.y) < 3 && !active.moved) return;
        active.moved = true;
        active.curX = x;
        active.curY = y;
        if (active.kind === 'pan') {
          view.current.ox += x-active.x;
          view.current.oy += y-active.y;
          active.x = x;
          active.y = y;
          schedule();
        } else if (active.kind === 'heading') {
          onHeadingChange?.(active.index, headingFromScreen(active.index, x, y), false);
        } else if (active.kind === 'box') {
          schedule();
        } else {
          const position = toMap(x, y);
          onMove(active.index, position.x, position.y, false);
        }
      }}
      onPointerUp={(event) => {
        const active = drag.current;
        drag.current = null;
        if (!active) return;
        const { x, y } = local(event);
        const position = toMap(x, y);
        if (active.kind === 'heading') {
          if (active.moved) onHeadingChange?.(active.index, headingFromScreen(active.index, x, y), true);
          return;
        }
        if (active.kind === 'box') {
          schedule();
          if (active.moved && onBoxSelect) {
            const { scale, ox, oy } = view.current;
            const x0 = Math.min(active.x, x), x1 = Math.max(active.x, x);
            const y0 = Math.min(active.y, y), y1 = Math.max(active.y, y);
            const matched: number[] = [];
            points.forEach((pt, idx) => {
              const px = ox + pt.x * scale;
              const py = oy + pt.y * scale;
              if (px >= x0 && px <= x1 && py >= y0 && py <= y1) matched.push(idx);
            });
            onBoxSelect(matched, active.shift);
          }
          return;
        }
        if (active.kind === 'point') {
          if (active.moved) onMove(active.index, position.x, position.y, true);
          return;
        }
        if (active.moved || active.index >= 0 || preview) return;
        const tp = hitTeleport(x, y);
        if (tp && onPickTeleport) {
          onPickTeleport(tp);
          return;
        }
        if (tool === 'add' && !readOnly) {
          const segment = hitSegment(x, y);
          onInsert(segment >= 0 ? segment : selected ?? points.length-1, position.x, position.y);
        } else {
          onSelect(null);
        }
      }}
      onDoubleClick={(event) => {
        if (readOnly || preview || tool === 'add') return;
        const { x, y } = local(event);
        const segment = hitSegment(x, y);
        if (segment >= 0 && hitPoint(x, y) < 0) {
          const position = toMap(x, y);
          onInsert(segment, position.x, position.y);
        }
      }}
      onPointerLeave={() => setCursor(null)} />
    {cursor && <span className="pointer-events-none absolute bottom-2 right-3 rounded bg-bg-secondary/90 px-2 py-0.5 text-[11px] tabular-nums text-text-muted">
      {cursor.teleport && <strong className="mr-2 font-medium text-[#7c6cf0]">流转之柱：{cursor.teleport.region} · {cursor.teleport.name}</strong>}
      {cursor.x.toFixed(1)}, {cursor.y.toFixed(1)}
    </span>}
  </div>;
});
