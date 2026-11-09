import { useState, useMemo, useEffect, useLayoutEffect, useCallback, useRef, useId, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';
import { ArrowDown, ArrowUp, Check, ChevronDown, FolderOpen, Layers, Plus, Route, Trash2, X } from 'lucide-react';
import { open as openFileDialog } from '@tauri-apps/plugin-dialog';
import { isTauri } from '@/utils/paths';
import { useAppStore } from '@/stores/appStore';
import {
  findRouteSummaryByValue,
  formatRouteValueLabel,
  listRoutes,
  MAP_TITLES,
  useRouteCatalogStore,
  type RouteSummary,
} from '@/services/routeStudioService';

function routeBadgeText(item: RouteSummary): string {
  if (item.kind === 'route_collection') {
    return `清单 · ${item.points} 条`;
  }
  const mapTitle = MAP_TITLES[item.map] || item.map || '';
  return mapTitle ? `${mapTitle} · ${item.points} 点` : `${item.points} 点`;
}

export function RouteSelectInput({
  value,
  onChange,
  onSelectSummary,
  placeholder = '请选择路线…',
  disabled = false,
  className,
}: {
  value: string;
  onChange: (path: string) => void;
  onSelectSummary?: (summary: RouteSummary) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}) {
  const triggerId = useId();
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listboxRef = useRef<HTMLDivElement | null>(null);
  const { user, builtin, loaded } = useRouteCatalogStore();

  useEffect(() => {
    if (isTauri() && !loaded) {
      void listRoutes().catch(() => {});
    }
  }, [loaded]);

  const runnableBuiltin = useMemo(
    () => builtin.filter((item) => item.runnable !== false),
    [builtin],
  );

  const selectedRoute = useMemo(
    () => findRouteSummaryByValue(value, user, builtin),
    [value, user, builtin],
  );
  const displayLabel = useMemo(
    () => formatRouteValueLabel(value, user, builtin),
    [value, user, builtin],
  );

  const filteredUser = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return user;
    return user.filter((item) => {
      const mapTitle = MAP_TITLES[item.map] ?? item.map ?? '';
      return `${item.title ?? ''} ${item.id} ${mapTitle}`.toLowerCase().includes(q);
    });
  }, [user, searchQuery]);

  const filteredBuiltin = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return runnableBuiltin;
    return runnableBuiltin.filter((item) => {
      const mapTitle = MAP_TITLES[item.map] ?? item.map ?? '';
      return `${item.title ?? ''} ${item.id} ${mapTitle}`.toLowerCase().includes(q);
    });
  }, [runnableBuiltin, searchQuery]);

  const flatOptions = useMemo(
    () => [...filteredUser, ...filteredBuiltin],
    [filteredUser, filteredBuiltin],
  );
  const [pos, setPos] = useState<{ top?: number; bottom?: number; left: number; width: number; maxHeight: number } | null>(null);

  const closeDropdown = useCallback(() => {
    setOpen(false);
    setSearchQuery('');
  }, []);

  const closeAndFocusTrigger = useCallback(() => {
    setOpen(false);
    setSearchQuery('');
    triggerRef.current?.focus({ preventScroll: true });
  }, []);

  const updatePos = useCallback(() => {
    if (!triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) {
      closeDropdown();
      return;
    }
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const width = Math.min(Math.max(rect.width, 320), vw - 16);
    let left = rect.right - width;
    if (left < 8) left = Math.max(8, rect.left);
    if (left + width > vw - 8) left = Math.max(8, vw - width - 8);
    const spaceBelow = vh - rect.bottom - 12;
    const spaceAbove = rect.top - 12;
    if (spaceBelow < 220 && spaceAbove > spaceBelow) {
      setPos({ bottom: vh - rect.top + 4, left, width, maxHeight: Math.min(320, Math.max(150, spaceAbove)) });
    } else {
      setPos({ top: rect.bottom + 4, left, width, maxHeight: Math.min(320, Math.max(150, spaceBelow)) });
    }
  }, [closeDropdown]);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    updatePos();
  }, [open, updatePos]);

  useEffect(() => {
    if (!open) return;
    const onScroll = (e: Event) => {
      const target = e.target;
      if (target instanceof Node && menuRef.current?.contains(target)) return;
      if (target instanceof Node && triggerRef.current && target.contains(triggerRef.current)) {
        closeDropdown();
      }
    };
    const onOutside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        closeDropdown();
      }
    };
    window.addEventListener('resize', updatePos);
    window.addEventListener('scroll', onScroll, true);
    document.addEventListener('mousedown', onOutside);
    return () => {
      window.removeEventListener('resize', updatePos);
      window.removeEventListener('scroll', onScroll, true);
      document.removeEventListener('mousedown', onOutside);
    };
  }, [open, updatePos, closeDropdown]);

  const openDropdown = () => {
    if (disabled) return;
    setSearchQuery('');
    if (isTauri()) void listRoutes().catch(() => {});
    const idx = selectedRoute ? flatOptions.findIndex((item) => item.id === selectedRoute.id) : 0;
    setActiveIndex(Math.max(0, idx));
    setOpen(true);
  };

  useEffect(() => {
    if (open && !disabled) {
      const timer = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(timer);
    }
  }, [open, disabled]);

  useEffect(() => {
    setActiveIndex(0);
  }, [searchQuery]);
  useEffect(() => {
    if (!open || !listboxRef.current) return;
    const el = listboxRef.current.querySelector(`[data-index="${activeIndex}"]`);
    if (el) el.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, open]);

  const selectItem = (item: RouteSummary) => {
    onChange(item.path);
    onSelectSummary?.(item);
    closeAndFocusTrigger();
  };

  const handleBrowseFile = async () => {
    if (!isTauri() || disabled) return;
    closeDropdown();
    try {
      const selected = await openFileDialog({
        multiple: false,
        filters: [{ name: '路线 JSON', extensions: ['json'] }],
      });
      if (selected && typeof selected === 'string') {
        onChange(selected);
      }
    } catch {
      // Ignore canceled dialog
    }
  };

  const handleInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Tab') {
      closeAndFocusTrigger();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      closeAndFocusTrigger();
      return;
    }
    if (!flatOptions.length) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((prev) => Math.min(flatOptions.length - 1, prev + 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((prev) => Math.max(0, prev - 1));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const target = flatOptions[activeIndex];
      if (target) selectItem(target);
    }
  };

  const renderRow = (item: RouteSummary, index: number) => {
    const isSelected = selectedRoute ? selectedRoute.id === item.id && selectedRoute.builtin === item.builtin : value === item.path;
    const isActive = index === activeIndex;
    return (
      <button
        key={`${item.builtin ? 'b' : 'u'}-${item.id}`}
        type="button"
        data-index={index}
        onClick={() => selectItem(item)}
        onMouseEnter={() => setActiveIndex(index)}
        className={clsx(
          'w-full px-3 py-2 text-left text-xs flex items-center justify-between gap-2 transition-colors',
          isActive
            ? 'bg-bg-active text-text-primary'
            : isSelected
              ? 'bg-accent/10 text-accent font-medium'
              : 'text-text-primary hover:bg-bg-hover',
        )}
        role="option"
        aria-selected={isSelected}
        title={item.title || item.id}
      >
        <span className="flex items-center gap-1.5 min-w-0 flex-1">
          {item.kind === 'route_collection' && <Layers className="w-3.5 h-3.5 shrink-0 text-accent-secondary" />}
          <span className="truncate">{item.title || item.id}</span>
        </span>
        <span className="flex items-center gap-1.5 shrink-0">
          <span className="text-[11px] text-text-muted">{routeBadgeText(item)}</span>
          {isSelected && <Check className="w-3.5 h-3.5 text-accent" />}
        </span>
      </button>
    );
  };
  return (
    <div className={clsx('flex items-center gap-1.5 min-w-0', className)}>
      <button
        type="button"
        id={triggerId}
        ref={triggerRef}
        disabled={disabled}
        onClick={() => (open ? closeDropdown() : openDropdown())}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === ' ' || e.key === 'Enter' || e.key === 'ArrowDown') {
            e.preventDefault();
            if (!open) openDropdown();
          } else if (e.key === 'Escape' && open) {
            e.preventDefault();
            closeAndFocusTrigger();
          }
        }}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        title={selectedRoute ? `${selectedRoute.title || selectedRoute.id}（${routeBadgeText(selectedRoute)}）` : value || placeholder}
        className={clsx(
          'min-w-0 flex-1 px-3 py-1.5 text-sm rounded-lg border flex items-center justify-between gap-2',
          'bg-bg-secondary text-text-primary border-border',
          'focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/20',
          disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:bg-bg-hover transition-colors',
        )}
      >
        <span className="flex items-center gap-1.5 min-w-0 truncate">
          {selectedRoute?.kind === 'route_collection' && (
            <Layers className="w-3.5 h-3.5 shrink-0 text-accent-secondary" />
          )}
          <span className={clsx('truncate', !displayLabel && 'text-text-muted')}>
            {displayLabel || placeholder}
          </span>
        </span>
        <span className="flex items-center gap-1.5 shrink-0">
          {selectedRoute && (
            <span className="text-xs text-text-muted hidden sm:inline">
              {routeBadgeText(selectedRoute)}
            </span>
          )}
          <ChevronDown className={clsx('w-4 h-4 text-text-secondary transition-transform', open && 'rotate-180')} />
        </span>
      </button>

      <button
        type="button"
        disabled={disabled}
        onClick={() => useAppStore.getState().setCurrentPage('routes')}
        title="打开路线编辑器（录制、编辑或生成路线）"
        className={clsx(
          'px-2.5 py-1.5 rounded-lg border border-border shrink-0',
          'bg-bg-secondary hover:bg-bg-hover text-text-secondary transition-colors',
          disabled && 'opacity-60 cursor-not-allowed',
        )}
      >
        <Route className="w-4 h-4" />
      </button>

      {open && !disabled && pos && createPortal(
        <div
          ref={menuRef}
          className="mxu-overlay-surface fixed z-[9999] rounded-lg border border-border bg-bg-primary shadow-xl overflow-hidden flex flex-col animate-in fade-in zoom-in-95 duration-100"
          style={{ top: pos.top, bottom: pos.bottom, left: pos.left, width: pos.width, maxHeight: pos.maxHeight }}
        >
          <div className="p-2 border-b border-border shrink-0">
            <input
              ref={inputRef}
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={handleInputKeyDown}
              placeholder="搜索路线名称、材料、区域或地图…"
              className="w-full px-2.5 py-1.5 text-xs rounded-lg border bg-bg-secondary text-text-primary border-border focus:outline-none focus:border-accent placeholder:text-text-muted"
            />
          </div>

          <div id={listboxId} ref={listboxRef} role="listbox" aria-labelledby={triggerId} className="overflow-y-auto flex-1 min-h-0 divide-y divide-border/50">
            <div>
              <div className="px-3 py-1.5 text-[11px] font-medium text-text-muted bg-bg-secondary/60 flex items-center justify-between">
                <span>我的路线与清单</span>
                <span className="tabular-nums">{filteredUser.length}</span>
              </div>
              {filteredUser.length === 0 ? (
                <div className="px-3 py-2.5 text-xs text-text-muted">
                  {searchQuery.trim() ? '无匹配的自建路线' : '还没有自建路线，可点下方去路线编辑器录制或生成，或直接选用内置路线'}
                </div>
              ) : (
                filteredUser.map((item, idx) => renderRow(item, idx))
              )}
            </div>

            {filteredBuiltin.length > 0 && (
              <div>
                <div className="px-3 py-1.5 text-[11px] font-medium text-text-muted bg-bg-secondary/60 flex items-center justify-between">
                  <span>内置路线库</span>
                  <span className="tabular-nums">{filteredBuiltin.length}</span>
                </div>
                {filteredBuiltin.map((item, idx) => renderRow(item, filteredUser.length + idx))}
              </div>
            )}
          </div>

          <div className="p-1.5 border-t border-border bg-bg-secondary/80 flex items-center justify-between gap-1.5 shrink-0">
            <button
              type="button"
              onClick={() => {
                closeDropdown();
                useAppStore.getState().setCurrentPage('routes');
              }}
              className="flex items-center gap-1 px-2 py-1 rounded text-xs text-accent hover:bg-accent/10 transition-colors"
            >
              <Route className="w-3.5 h-3.5" />
              <span>去路线编辑器录制 / 生成</span>
            </button>
            {isTauri() && (
              <button
                type="button"
                onClick={() => void handleBrowseFile()}
                className="flex items-center gap-1 px-2 py-1 rounded text-xs text-text-secondary hover:bg-bg-hover transition-colors"
              >
                <FolderOpen className="w-3.5 h-3.5" />
                <span>浏览文件…</span>
              </button>
            )}
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}


type MultiFilterCategory = 'all' | 'regional' | 'material' | 'category' | 'collection' | 'user';

const CATEGORY_TABS: Array<{ id: MultiFilterCategory; label: string }> = [
  { id: 'all', label: '全部' },
  { id: 'regional', label: '区域全收集' },
  { id: 'material', label: '单材料路线' },
  { id: 'category', label: '分类速刷' },
  { id: 'collection', label: '推荐合集' },
  { id: 'user', label: '我的路线' },
];

function matchRouteCategory(item: RouteSummary, cat: MultiFilterCategory): boolean {
  if (cat === 'all') return true;
  if (cat === 'user') return !item.builtin;
  if (cat === 'collection') return item.kind === 'route_collection';
  if (cat === 'regional') return item.id.startsWith('gb_all_');
  if (cat === 'material') return item.id.startsWith('gb_mat_') || item.id.startsWith('gb_fish_');
  if (cat === 'category') return item.id.startsWith('gb_cat_');
  return true;
}

export function RouteMultiSelectInput({
  value,
  onChange,
  placeholder = '请选择要依次执行的采集路线（支持多选）…',
  disabled = false,
  className,
}: {
  value: string;
  onChange: (joined: string) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}) {
  const triggerId = useId();
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [category, setCategory] = useState<MultiFilterCategory>('all');
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const { user, builtin, loaded } = useRouteCatalogStore();

  useEffect(() => {
    if (isTauri() && !loaded) {
      void listRoutes().catch(() => {});
    }
  }, [loaded]);

  const runnableBuiltin = useMemo(
    () => builtin.filter((item) => item.runnable !== false),
    [builtin],
  );

  const selectedTokens = useMemo(
    () =>
      value
        .split(/[,;\n]+/)
        .map((s) => s.trim())
        .filter(Boolean),
    [value],
  );

  const selectedEntries = useMemo(
    () =>
      selectedTokens.map((token) => ({
        token,
        summary: findRouteSummaryByValue(token, user, builtin),
        label: formatRouteValueLabel(token, user, builtin),
      })),
    [selectedTokens, user, builtin],
  );

  const commitTokens = useCallback(
    (next: string[]) => {
      const unique: string[] = [];
      for (const t of next) {
        const clean = t.trim();
        if (clean && !unique.includes(clean)) unique.push(clean);
      }
      onChange(unique.join(','));
    },
    [onChange],
  );
  const toggleRoute = useCallback(
    (item: RouteSummary) => {
      const existingIdx = selectedEntries.findIndex(
        (e) => (e.summary && e.summary.id === item.id) || e.token === item.id || e.token === item.path,
      );
      if (existingIdx >= 0) {
        const next = [...selectedTokens];
        next.splice(existingIdx, 1);
        commitTokens(next);
      } else {
        commitTokens([...selectedTokens, item.id]);
      }
    },
    [selectedEntries, selectedTokens, commitTokens],
  );

  const moveIndex = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= selectedTokens.length) return;
    const next = [...selectedTokens];
    const [moved] = next.splice(index, 1);
    next.splice(target, 0, moved);
    commitTokens(next);
  };

  const removeIndex = (index: number) => {
    const next = [...selectedTokens];
    next.splice(index, 1);
    commitTokens(next);
  };

  const allCandidates = useMemo(() => [...user, ...runnableBuiltin], [user, runnableBuiltin]);

  const filteredOptions = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return allCandidates.filter((item) => {
      if (!matchRouteCategory(item, category)) return false;
      if (!q) return true;
      const mapTitle = MAP_TITLES[item.map] ?? item.map ?? '';
      return `${item.title ?? ''} ${item.id} ${mapTitle}`.toLowerCase().includes(q);
    });
  }, [allCandidates, category, searchQuery]);

  const [pos, setPos] = useState<{ top?: number; bottom?: number; left: number; width: number; maxHeight: number } | null>(null);

  const closeDropdown = useCallback(() => {
    setOpen(false);
    setSearchQuery('');
  }, []);

  const updatePos = useCallback(() => {
    if (!triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) {
      closeDropdown();
      return;
    }
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const width = Math.min(Math.max(rect.width, 400), vw - 16);
    let left = rect.right - width;
    if (left < 8) left = Math.max(8, rect.left);
    if (left + width > vw - 8) left = Math.max(8, vw - width - 8);
    const spaceBelow = vh - rect.bottom - 12;
    const spaceAbove = rect.top - 12;
    if (spaceBelow < 260 && spaceAbove > spaceBelow) {
      setPos({ bottom: vh - rect.top + 4, left, width, maxHeight: Math.min(380, Math.max(180, spaceAbove)) });
    } else {
      setPos({ top: rect.bottom + 4, left, width, maxHeight: Math.min(380, Math.max(180, spaceBelow)) });
    }
  }, [closeDropdown]);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    updatePos();
  }, [open, updatePos]);

  useEffect(() => {
    if (!open) return;
    const onOutside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        closeDropdown();
      }
    };
    window.addEventListener('resize', updatePos);
    document.addEventListener('mousedown', onOutside);
    return () => {
      window.removeEventListener('resize', updatePos);
      document.removeEventListener('mousedown', onOutside);
    };
  }, [open, updatePos, closeDropdown]);
  const selectAllFiltered = () => {
    const additions = filteredOptions.slice(0, 32).map((item) => item.id);
    commitTokens([...selectedTokens, ...additions]);
  };

  return (
    <div className={clsx('flex flex-col gap-2 min-w-0', className)}>
      <div className="flex items-center gap-1.5 min-w-0">
        <button
          type="button"
          id={triggerId}
          ref={triggerRef}
          disabled={disabled}
          onClick={() => {
            if (disabled) return;
            if (open) {
              closeDropdown();
            } else {
              if (isTauri()) void listRoutes().catch(() => {});
              setOpen(true);
              setTimeout(() => inputRef.current?.focus(), 0);
            }
          }}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listboxId}
          className={clsx(
            'min-w-0 flex-1 px-3 py-1.5 text-sm rounded-lg border flex items-center justify-between gap-2',
            'bg-bg-secondary text-text-primary border-border',
            'focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/20',
            disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:bg-bg-hover transition-colors',
          )}
        >
          <span className="flex items-center gap-1.5 min-w-0 truncate">
            <Plus className="w-3.5 h-3.5 shrink-0 text-accent" />
            <span className={clsx('truncate', selectedEntries.length === 0 && 'text-text-muted')}>
              {selectedEntries.length > 0
                ? `已选 ${selectedEntries.length} 条采集路线（点击继续勾选或筛选）`
                : placeholder}
            </span>
          </span>
          <ChevronDown className={clsx('w-4 h-4 text-text-secondary transition-transform shrink-0', open && 'rotate-180')} />
        </button>

        <button
          type="button"
          disabled={disabled}
          onClick={() => useAppStore.getState().setCurrentPage('routes')}
          title="打开路线编辑器（3D 预览、录制或生成新路线）"
          className={clsx(
            'px-2.5 py-1.5 rounded-lg border border-border shrink-0',
            'bg-bg-secondary hover:bg-bg-hover text-text-secondary transition-colors',
            disabled && 'opacity-60 cursor-not-allowed',
          )}
        >
          <Route className="w-4 h-4" />
        </button>
      </div>

      {selectedEntries.length > 0 && (
        <div className="rounded-lg border border-border bg-bg-secondary/50 overflow-hidden">
          <div className="px-2.5 py-1 text-[11px] text-text-muted bg-bg-secondary/80 border-b border-border/60 flex items-center justify-between">
            <span>按以下先后顺序依次跑完（共 {selectedEntries.length} 条）</span>
            {!disabled && (
              <button
                type="button"
                onClick={() => commitTokens([])}
                className="flex items-center gap-1 text-text-muted hover:text-error transition-colors"
                title="清空已选路线"
              >
                <Trash2 className="w-3 h-3" />
                <span>清空</span>
              </button>
            )}
          </div>
          <div className="max-h-44 overflow-y-auto divide-y divide-border/40">
            {selectedEntries.map((entry, idx) => (
              <div
                key={`${entry.token}-${idx}`}
                className="px-2.5 py-1.5 text-xs flex items-center justify-between gap-2 hover:bg-bg-hover/60"
              >
                <div className="flex items-center gap-2 min-w-0 flex-1">
                  <span className="w-5 h-5 rounded bg-accent/15 text-accent text-[11px] font-semibold flex items-center justify-center shrink-0 tabular-nums">
                    {idx + 1}
                  </span>
                  <span className="truncate text-text-primary" title={entry.label}>
                    {entry.label}
                  </span>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  {entry.summary && (
                    <span className="text-[11px] text-text-muted mr-1 hidden sm:inline">
                      {routeBadgeText(entry.summary)}
                    </span>
                  )}
                  <button
                    type="button"
                    disabled={disabled || idx === 0}
                    onClick={() => moveIndex(idx, -1)}
                    className="p-0.5 rounded hover:bg-bg-active text-text-secondary disabled:opacity-30"
                    title="上移"
                  >
                    <ArrowUp className="w-3.5 h-3.5" />
                  </button>
                  <button
                    type="button"
                    disabled={disabled || idx === selectedEntries.length - 1}
                    onClick={() => moveIndex(idx, 1)}
                    className="p-0.5 rounded hover:bg-bg-active text-text-secondary disabled:opacity-30"
                    title="下移"
                  >
                    <ArrowDown className="w-3.5 h-3.5" />
                  </button>
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => removeIndex(idx)}
                    className="p-0.5 rounded hover:bg-error/15 text-text-muted hover:text-error"
                    title="移除此路线"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {open && !disabled && pos && createPortal(
        <div
          ref={menuRef}
          className="mxu-overlay-surface fixed z-[9999] rounded-lg border border-border bg-bg-primary shadow-xl overflow-hidden flex flex-col animate-in fade-in zoom-in-95 duration-100"
          style={{ top: pos.top, bottom: pos.bottom, left: pos.left, width: pos.width, maxHeight: pos.maxHeight }}
        >
          <div className="p-2 border-b border-border space-y-2 shrink-0">
            <input
              ref={inputRef}
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.preventDefault();
                  closeDropdown();
                }
              }}
              placeholder="搜索材料名称（如晴日蝶兰、小绵菊）、区域（如花愿镇）或地图…"
              className="w-full px-2.5 py-1.5 text-xs rounded-lg border bg-bg-secondary text-text-primary border-border focus:outline-none focus:border-accent placeholder:text-text-muted"
            />
            <div className="flex flex-wrap items-center gap-1">
              {CATEGORY_TABS.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setCategory(tab.id)}
                  className={clsx(
                    'px-2 py-0.5 rounded-md text-[11px] transition-colors',
                    category === tab.id
                      ? 'bg-accent text-white font-medium'
                      : 'bg-bg-secondary text-text-secondary hover:bg-bg-hover',
                  )}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>

          <div
            id={listboxId}
            role="listbox"
            aria-multiselectable="true"
            className="overflow-y-auto flex-1 min-h-0 divide-y divide-border/40"
          >
            {filteredOptions.length === 0 ? (
              <div className="px-3 py-4 text-xs text-text-muted text-center">
                未找到匹配的路线，请尝试切换分类或修改搜索词
              </div>
            ) : (
              filteredOptions.map((item) => {
                const orderIdx = selectedEntries.findIndex(
                  (e) => (e.summary && e.summary.id === item.id) || e.token === item.id || e.token === item.path,
                );
                const isSelected = orderIdx >= 0;
                return (
                  <button
                    key={`${item.builtin ? 'b' : 'u'}-${item.id}`}
                    type="button"
                    onClick={() => toggleRoute(item)}
                    className={clsx(
                      'w-full px-3 py-2 text-left text-xs flex items-center justify-between gap-2 transition-colors',
                      isSelected
                        ? 'bg-accent/10 text-accent font-medium'
                        : 'text-text-primary hover:bg-bg-hover',
                    )}
                    role="option"
                    aria-selected={isSelected}
                  >
                    <span className="flex items-center gap-2 min-w-0 flex-1">
                      <span
                        className={clsx(
                          'w-4 h-4 rounded border flex items-center justify-center shrink-0 transition-colors',
                          isSelected ? 'bg-accent border-accent text-white' : 'border-border bg-bg-secondary',
                        )}
                      >
                        {isSelected && <Check className="w-3 h-3" />}
                      </span>
                      {item.kind === 'route_collection' && (
                        <Layers className="w-3.5 h-3.5 shrink-0 text-accent-secondary" />
                      )}
                      <span className="truncate">{item.title || item.id}</span>
                    </span>
                    <span className="flex items-center gap-1.5 shrink-0">
                      {isSelected && (
                        <span className="px-1.5 py-0.5 rounded bg-accent/20 text-accent text-[10px] font-semibold tabular-nums">
                          #{orderIdx + 1}
                        </span>
                      )}
                      <span className="text-[11px] text-text-muted">{routeBadgeText(item)}</span>
                    </span>
                  </button>
                );
              })
            )}
          </div>

          <div className="p-1.5 border-t border-border bg-bg-secondary/80 flex items-center justify-between gap-1.5 shrink-0">
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={selectAllFiltered}
                disabled={filteredOptions.length === 0}
                className="px-2 py-1 rounded text-xs text-accent hover:bg-accent/10 transition-colors disabled:opacity-40"
              >
                全选当前筛选 ({Math.min(32, filteredOptions.length)})
              </button>
              <button
                type="button"
                onClick={() => {
                  closeDropdown();
                  useAppStore.getState().setCurrentPage('routes');
                }}
                className="flex items-center gap-1 px-2 py-1 rounded text-xs text-text-secondary hover:bg-bg-hover transition-colors"
              >
                <Route className="w-3.5 h-3.5" />
                <span>路线编辑器</span>
              </button>
            </div>
            <button
              type="button"
              onClick={closeDropdown}
              className="px-2.5 py-1 rounded text-xs bg-accent text-white hover:opacity-90 transition-opacity"
            >
              完成 ({selectedEntries.length})
            </button>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}
