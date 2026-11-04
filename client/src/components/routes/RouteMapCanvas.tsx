import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { actionMeta, type RoutePoint } from '@/services/routeDocument';

export interface MapInfo { id: string; title: string; width: number; height: number; image: string }
export interface ChestMarker { id: string; kind: string; map: string; x: number; y: number; label: string }
export interface MapCanvasHandle { fit: () => void; zoom: (factor: number) => void; focus: (x: number, y: number) => void }

const HIT_POINT = 10;
const HIT_SEGMENT = 7;

function cssColor(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#b4536e';
}

function distanceToSegment(px: number, py: number, a: { x: number; y: number }, b: { x: number; y: number }) {
  const dx = b.x-a.x;
  const dy = b.y-a.y;
  const t = Math.max(0, Math.min(1, ((px-a.x)*dx+(py-a.y)*dy)/(dx*dx+dy*dy || 1)));
  return Math.hypot(px-a.x-t*dx, py-a.y-t*dy);
}

/** The map view: drag to pan, wheel to zoom, points are dragged directly. */
export const RouteMapCanvas = forwardRef<MapCanvasHandle, {
  map: MapInfo | null;
  image: HTMLCanvasElement | null;
  points: RoutePoint[];
  preview?: RoutePoint[] | null;
  samples?: [number, number][] | null;
  pose?: { x: number; y: number; heading: number | null } | null;
  chests?: ChestMarker[];
  selected: number | null;
  tool: 'select' | 'add';
  readOnly: boolean;
  onSelect: (index: number | null) => void;
  /** Called while dragging; commit is true once on release. */
  onMove: (index: number, x: number, y: number, commit: boolean) => void;
  onInsert: (afterIndex: number, x: number, y: number) => void;
}>(function RouteMapCanvas({ map, image, points, preview, samples, pose, chests, selected, tool, readOnly, onSelect, onMove, onInsert }, ref) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const view = useRef({ scale: 1, ox: 0, oy: 0 });
  const drag = useRef<{ kind: 'pan' | 'point'; index: number; x: number; y: number; moved: boolean } | null>(null);
  const frame = useRef(0);
  const [cursor, setCursor] = useState<{ x: number; y: number; chest?: string } | null>(null);
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
      // The map is a grey matching reference; tint it to sit with the theme.
      context.filter = dark ? 'invert(0.86) hue-rotate(180deg) brightness(0.9)' : 'sepia(0.28) brightness(1.04)';
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
    if (chests?.length) {
      for (const chest of chests) {
        const cx = sx(chest.x);
        const cy = sy(chest.y);
        if (cx < -16 || cy < -16 || cx > width + 16 || cy > height + 16) continue;
        context.save();
        context.translate(cx, cy);
        context.rotate(Math.PI / 4);
        context.fillStyle = chest.kind === 'momo_chest' ? '#ec4899' : '#f59e0b';
        context.strokeStyle = '#ffffff';
        context.lineWidth = 1.5;
        context.beginPath();
        context.rect(-5, -5, 10, 10);
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
    const crowded = shown.length > 1 && scale < 0.5 && shown.length > 150;
    shown.forEach((point, index) => {
      const special = Boolean(point.action);
      const isSelected = index === selected && !preview;
      if (crowded && !special && !point.jump && !isSelected && index && index !== shown.length-1) return;
      const px = sx(point.x);
      const py = sy(point.y);
      if (isSelected) {
        context.beginPath();
        context.arc(px, py, Math.max(point.radius*scale, 13), 0, Math.PI*2);
        context.fillStyle = `${accent}26`;
        context.fill();
        context.strokeStyle = accent;
        context.lineWidth = 1.5;
        context.stroke();
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
  }, [map, image, shown, preview, samples, pose, chests, selected]);

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
  // Refit when a different map or route opens, not on every edit.
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
    // React's wheel listener is passive and cannot stop the page from scrolling.
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
  const hitChest = (x: number, y: number) => {
    if (!chests?.length) return null;
    const { scale, ox, oy } = view.current;
    let best: ChestMarker | null = null;
    let bestDist = 11;
    for (const chest of chests) {
      const d = Math.hypot(ox + chest.x * scale - x, oy + chest.y * scale - y);
      if (d <= bestDist) {
        bestDist = d;
        best = chest;
      }
    }
    return best;
  };
  const local = (event: React.PointerEvent | React.MouseEvent) => {
    const box = canvas.current!.getBoundingClientRect();
    return { x: event.clientX-box.left, y: event.clientY-box.top };
  };

  return <div className="relative h-full w-full overflow-hidden bg-bg-tertiary">
    <canvas ref={canvas} className="h-full w-full touch-none" style={{ cursor: tool === 'add' && !readOnly ? 'crosshair' : 'grab' }}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        const { x, y } = local(event);
        const index = preview ? -1 : hitPoint(x, y);
        canvas.current?.setPointerCapture(event.pointerId);
        drag.current = { kind: index >= 0 && !readOnly ? 'point' : 'pan', index, x, y, moved: false };
        if (index >= 0) onSelect(index);
      }}
      onPointerMove={(event) => {
        const { x, y } = local(event);
        const hoveredChest = hitChest(x, y);
        setCursor({ ...toMap(x, y), chest: hoveredChest?.label });
        const active = drag.current;
        if (!active) return;
        if (Math.hypot(x-active.x, y-active.y) < 3 && !active.moved) return;
        active.moved = true;
        if (active.kind === 'pan') {
          view.current.ox += x-active.x;
          view.current.oy += y-active.y;
          active.x = x;
          active.y = y;
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
        if (active.kind === 'point') {
          if (active.moved) onMove(active.index, position.x, position.y, true);
          return;
        }
        if (active.moved || active.index >= 0 || preview) return;
        if (tool === 'add' && !readOnly) {
          const chest = hitChest(x, y);
          const targetPos = chest ? { x: chest.x, y: chest.y } : position;
          const segment = hitSegment(x, y);
          onInsert(segment >= 0 ? segment : selected ?? points.length-1, targetPos.x, targetPos.y);
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
      {cursor.chest && <strong className="mr-2 font-medium text-accent">{cursor.chest}</strong>}
      {cursor.x.toFixed(1)}, {cursor.y.toFixed(1)}
    </span>}
  </div>;
});
