// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';

const MOUSE_KEYS = ['MouseLeft', 'MouseMiddle', 'MouseRight', 'Thumb1', 'Thumb2'];
const MODIFIERS = new Set(['Shift', 'Control', 'Alt', 'Meta']);
const NAMED_KEYS: Record<string, string> = {
  Escape: 'Esc', Space: 'Space', Enter: 'Enter', NumpadEnter: 'Enter',
  ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right',
  ShiftLeft: 'LShift', ShiftRight: 'RShift', ControlLeft: 'LCtrl', ControlRight: 'RCtrl',
  AltLeft: 'LAlt', AltRight: 'RAlt', MetaLeft: 'LWin', MetaRight: 'RWin',
  Backspace: 'Backspace', Tab: 'Tab', CapsLock: 'CapsLock',
  Insert: 'Insert', Delete: 'Delete', Home: 'Home', End: 'End',
  PageUp: 'PageUp', PageDown: 'PageDown', Pause: 'Pause', ScrollLock: 'ScrollLock',
  NumLock: 'NumLock', PrintScreen: 'PrintScreen', ContextMenu: 'Apps',
  Backquote: 'Backquote', Minus: 'Minus', Equal: 'Equal', BracketLeft: 'BracketLeft',
  BracketRight: 'BracketRight', Backslash: 'Backslash', Semicolon: 'Semicolon',
  Quote: 'Quote', Comma: 'Comma', Period: 'Period', Slash: 'Slash',
  NumpadAdd: 'NumpadAdd', NumpadSubtract: 'NumpadSubtract',
  NumpadMultiply: 'NumpadMultiply', NumpadDivide: 'NumpadDivide', NumpadDecimal: 'NumpadDecimal',
};

export function gameKeyName(code: string): string | null {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^Numpad[0-9]$/.test(code) || /^F([1-9]|1[0-9]|2[0-4])$/.test(code)) return code;
  return NAMED_KEYS[code] ?? null;
}

