import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  Camera,
  Check,
  CheckCircle2,
  ClipboardPaste,
  Copy,
  Edit3,
  ImagePlus,
  Loader2,
  Palette,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
  XCircle,
} from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import { toast } from 'sonner';
import clsx from 'clsx';
import { isTauri } from '@/utils/paths';
import {
  compressCoverDataUrl,
  decodeShareCode,
  fileToCompressedCover,
  useOutfitCollectionStore,
} from '@/services/outfits/store';
import type { OutfitEntry, OutfitEvaluation, ResolvedOutfitPiece } from '@/services/outfits/types';
import { ModalPortal, useModalFocusTrap } from './ui/Modal';
import { primaryButton, secondaryButton } from './ui/FeaturePage';

interface GamePhotoItem {
  path: string;
  filename: string;
  source: string;
}

const CATEGORY_LABELS: Record<'core' | 'accessory' | 'makeup', string> = {
  core: '主体服装',
  accessory: '饰品部件',
  makeup: '妆容部件',
};

function statusSummaryText(ev: OutfitEvaluation): { label: string; cls: string } {
  if (ev.status === 'wearable') {
    return {
      label: ev.sameModelCount > 0 ? `可直接穿（含 ${ev.sameModelCount} 件同模）` : '可直接穿',
      cls: 'border-success/35 bg-success/12 text-success',
    };
  }
  if (ev.status === 'same_model') {
    return {
      label: `同模齐全 · 缺 ${ev.sameModelCount} 件进化/焕新色`,
      cls: 'border-warning/35 bg-warning/12 text-warning',
    };
  }
  const gap = Math.max(1, ev.requiredCount - ev.matchedCount);
  if (ev.status === 'near') {
    return {
      label: `仅缺 ${gap} 件`,
      cls: 'border-warning/35 bg-warning/12 text-warning',
    };
  }
  return {
    label: `缺少 ${gap} 件`,
    cls: 'border-error/35 bg-error/10 text-error',
  };
}

function PieceRow({ piece }: { piece: ResolvedOutfitPiece }) {
  const icon = piece.ignored ? (
    <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-border text-[10px] text-text-muted">
      —
    </span>
  ) : piece.ownership === 'owned_exact' ? (
    <CheckCircle2 className="h-4 w-4 shrink-0 text-success" />
  ) : piece.ownership === 'owned_group' ? (
    <AlertCircle className={clsx('h-4 w-4 shrink-0', piece.satisfied ? 'text-success' : 'text-warning')} />
  ) : (
    <XCircle className="h-4 w-4 shrink-0 text-error" />
  );

  const badge = piece.ignored
    ? { text: '已忽略', cls: 'bg-bg-primary text-text-muted border-border' }
    : piece.ownership === 'owned_exact'
      ? { text: '已拥有', cls: 'bg-success/12 text-success border-success/25' }
      : piece.ownership === 'owned_group'
        ? { text: '已有同模', cls: 'bg-warning/12 text-warning border-warning/25' }
        : { text: '未拥有', cls: 'bg-error/10 text-error border-error/25' };

  return (
    <div
      className={clsx(
        'flex flex-wrap items-center gap-2 rounded-xl border px-3 py-2 text-xs',
        piece.ignored
          ? 'border-border/60 bg-bg-primary/40 opacity-65'
          : piece.ownership === 'missing'
            ? 'border-error/25 bg-error/[0.03]'
            : 'border-border bg-bg-primary/80',
      )}
    >
      {icon}
      <span className="shrink-0 rounded-md bg-bg-secondary px-1.5 py-0.5 text-[11px] text-text-secondary">
        {piece.partName}
      </span>
      <span className="min-w-0 font-medium text-text-primary">{piece.name}</span>
      {piece.quality > 0 && (
        <span className="shrink-0 text-[11px] tabular-nums text-text-muted">{piece.quality >= 8 ? '星冕' : `${piece.quality}★`}</span>
      )}
      {piece.variantLabel && (
        <span className="shrink-0 rounded-full border border-accent/25 bg-accent/10 px-2 py-0.5 text-[10px] text-accent">
          {piece.variantLabel}
        </span>
      )}
      {piece.suitName && (
        <span className="shrink-0 text-[11px] text-text-muted">「{piece.suitName}」</span>
      )}
      {piece.dyedHexColors.length > 0 && (
        <span
          className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-secondary px-2 py-0.5"
          title={`已染色：${piece.dyedHexColors.join(', ')}`}
        >
          <Palette className="h-3 w-3 text-text-muted" />
          {piece.dyedHexColors.slice(0, 6).map((hex) => (
            <span
              key={hex}
              className="h-2.5 w-2.5 rounded-full border border-black/15"
              style={{ backgroundColor: hex }}
            />
          ))}
        </span>
      )}
      {piece.hasPattern && (
        <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] text-text-secondary">
          织纹
        </span>
      )}
      {piece.fxHidden && (
        <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] text-text-muted">
          隐藏特效
        </span>
      )}
      <span className={clsx('ml-auto shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium', badge.cls)}>
        {badge.text}
      </span>
      {piece.ownership === 'missing' && piece.source && (
        <p className="w-full pl-6 text-[11px] text-text-muted">获取途径：{piece.source}</p>
      )}
    </div>
  );
}

