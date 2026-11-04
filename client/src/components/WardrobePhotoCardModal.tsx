import { useEffect, useRef, useState } from 'react';
import { Camera, Check, ClipboardCopy, Download, FolderOpen, Image as ImageIcon, X } from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import { toast } from 'sonner';
import { isTauri } from '@/utils/paths';
import type { Match, OutfitResult, ScoringCatalog } from '@/services/scoring/types';
import { ModalPortal, useModalFocusTrap } from './ui/Modal';
import { primaryButton, secondaryButton } from './ui/FeaturePage';

interface GamePhotoItem {
  path: string;
  filename: string;
  uid: string;
  source: string;
  modified: number;
}

const levelLabel = (level: number | null) => level === null ? '等级未知' : level === 11 ? '已焕新' : `${level} 级`;
const num = (v: number) => v.toLocaleString('zh-CN', { maximumFractionDigits: 0 });

export function WardrobePhotoCardModal({ open, onClose, outfit, match, catalog, modeLabel, customHeadline }: {
  open: boolean;
  onClose: () => void;
  outfit: OutfitResult;
  match: Match;
  catalog: ScoringCatalog;
  modeLabel: string;
  customHeadline?: string;
}) {
  const [photos, setPhotos] = useState<GamePhotoItem[]>([]);
  const [selectedPath, setSelectedPath] = useState('');
  const [photoImg, setPhotoImg] = useState<HTMLImageElement | null>(null);
  const [note, setNote] = useState('');
  const [copied, setCopied] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const initializedRef = useRef(false);

  useModalFocusTrap({ active: open, containerRef: panelRef, onEscape: onClose });

  useEffect(() => {
    if (!open || !isTauri()) return;
    let cancelled = false;
    void invoke<{ photos: GamePhotoItem[] }>('list_game_photos')
      .then((res) => {
        if (cancelled) return;
        const list = res.photos ?? [];
        setPhotos(list);
        if (!initializedRef.current) {
          initializedRef.current = true;
          if (list[0]) setSelectedPath(list[0].path);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [open]);

  useEffect(() => {
    if (!selectedPath || !isTauri()) return;
    let cancelled = false;
    void invoke<string>('read_game_photo_base64', { path: selectedPath })
      .then((dataUrl) => {
        if (cancelled) return;
        const img = new Image();
        img.onload = () => { if (!cancelled) setPhotoImg(img); };
        img.src = dataUrl;
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [selectedPath]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!open || !canvas || !ctx) return;
    const W = 1080;
    const rows = outfit.pieces.length;
    const H = Math.max(640, 220 + rows * 44 + 70);
    canvas.width = W;
    canvas.height = H;

    const grad = ctx.createLinearGradient(0, 0, W, H);
    grad.addColorStop(0, '#fff4f7');
    grad.addColorStop(1, '#eef2ff');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, H);

    const font = '"Microsoft YaHei", "PingFang SC", sans-serif';
    const leftW = photoImg ? 420 : 0;

    if (photoImg) {
      ctx.save();
      ctx.beginPath();
      ctx.roundRect(36, 36, 384, H - 72, 18);
      ctx.clip();
      const scale = Math.max(384 / photoImg.naturalWidth, (H - 72) / photoImg.naturalHeight);
      const dw = photoImg.naturalWidth * scale;
      const dh = photoImg.naturalHeight * scale;
      ctx.drawImage(photoImg, 36 + (384 - dw) / 2, 36 + (H - 72 - dh) / 2, dw, dh);
      ctx.restore();
    }

    const rx = 48 + leftW;
    const rw = W - rx - 40;
    ctx.fillStyle = '#7c6573';
    ctx.font = `18px ${font}`;
    ctx.fillText(`${match.group} · ${modeLabel}`, rx, 66);

    ctx.fillStyle = '#362731';
    ctx.font = `bold 30px ${font}`;
    ctx.fillText(match.name, rx, 108);

    ctx.fillStyle = '#e06388';
    ctx.font = `bold 42px ${font}`;
    ctx.fillText(customHeadline ?? `估分 ${num(outfit.score)}`, rx, 164);

    if (note.trim()) {
      ctx.fillStyle = '#6b5563';
      ctx.font = `16px ${font}`;
      ctx.fillText(note.trim(), rx + 260, 158);
    }

    outfit.pieces.forEach((piece, idx) => {
      const y = 192 + idx * 42;
      ctx.fillStyle = idx % 2 === 0 ? 'rgba(255,255,255,0.78)' : 'rgba(255,255,255,0.45)';
      ctx.beginPath();
      ctx.roundRect(rx, y, rw, 36, 8);
      ctx.fill();

      const part = (catalog.part_names[piece.type] ?? String(piece.type)).split(' / ').pop() ?? '';
      ctx.fillStyle = '#8b7a85';
      ctx.font = `15px ${font}`;
      ctx.fillText(part, rx + 14, y + 23);

      ctx.fillStyle = '#362731';
      ctx.font = `bold 16px ${font}`;
      ctx.fillText(piece.item.name, rx + 96, y + 23);

      ctx.fillStyle = '#7c6573';
      ctx.font = `14px ${font}`;
      ctx.fillText(levelLabel(piece.level), rx + rw - 155, y + 23);

      ctx.fillStyle = customHeadline && piece.score === 0 ? '#9ca3af' : '#e06388';
      ctx.font = `bold 16px ${font}`;
      ctx.textAlign = 'right';
      ctx.fillText(customHeadline ? (piece.score > 0 ? '已拥有' : '未获得') : num(piece.score), rx + rw - 14, y + 23);
      ctx.textAlign = 'left';
    });

    ctx.fillStyle = '#8b7a85';
    ctx.font = `16px ${font}`;
    ctx.fillText('MaaNikki · 搭配与相册分享卡', rx, H - 28);
  }, [open, outfit, match, catalog, modeLabel, customHeadline, photoImg, note]);

  function onPickLocalFile(file?: File) {
    if (!file) return;
    initializedRef.current = true;
    setSelectedPath('');
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => { setPhotoImg(img); };
      img.src = String(reader.result);
    };
    reader.readAsDataURL(file);
  }

  async function copyCard() {
    const canvas = canvasRef.current;
    if (!canvas) return;
    try {
      const blob = await new Promise<Blob>((resolve, reject) =>
        canvas.toBlob((b) => b ? resolve(b) : reject(new Error('生成失败')), 'image/png'));
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
      toast.success('穿搭分享卡已复制，可直接粘贴分享');
    } catch {
      toast.error('复制分享卡失败');
    }
  }

  function downloadCard() {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const link = document.createElement('a');
    link.download = `MaaNikki-穿搭分享-${match.name}.png`;
    link.href = canvas.toDataURL('image/png');
    link.click();
  }

  if (!open) return null;

  return (
    <ModalPortal>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-xs" onClick={onClose}>
        <div ref={panelRef} role="dialog" aria-modal="true" aria-label="生成穿搭与相册分享卡"
          className="w-full max-w-3xl rounded-2xl border border-border bg-bg-secondary p-5 text-text-primary shadow-xl"
          onClick={(e) => e.stopPropagation()}>
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-base font-semibold">生成穿搭与相册分享卡</h3>
            <button type="button" onClick={onClose} aria-label="关闭" className="rounded-lg p-1.5 text-text-muted hover:bg-bg-hover hover:text-text-primary">
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {photos.length > 0 && (
                <select aria-label="选择游戏本地相册照片" value={selectedPath}
                  onChange={(e) => {
                    initializedRef.current = true;
                    const val = e.target.value;
                    setSelectedPath(val);
                    if (!val) setPhotoImg(null);
                    if (fileRef.current) fileRef.current.value = '';
                  }}
                  className="max-w-xs rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent">
                  <option value="">不附带照片（仅纯文字卡片）</option>
                  {photos.map((p) => (
                    <option key={p.path} value={p.path}>[{p.source}] {p.filename}</option>
                  ))}
                </select>
              )}
              <button type="button" onClick={() => fileRef.current?.click()} className={secondaryButton}>
                <FolderOpen className="h-3.5 w-3.5" />选择本地照片
              </button>
              {photoImg && (
                <button type="button" onClick={() => {
                  initializedRef.current = true;
                  setSelectedPath('');
                  setPhotoImg(null);
                  if (fileRef.current) fileRef.current.value = '';
                }}
                  className="rounded-lg border border-border px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover">
                  移除照片
                </button>
              )}
              <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={(e) => onPickLocalFile(e.target.files?.[0])} />
              <input type="text" value={note} onChange={(e) => setNote(e.target.value)} placeholder="可选备注（如：相机参数 / 染色码）"
                className="min-w-44 flex-1 rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs outline-none focus:border-accent" />
            </div>

            <div className="overflow-auto rounded-xl border border-border bg-bg-tertiary p-3">
              <canvas ref={canvasRef} className="mx-auto max-h-[55vh] w-auto rounded-lg shadow-sm" />
            </div>

            <div className="flex items-center justify-between text-xs text-text-muted">
              <span className="flex items-center gap-1.5">
                <Camera className="h-3.5 w-3.5" />
                {photos.length ? `已自动关联游戏相册（最近 ${photos.length} 张照片）` : '可点击「选择本地照片」附加游戏摄影原图'}
              </span>
              <div className="flex gap-2">
                <button type="button" onClick={downloadCard} className={secondaryButton}>
                  <Download className="h-3.5 w-3.5" />保存图片
                </button>
                <button type="button" onClick={() => void copyCard()} className={primaryButton}>
                  {copied ? <Check className="h-3.5 w-3.5" /> : <ClipboardCopy className="h-3.5 w-3.5" />}
                  {copied ? '已复制' : '复制分享图'}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </ModalPortal>
  );
}

export { ImageIcon };