export function GameKeyInput({ value, onChange, placeholder, disabled, className }: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}) {
  const [capturing, setCapturing] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const captureRef = useRef<HTMLDivElement>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (!capturing || disabled) return;
    const held = new Set<string>();
    let pendingKey: { code: string; name: string } | null = null;
    let chord = false;
    let pendingMouse: string | null = null;
    document.documentElement.dataset.maanikkiKeyCapture = 'active';
    const finish = (name: string) => {
      onChangeRef.current(name);
      setCapturing(false);
    };
    const block = (event: Event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    const keyDown = (event: KeyboardEvent) => {
      block(event);
      if (event.repeat) return;
      const wasHeld = held.has(event.code);
      held.add(event.code);
      // OS modifier state also catches modifiers held before opening the recorder.
      const modifier = MODIFIERS.has(event.key);
      const modified = event.ctrlKey || event.altKey || event.shiftKey || event.metaKey;
      const modifierCount = [event.ctrlKey, event.altKey, event.shiftKey, event.metaKey].filter(Boolean).length;
      if (held.size > 1 || modifierCount > 1 || (!modifier && modified) || wasHeld || pendingMouse) {
        chord = true;
        pendingKey = null;
        pendingMouse = null;
        setError('暂不支持组合键，请松开所有按键后再按一个键。');
        return;
      }
      if (chord) return;
      const name = gameKeyName(event.code);
      if (!name) {
        setError('未识别这个按键，请重试或手动填写键名。');
        return;
      }
      pendingKey = { code: event.code, name };
      setError('松开该按键以完成录入。');
    };
    const keyUp = (event: KeyboardEvent) => {
      block(event);
      held.delete(event.code);
      if (!chord && pendingKey?.code === event.code) {
        finish(pendingKey.name);
      }
      if (held.size === 0) {
        chord = false;
        pendingKey = null;
      }
    };
    const mouseDown = (event: MouseEvent) => {
      // The cancel button and clicks outside the pad must not change a binding.
      if (!(event.target instanceof Node) || !captureRef.current?.contains(event.target)) return;
      block(event);
      if (held.size || event.ctrlKey || event.altKey || event.shiftKey || event.metaKey
          || (event.buttons & (event.buttons - 1)) !== 0) {
        chord = true;
        pendingKey = null;
        pendingMouse = null;
        setError('暂不支持组合键，请松开键盘后再点击鼠标。');
        return;
      }
      const name = MOUSE_KEYS[event.button];
      if (name) pendingMouse = name;
    };
    const mouseUp = (event: MouseEvent) => {
      if (!pendingMouse) {
        if (event.target instanceof Node && captureRef.current?.contains(event.target)) block(event);
        if (event.buttons === 0 && held.size === 0) chord = false;
        return;
      }
      block(event);
      const name = pendingMouse;
      pendingMouse = null;
      // Suppress the ensuing middle/right/side-button browser action as the dialog closes.
      const suppressClick = (next: Event) => { next.preventDefault(); next.stopImmediatePropagation(); };
      window.addEventListener('auxclick', suppressClick, { capture: true, once: true });
      window.addEventListener('contextmenu', suppressClick, { capture: true, once: true });
      window.setTimeout(() => {
        window.removeEventListener('auxclick', suppressClick, true);
        window.removeEventListener('contextmenu', suppressClick, true);
      }, 500);
      finish(name);
    };
    const suppressMouseDefault = (event: Event) => {
      if (event.target instanceof Node && captureRef.current?.contains(event.target)) block(event);
    };
    const lostFocus = () => {
      setCapturing(false); // Never keep listening after switching to the game.
    };
    window.addEventListener('keydown', keyDown, true);
    window.addEventListener('keyup', keyUp, true);
    window.addEventListener('mousedown', mouseDown, true);
    window.addEventListener('mouseup', mouseUp, true);
    window.addEventListener('auxclick', suppressMouseDefault, true);
    window.addEventListener('contextmenu', suppressMouseDefault, true);
    window.addEventListener('blur', lostFocus);
    captureRef.current?.focus();
    return () => {
      // Also block delayed native global-shortcut callbacks from this captured keystroke.
      document.documentElement.dataset.maanikkiKeyCapture = String(Date.now() + 1000);
      window.removeEventListener('keydown', keyDown, true);
      window.removeEventListener('keyup', keyUp, true);
      window.removeEventListener('mousedown', mouseDown, true);
      window.removeEventListener('mouseup', mouseUp, true);
      window.removeEventListener('auxclick', suppressMouseDefault, true);
      window.removeEventListener('contextmenu', suppressMouseDefault, true);
      window.removeEventListener('blur', lostFocus);
      inputRef.current?.focus();
    };
  }, [capturing, disabled]);

  return (
    <div className={clsx('flex items-center gap-2 min-w-0', className)}>
      <input
        ref={inputRef}
        type="text"
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        aria-label="键名，可手动填写"
        className="w-full min-w-0 px-3 py-1.5 text-sm rounded-md border bg-bg-secondary text-text-primary border-border focus:outline-none focus:ring-1 focus:border-accent focus:ring-accent/20"
      />
      <button
        type="button"
        disabled={disabled}
        onClick={() => { setError(''); setCapturing(true); }}
        className="shrink-0 px-3 py-1.5 text-sm rounded-md border border-border text-accent hover:bg-bg-active disabled:opacity-50"
      >录入</button>
      {capturing && !disabled && createPortal(
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/45 p-6" role="dialog" aria-modal="true" aria-label="录入游戏键位">
          <div className="w-full max-w-sm rounded-xl border border-border bg-bg-secondary p-5 shadow-2xl">
            <h3 className="text-base font-semibold text-text-primary">录入游戏键位</h3>
            <p className="mt-2 text-sm text-text-secondary">按下一个键盘按键，或在下方区域点击鼠标键。</p>
            <div ref={captureRef} tabIndex={-1} className="mt-4 rounded-lg border-2 border-dashed border-accent/60 bg-bg-tertiary p-7 text-center text-sm text-text-primary outline-none">
              等待按键…<br /><span className="text-xs text-text-muted">支持左右键、中键、侧键 1 和侧键 2</span>
            </div>
            <p className="mt-3 text-xs text-text-muted">Esc 也可录入；取消请点击按钮。切换窗口会取消录入。</p>
            {error && <p className="mt-2 text-xs text-error" role="status">{error}</p>}
            <div className="mt-4 flex justify-end"><button type="button" onClick={() => setCapturing(false)} className="rounded-md border border-border px-4 py-1.5 text-sm text-text-primary hover:bg-bg-active">取消</button></div>
          </div>
        </div>, document.body,
      )}
    </div>
  );
}