export function OutfitDetailModal({
  outfit,
  evaluation,
  onClose,
}: {
  outfit: OutfitEntry | null;
  evaluation: OutfitEvaluation | null;
  onClose: () => void;
}) {
  const updateOutfit = useOutfitCollectionStore((s) => s.updateOutfit);
  const deleteOutfit = useOutfitCollectionStore((s) => s.deleteOutfit);

  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState('');
  const [note, setNote] = useState('');
  const [tagsText, setTagsText] = useState('');
  const [refreshingCode, setRefreshingCode] = useState(false);
  const [copied, setCopied] = useState(false);
  const [showPhotoPicker, setShowPhotoPicker] = useState(false);
  const [gamePhotos, setGamePhotos] = useState<GamePhotoItem[]>([]);
  const [loadingPhotos, setLoadingPhotos] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const containerRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  useModalFocusTrap({ active: Boolean(outfit), containerRef, onEscape: onClose });

  useEffect(() => {
    if (outfit) {
      setTitle(outfit.title);
      setNote(outfit.note);
      setTagsText(outfit.tags.join(' '));
      setEditing(false);
      setConfirmDelete(false);
      setShowPhotoPicker(false);
    }
  }, [outfit]);

  const groupedPieces = useMemo(() => {
    if (!evaluation) return { core: [], accessory: [], makeup: [] };
    return {
      core: evaluation.pieces.filter((p) => p.category === 'core'),
      accessory: evaluation.pieces.filter((p) => p.category === 'accessory'),
      makeup: evaluation.pieces.filter((p) => p.category === 'makeup'),
    };
  }, [evaluation]);

  const handleCopyCode = async () => {
    if (!outfit) return;
    try {
      await navigator.clipboard.writeText(outfit.code);
      setCopied(true);
      toast.success(`已复制搭配码 ${outfit.code}`);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      toast.error('复制失败，请手动复制');
    }
  };

  const handleSaveMeta = async () => {
    if (!outfit) return;
    const tags = Array.from(new Set(tagsText.split(/[\s,，、]+/).map((t) => t.trim()).filter(Boolean)));
    await updateOutfit(outfit.id, {
      title: title.trim() || outfit.title,
      note: note.trim(),
      tags,
    });
    setEditing(false);
    toast.success('已更新搭配信息');
  };

  const handlePasteCover = useCallback(async (e: React.ClipboardEvent) => {
    if (!outfit) return;
    const items = Array.from(e.clipboardData?.items ?? []);
    const imgItem = items.find((item) => item.type.startsWith('image/'));
    if (!imgItem) return;
    const file = imgItem.getAsFile();
    if (!file) return;
    e.preventDefault();
    try {
      const compressed = await fileToCompressedCover(file);
      await updateOutfit(outfit.id, { coverDataUrl: compressed });
      toast.success('已从剪贴板更新封面');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '更新封面失败');
    }
  }, [outfit, updateOutfit]);

  const handleRefreshDecode = async () => {
    if (!outfit || refreshingCode) return;
    setRefreshingCode(true);
    try {
      const decoded = await decodeShareCode(outfit.code);
      await updateOutfit(outfit.id, { decoded });
      toast.success('已重新解析搭配码数据');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '重新解析失败');
    } finally {
      setRefreshingCode(false);
    }
  };

  const handleOpenGamePhotos = async () => {
    if (!isTauri()) return;
    setShowPhotoPicker(true);
    setLoadingPhotos(true);
    try {
      const res = await invoke<{ photos: GamePhotoItem[] }>('list_game_photos');
      setGamePhotos(res.photos ?? []);
    } catch (err) {
      toast.error(String(err));
    } finally {
      setLoadingPhotos(false);
    }
  };

  if (!outfit || !evaluation) return null;

  const summary = statusSummaryText(evaluation);
  const gradientColors = evaluation.paletteHex.length >= 2
    ? evaluation.paletteHex.slice(0, 3)
    : evaluation.paletteHex.length === 1
      ? [evaluation.paletteHex[0], '#6366f1']
      : ['#6366f1', '#ec4899'];

  return (
    <ModalPortal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-xs"
        onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
        onPaste={handlePasteCover}
      >
        <div
          ref={containerRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="outfit-detail-title"
          className="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-border bg-bg-secondary text-text-primary shadow-xl"
        >
          <header className="flex items-center gap-3 border-b border-border px-5 py-3.5">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 id="outfit-detail-title" className="truncate text-base font-semibold">
                  {outfit.title}
                </h2>
                <span className={clsx('rounded-full border px-2.5 py-0.5 text-xs font-medium', summary.cls)}>
                  {summary.label}
                </span>
                <span className="rounded-md border border-border bg-bg-primary px-1.5 py-0.5 text-[11px] text-text-secondary">
                  {outfit.region}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-text-secondary">
                <code className="rounded bg-bg-primary px-1.5 py-0.5 font-mono text-[11px] text-text-primary">
                  {outfit.code}
                </code>
                <button
                  type="button"
                  onClick={() => void handleCopyCode()}
                  className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-accent hover:bg-accent/10"
                >
                  {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                  {copied ? '已复制' : '复制搭配码'}
                </button>
                <span className="tabular-nums text-text-muted">
                  匹配 {evaluation.matchedCount} / {evaluation.requiredCount} 件（共 {evaluation.totalPieceCount} 件）
                </span>
              </div>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover"
            >
              <X className="h-4 w-4" />
            </button>
          </header>

          <div className="grid flex-1 grid-cols-1 gap-4 overflow-y-auto p-5 md:grid-cols-[260px_1fr]">
            {/* Left column: cover, palette, metadata & actions */}
            <div className="space-y-3">
              <div className="relative overflow-hidden rounded-xl border border-border bg-bg-primary">
                {outfit.coverDataUrl ? (
                  <img
                    src={outfit.coverDataUrl}
                    alt={outfit.title}
                    className="max-h-72 w-full object-cover"
                  />
                ) : (
                  <div
                    className="flex h-48 flex-col items-center justify-center p-4 text-center text-white"
                    style={{
                      background: `linear-gradient(135deg, ${gradientColors.join(', ')})`,
                    }}
                  >
                    <Sparkles className="h-7 w-7 opacity-85" />
                    <p className="mt-2 text-sm font-semibold drop-shadow-xs">{outfit.title}</p>
                    <p className="mt-1 text-[11px] opacity-85">按 Ctrl+V 可直接粘贴搭配截图作为封面</p>
                  </div>
                )}
              </div>

              <div className="flex flex-wrap gap-1.5">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    e.target.value = '';
                    if (!f) return;
                    try {
                      const compressed = await fileToCompressedCover(f);
                      await updateOutfit(outfit.id, { coverDataUrl: compressed });
                      toast.success('已更新封面');
                    } catch (err) {
                      toast.error(err instanceof Error ? err.message : '读取图片失败');
                    }
                  }}
                />
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className="inline-flex flex-1 items-center justify-center gap-1 rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-[11px] hover:bg-bg-hover"
                >
                  <ImagePlus className="h-3.5 w-3.5" />换封面
                </button>
                {isTauri() && (
                  <button
                    type="button"
                    onClick={() => void handleOpenGamePhotos()}
                    className="inline-flex flex-1 items-center justify-center gap-1 rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-[11px] hover:bg-bg-hover"
                  >
                    <Camera className="h-3.5 w-3.5" />游戏相册
                  </button>
                )}
                {outfit.coverDataUrl && (
                  <button
                    type="button"
                    onClick={() => void updateOutfit(outfit.id, { coverDataUrl: undefined })}
                    className="rounded-lg border border-border bg-bg-primary px-2 py-1.5 text-[11px] text-text-muted hover:text-warning"
                  >
                    清除
                  </button>
                )}
              </div>

              {showPhotoPicker && (
                <div className="rounded-xl border border-border bg-bg-primary p-2.5 text-xs">
                  <div className="mb-1.5 flex items-center justify-between">
                    <span className="text-[11px] font-medium text-text-secondary">选择游戏相册照片</span>
                    <button
                      type="button"
                      onClick={() => setShowPhotoPicker(false)}
                      className="text-[11px] text-text-muted"
                    >
                      关闭
                    </button>
                  </div>
                  {loadingPhotos ? (
                    <div className="flex items-center gap-1.5 py-2 text-[11px] text-text-muted">
                      <Loader2 className="h-3 w-3 animate-spin" />扫描中…
                    </div>
                  ) : gamePhotos.length === 0 ? (
                    <p className="text-[11px] text-text-muted">未找到相册照片</p>
                  ) : (
                    <div className="max-h-32 space-y-1 overflow-y-auto">
                      {gamePhotos.slice(0, 12).map((p) => (
                        <button
                          key={p.path}
                          type="button"
                          onClick={async () => {
                            try {
                              const raw = await invoke<string>('read_game_photo_base64', { path: p.path });
                              await updateOutfit(outfit.id, { coverDataUrl: await compressCoverDataUrl(raw) });
                              setShowPhotoPicker(false);
                              toast.success('已选用相册封面');
                            } catch (err) {
                              toast.error(String(err));
                            }
                          }}
                          className="block w-full truncate rounded px-1.5 py-1 text-left font-mono text-[11px] hover:bg-bg-hover"
                        >
                          {p.filename}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {evaluation.paletteHex.length > 0 && (
                <div className="rounded-xl border border-border bg-bg-primary p-3">
                  <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-text-secondary">
                    <Palette className="h-3.5 w-3.5 text-accent" />
                    染色主色调（{evaluation.dyedPieceCount} 件已染色）
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {evaluation.paletteHex.map((hex) => (
                      <span
                        key={hex}
                        className="inline-flex items-center gap-1 rounded-full border border-border bg-bg-secondary px-2 py-0.5 text-[10px] font-mono text-text-secondary"
                      >
                        <span className="h-2.5 w-2.5 rounded-full border border-black/15" style={{ backgroundColor: hex }} />
                        {hex}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div className="rounded-xl border border-border bg-bg-primary p-3 text-xs">
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-medium text-text-secondary">备注与标签</span>
                  {!editing && (
                    <button
                      type="button"
                      onClick={() => setEditing(true)}
                      className="inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
                    >
                      <Edit3 className="h-3 w-3" />编辑
                    </button>
                  )}
                </div>
                {editing ? (
                  <div className="space-y-2">
                    <input
                      type="text"
                      value={title}
                      onChange={(e) => setTitle(e.target.value)}
                      placeholder="搭配标题"
                      className="w-full rounded-lg border border-border bg-bg-secondary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
                    />
                    <input
                      type="text"
                      value={tagsText}
                      onChange={(e) => setTagsText(e.target.value)}
                      placeholder="标签（空格分隔）"
                      className="w-full rounded-lg border border-border bg-bg-secondary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
                    />
                    <textarea
                      rows={2}
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      placeholder="备注 / 来源"
                      className="w-full rounded-lg border border-border bg-bg-secondary px-2.5 py-1.5 text-xs outline-none focus:border-accent"
                    />
                    <div className="flex justify-end gap-1.5">
                      <button type="button" onClick={() => setEditing(false)} className={secondaryButton}>
                        取消
                      </button>
                      <button type="button" onClick={() => void handleSaveMeta()} className={primaryButton}>
                        保存
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {outfit.tags.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {outfit.tags.map((tag) => (
                          <span
                            key={tag}
                            className="rounded-full bg-accent/10 px-2 py-0.5 text-[11px] text-accent"
                          >
                            #{tag}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <p className="text-[11px] text-text-muted">暂无标签</p>
                    )}
                    {outfit.note && (
                      <p className="whitespace-pre-wrap text-[11px] leading-5 text-text-secondary">
                        {outfit.note}
                      </p>
                    )}
                  </div>
                )}
              </div>

              <div className="flex items-center gap-2 pt-1">
                {isTauri() && (
                  <button
                    type="button"
                    disabled={refreshingCode}
                    onClick={() => void handleRefreshDecode()}
                    className="inline-flex flex-1 items-center justify-center gap-1 rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs text-text-secondary hover:bg-bg-hover disabled:opacity-40"
                  >
                    <RefreshCw className={clsx('h-3.5 w-3.5', refreshingCode && 'animate-spin')} />
                    重新解析
                  </button>
                )}
                {confirmDelete ? (
                  <button
                    type="button"
                    onClick={async () => {
                      await deleteOutfit(outfit.id);
                      toast.success('已移出搭配收藏');
                      onClose();
                    }}
                    className="inline-flex items-center gap-1 rounded-lg bg-error px-3 py-1.5 text-xs font-medium text-white"
                  >
                    确认删除
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(true)}
                    className="inline-flex items-center gap-1 rounded-lg border border-border bg-bg-primary px-2.5 py-1.5 text-xs text-text-muted hover:border-error/40 hover:text-error"
                  >
                    <Trash2 className="h-3.5 w-3.5" />删除
                  </button>
                )}
              </div>
              <p className="flex items-center gap-1 text-[11px] text-text-muted">
                <ClipboardPaste className="h-3 w-3 shrink-0" />
                在弹窗内直接按 Ctrl+V 即可更换封面图
              </p>
            </div>

            {/* Right column: piece-by-piece breakdown */}
            <div className="space-y-4">
              {(['core', 'accessory', 'makeup'] as const).map((cat) => {
                const list = groupedPieces[cat];
                if (list.length === 0) return null;
                const activeCount = list.filter((p) => !p.ignored).length;
                const satisfiedCount = list.filter((p) => !p.ignored && p.satisfied).length;
                const allIgnored = activeCount === 0;
                return (
                  <section key={cat} className="space-y-2">
                    <div className="flex items-center justify-between">
                      <h3 className="text-xs font-semibold text-text-primary">
                        {CATEGORY_LABELS[cat]}
                        <span className="ml-1.5 font-normal text-text-muted">（{list.length} 件）</span>
                      </h3>
                      <span className="text-[11px] tabular-nums text-text-secondary">
                        {allIgnored ? '当前匹配规则已忽略' : `已满足 ${satisfiedCount} / ${activeCount}`}
                      </span>
                    </div>
                    <div className="space-y-1.5">
                      {list.map((piece) => (
                        <PieceRow key={piece.clothId} piece={piece} />
                      ))}
                    </div>
                  </section>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </ModalPortal>
  );
}
