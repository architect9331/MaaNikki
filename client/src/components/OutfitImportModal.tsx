import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Camera, Check, ClipboardPaste, ImagePlus, Loader2, Sparkles, X } from 'lucide-react';
import { invoke } from '@tauri-apps/api/core';
import { toast } from 'sonner';
import clsx from 'clsx';
import { isTauri } from '@/utils/paths';
import { extractShareCodes } from '@/services/outfits/matcher';
import {
  compressCoverDataUrl,
  decodeShareCode,
  fileToCompressedCover,
  useOutfitCollectionStore,
} from '@/services/outfits/store';
import type { DecodedShareCode, OutfitEntry } from '@/services/outfits/types';
import type { ScoringPayload } from '@/services/scoring/types';
import { ModalPortal, useModalFocusTrap } from './ui/Modal';
import { primaryButton, secondaryButton } from './ui/FeaturePage';

interface GamePhotoItem {
  path: string;
  filename: string;
  uid: string;
  source: string;
  modified: number;
}

const QUICK_TAGS = ['日常', '拍照', '染色', '礼服', '古风', '哥特', '森系', '学院', '混搭', '待抽'];

function deriveAutoTitle(decoded: DecodedShareCode, payload: ScoringPayload | null): string {
  const gameItems = payload?.catalog.game_items ?? {};
  // Prefer dress (290), then top (230), then hair (210), then first known item.
  const priority = ['290', '230', '210', '220'];
  for (const part of priority) {
    for (const id of decoded.wearing_clothes) {
      const gi = gameItems[String(id)];
      if (gi && gi.part === part && gi.name) {
        return gi.name;
      }
    }
  }
  for (const id of decoded.wearing_clothes) {
    const gi = gameItems[String(id)];
    if (gi?.name) return gi.name;
  }
  return decoded.code.replace(/#$/, '');
}

export function OutfitImportModal({
  open,
  onClose,
  payload,
}: {
  open: boolean;
  onClose: () => void;
  payload: ScoringPayload | null;
}) {
  const existingItems = useOutfitCollectionStore((s) => s.items);
  const addOutfits = useOutfitCollectionStore((s) => s.addOutfits);

  const [rawText, setRawText] = useState('');
  const [title, setTitle] = useState('');
  const [note, setNote] = useState('');
  const [tagsText, setTagsText] = useState('');
  const [coverDataUrl, setCoverDataUrl] = useState<string | undefined>(undefined);
  const [importing, setImporting] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [failures, setFailures] = useState<{ code: string; reason: string }[]>([]);
  const [showPhotoPicker, setShowPhotoPicker] = useState(false);
  const [gamePhotos, setGamePhotos] = useState<GamePhotoItem[]>([]);
  const [loadingPhotos, setLoadingPhotos] = useState(false);
  const [loadingPhotoPath, setLoadingPhotoPath] = useState<string | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleClose = useCallback(() => {
    if (importing) return;
    onClose();
  }, [importing, onClose]);

  useModalFocusTrap({ active: open, containerRef, onEscape: handleClose });

  useEffect(() => {
    if (!open) {
      setRawText('');
      setTitle('');
      setNote('');
      setTagsText('');
      setCoverDataUrl(undefined);
      setFailures([]);
      setProgress(null);
      setShowPhotoPicker(false);
    }
  }, [open]);

  const extractedCodes = useMemo(() => extractShareCodes(rawText), [rawText]);
  const existingSet = useMemo(() => new Set(existingItems.map((i) => i.code)), [existingItems]);
  const freshCodes = useMemo(
    () => extractedCodes.filter((c) => !existingSet.has(c)),
    [extractedCodes, existingSet],
  );
  const duplicateCount = extractedCodes.length - freshCodes.length;

  const parsedTags = useMemo(
    () => Array.from(new Set(tagsText.split(/[\s,，、]+/).map((t) => t.trim()).filter(Boolean))),
    [tagsText],
  );

  const toggleQuickTag = (tag: string) => {
    if (parsedTags.includes(tag)) {
      setTagsText(parsedTags.filter((t) => t !== tag).join(' '));
    } else {
      setTagsText([...parsedTags, tag].join(' '));
    }
  };

  const handlePaste = useCallback(async (e: React.ClipboardEvent) => {
    const items = Array.from(e.clipboardData?.items ?? []);
    const imgItem = items.find((item) => item.type.startsWith('image/'));
    if (!imgItem) return;
    const file = imgItem.getAsFile();
    if (!file) return;
    e.preventDefault();
    try {
      const compressed = await fileToCompressedCover(file);
      setCoverDataUrl(compressed);
      toast.success('已从剪贴板粘贴封面图片');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '粘贴图片失败');
    }
  }, []);

  const handleOpenGamePhotos = async () => {
    if (!isTauri()) return;
    setShowPhotoPicker(true);
    setLoadingPhotos(true);
    try {
      const res = await invoke<{ photos: GamePhotoItem[] }>('list_game_photos');
      setGamePhotos(res.photos ?? []);
    } catch (err) {
      toast.error(String(err));
      setGamePhotos([]);
    } finally {
      setLoadingPhotos(false);
    }
  };

  const handleSelectGamePhoto = async (photo: GamePhotoItem) => {
    setLoadingPhotoPath(photo.path);
    try {
      const dataUrl = await invoke<string>('read_game_photo_base64', { path: photo.path });
      const compressed = await compressCoverDataUrl(dataUrl);
      setCoverDataUrl(compressed);
      setShowPhotoPicker(false);
      toast.success('已选用游戏相册封面');
    } catch (err) {
      toast.error(String(err));
    } finally {
      setLoadingPhotoPath(null);
    }
  };

  const handleImport = async () => {
    if (freshCodes.length === 0) {
      toast.error(extractedCodes.length > 0 ? '识别到的搭配码已在收藏列表中' : '请先粘贴至少一个有效搭配码');
      return;
    }
    setImporting(true);
    setFailures([]);
    setProgress({ done: 0, total: freshCodes.length });
    const created: OutfitEntry[] = [];
    const failed: { code: string; reason: string }[] = [];
    const now = new Date().toISOString();

    for (let i = 0; i < freshCodes.length; i += 1) {
      const code = freshCodes[i];
      try {
        const decoded = await decodeShareCode(code);
        const autoTitle = deriveAutoTitle(decoded, payload);
        const entryTitle = freshCodes.length === 1 && title.trim()
          ? title.trim()
          : title.trim()
            ? `${title.trim()} · ${autoTitle}`
            : autoTitle;
        created.push({
          id: `outfit_${Date.now()}_${i}_${Math.random().toString(36).slice(2, 7)}`,
          code: decoded.code,
          region: decoded.region,
          title: entryTitle,
          note: note.trim(),
          tags: parsedTags,
          coverDataUrl: freshCodes.length === 1 ? coverDataUrl : undefined,
          createdAt: now,
          updatedAt: now,
          decoded,
        });
      } catch (err) {
        failed.push({ code, reason: err instanceof Error ? err.message : String(err) });
      }
      setProgress({ done: i + 1, total: freshCodes.length });
    }

    try {
      if (created.length > 0) {
        await addOutfits(created);
      }
      if (failed.length === 0) {
        toast.success(created.length === 1 ? `已导入搭配「${created[0].title}」` : `已批量导入 ${created.length} 套搭配`);
        onClose();
      } else {
        setFailures(failed);
        if (created.length > 0) {
          toast.warning(`已导入 ${created.length} 套，另有 ${failed.length} 个搭配码解析失败`);
        } else {
          toast.error('搭配码解析失败，请检查下方原因');
        }
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '保存搭配收藏失败');
    } finally {
      setImporting(false);
    }
  };

  if (!open) return null;

  return (
    <ModalPortal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-xs"
        onMouseDown={(e) => { if (e.target === e.currentTarget) handleClose(); }}
        onPaste={handlePaste}
      >
        <div
          ref={containerRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="outfit-import-title"
          className="flex max-h-[88vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-border bg-bg-secondary text-text-primary shadow-xl"
        >
          <header className="flex items-center gap-2 border-b border-border px-5 py-3.5">
            <Sparkles className="h-4 w-4 text-accent" />
            <h2 id="outfit-import-title" className="text-sm font-semibold">导入搭配码</h2>
            <span className="text-xs text-text-muted">支持直接粘贴整段分享文案自动提取</span>
            <button
              type="button"
              onClick={handleClose}
              disabled={importing}
              aria-label="关闭"
              className="ml-auto rounded-lg p-1 text-text-secondary hover:bg-bg-hover disabled:opacity-40"
            >
              <X className="h-4 w-4" />
            </button>
          </header>

          <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4 text-xs">
            <div>
              <label className="mb-1.5 block font-medium text-text-primary">
                搭配码 / 分享文案
              </label>
              <textarea
                value={rawText}
                onChange={(e) => setRawText(e.target.value)}
                rows={4}
                placeholder={'粘贴单个或多个搭配码（如 At9Vswpuo69#），或直接粘贴整段 B站 / 小红书 / NGA 分享文案，会自动提取所有搭配码。'}
                className="w-full rounded-xl border border-border bg-bg-primary px-3 py-2 text-xs leading-5 outline-none focus:border-accent"
              />
              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-text-secondary">
                {extractedCodes.length > 0 ? (
                  <>
                    <span className="inline-flex items-center gap-1 rounded-full bg-accent/12 px-2.5 py-0.5 font-medium text-accent">
                      <Check className="h-3 w-3" />已识别 {extractedCodes.length} 个搭配码
                    </span>
                    {duplicateCount > 0 && (
                      <span className="text-text-muted">（其中 {duplicateCount} 个已在收藏中，将自动跳过）</span>
                    )}
                    <div className="flex w-full flex-wrap gap-1 pt-1">
                      {extractedCodes.slice(0, 12).map((code) => {
                        const dup = existingSet.has(code);
                        return (
                          <span
                            key={code}
                            className={clsx(
                              'rounded-md border px-1.5 py-0.5 font-mono text-[11px]',
                              dup
                                ? 'border-border bg-bg-primary text-text-muted line-through'
                                : 'border-accent/30 bg-accent/8 text-accent',
                            )}
                          >
                            {code}
                          </span>
                        );
                      })}
                      {extractedCodes.length > 12 && (
                        <span className="text-text-muted">…共 {extractedCodes.length} 个</span>
                      )}
                    </div>
                  </>
                ) : (
                  <span className="text-text-muted">提示：支持国服、亚服、美服、欧服官方搭配码，无需启动游戏即可联网解析。</span>
                )}
              </div>
            </div>

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <label className="mb-1 block font-medium text-text-primary">
                  搭配名称 <span className="font-normal text-text-muted">（留空自动取主裙/发型名）</span>
                </label>
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder={freshCodes.length > 1 ? '批量导入可填统一前缀，如 雾蓝礼服' : '如：雾蓝长裙'}
                  className="w-full rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs outline-none focus:border-accent"
                />
              </div>
              <div>
                <label className="mb-1 block font-medium text-text-primary">
                  自定义标签 <span className="font-normal text-text-muted">（空格或逗号分隔）</span>
                </label>
                <input
                  type="text"
                  value={tagsText}
                  onChange={(e) => setTagsText(e.target.value)}
                  placeholder="如：日常 拍照 礼服"
                  className="w-full rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs outline-none focus:border-accent"
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-text-muted">快捷标签：</span>
              {QUICK_TAGS.map((tag) => {
                const active = parsedTags.includes(tag);
                return (
                  <button
                    key={tag}
                    type="button"
                    onClick={() => toggleQuickTag(tag)}
                    className={clsx(
                      'rounded-full border px-2.5 py-0.5 text-[11px] transition-colors',
                      active
                        ? 'border-accent bg-accent/15 font-medium text-accent'
                        : 'border-border bg-bg-primary text-text-secondary hover:bg-bg-hover',
                    )}
                  >
                    {tag}
                  </button>
                );
              })}
            </div>

            <div>
              <label className="mb-1 block font-medium text-text-primary">
                备注 / 来源 <span className="font-normal text-text-muted">（可选）</span>
              </label>
              <input
                type="text"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="可记录原作者、染色思路或拍照场景"
                className="w-full rounded-lg border border-border bg-bg-primary px-3 py-1.5 text-xs outline-none focus:border-accent"
              />
            </div>

            {freshCodes.length <= 1 && (
              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="font-medium text-text-primary">
                    封面预览图 <span className="font-normal text-text-muted">（可选，支持直接 Ctrl+V 粘贴截图）</span>
                  </span>
                  {coverDataUrl && (
                    <button
                      type="button"
                      onClick={() => setCoverDataUrl(undefined)}
                      className="text-[11px] text-text-muted hover:text-warning"
                    >
                      清除封面
                    </button>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2">
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
                        setCoverDataUrl(await fileToCompressedCover(f));
                      } catch (err) {
                        toast.error(err instanceof Error ? err.message : '读取图片失败');
                      }
                    }}
                  />
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    className={secondaryButton}
                  >
                    <ImagePlus className="h-3.5 w-3.5" />选择本地图片
                  </button>
                  {isTauri() && (
                    <button
                      type="button"
                      onClick={() => void handleOpenGamePhotos()}
                      className={secondaryButton}
                    >
                      <Camera className="h-3.5 w-3.5" />从游戏相册选取
                    </button>
                  )}
                  <span className="inline-flex items-center gap-1 text-[11px] text-text-muted">
                    <ClipboardPaste className="h-3.5 w-3.5" />也可直接按 Ctrl+V 粘贴截图
                  </span>
                </div>
                {coverDataUrl && (
                  <div className="mt-2 overflow-hidden rounded-xl border border-border bg-bg-primary">
                    <img src={coverDataUrl} alt="封面预览" className="max-h-44 w-full object-contain" />
                  </div>
                )}
                {showPhotoPicker && (
                  <div className="mt-2 rounded-xl border border-border bg-bg-primary p-3">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="font-medium text-text-secondary">最近的游戏相册 / 截图</span>
                      <button
                        type="button"
                        onClick={() => setShowPhotoPicker(false)}
                        className="text-[11px] text-text-muted hover:text-text-primary"
                      >
                        收起
                      </button>
                    </div>
                    {loadingPhotos ? (
                      <div className="flex items-center gap-2 py-4 text-text-muted">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />正在扫描游戏相册…
                      </div>
                    ) : gamePhotos.length === 0 ? (
                      <p className="py-2 text-text-muted">未扫描到游戏相册照片，可直接粘贴截图或选择本地图片。</p>
                    ) : (
                      <div className="max-h-36 space-y-1 overflow-y-auto">
                        {gamePhotos.slice(0, 15).map((photo) => (
                          <button
                            key={photo.path}
                            type="button"
                            disabled={loadingPhotoPath !== null}
                            onClick={() => void handleSelectGamePhoto(photo)}
                            className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left hover:bg-bg-hover disabled:opacity-50"
                          >
                            <span className="truncate font-mono text-[11px]">{photo.filename}</span>
                            <span className="ml-2 shrink-0 text-[11px] text-text-muted">
                              {loadingPhotoPath === photo.path ? '加载中…' : photo.source}
                            </span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {failures.length > 0 && (
              <div className="rounded-xl border border-warning/40 bg-warning/8 p-3">
                <p className="mb-1 font-medium text-warning">以下搭配码解析失败：</p>
                <ul className="space-y-1 text-[11px] text-text-secondary">
                  {failures.map((f) => (
                    <li key={f.code}>
                      <span className="font-mono text-text-primary">{f.code}</span>：{f.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          <footer className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
            <span className="text-xs text-text-muted">
              {progress && importing ? `正在解析 ${progress.done} / ${progress.total}…` : '解析后的数据会缓存到本地，切换筛选无需重复联网'}
            </span>
            <div className="flex items-center gap-2">
              <button type="button" onClick={handleClose} disabled={importing} className={secondaryButton}>
                取消
              </button>
              <button
                type="button"
                onClick={() => void handleImport()}
                disabled={importing || freshCodes.length === 0}
                className={primaryButton}
              >
                {importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                {importing
                  ? '解析中…'
                  : freshCodes.length > 1
                    ? `解析并收藏 (${freshCodes.length})`
                    : '解析并收藏'}
              </button>
            </div>
          </footer>
        </div>
      </div>
    </ModalPortal>
  );
}
