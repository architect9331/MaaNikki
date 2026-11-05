/**
 * 主题管理器
 * 负责加载、切换和应用主题
 */

import type {
  ModeTheme,
  AccentTheme,
  ThemeMode,
  AccentColor,
  AccentInfo,
  CustomAccent,
} from './types';
import { createLogger } from '@/utils/logger';
import { resolveLanguagePreference, type LanguagePreference } from '@/i18n';

const logger = createLogger('Theme');

// 静态导入所有主题预设（Vite 需要静态导入才能正确打包）
import lightTheme from './presets/light.json';
import darkTheme from './presets/dark.json';
import rosegoldAccent from './presets/accents/rosegold.json';
import goldAccent from './presets/accents/gold.json';
import celesteAccent from './presets/accents/celeste.json';
import meadowAccent from './presets/accents/meadow.json';
import starseaAccent from './presets/accents/starsea.json';
import dreamAccent from './presets/accents/dream.json';
import slateAccent from './presets/accents/slate.json';

/** 模式主题映射 */
const modeThemes: Record<ThemeMode, ModeTheme> = {
  light: lightTheme as ModeTheme,
  dark: darkTheme as ModeTheme,
};

/** 强调色主题映射（内置预设） */
const accentThemes: Record<AccentColor, AccentTheme> = {
  rosegold: rosegoldAccent as AccentTheme,
  gold: goldAccent as AccentTheme,
  celeste: celesteAccent as AccentTheme,
  meadow: meadowAccent as AccentTheme,
  starsea: starseaAccent as AccentTheme,
  dream: dreamAccent as AccentTheme,
  slate: slateAccent as AccentTheme,
};

/** 默认强调色；配置中保存的旧预设已移除时也回到这里。 */
const DEFAULT_ACCENT: AccentColor = 'rosegold';

/** 白字在强调色上看不清时改用深色字，自定义强调色同样适用。 */
function onAccentColor(hex: string): string {
  const channel = (start: number) => {
    const value = parseInt(hex.slice(start, start + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  const luminance = 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  return 1.05 / (luminance + 0.05) >= 4.5 ? '#ffffff' : '#1f1c1d';
}

/** 运行时注册的自定义强调色主题
 *
 * 使用索引簽名避免要求預先包含所有內建鍵
 */
const customAccentThemes: { [key: string]: AccentTheme } = {};

/** 记录当前已注册的自定义强调色名称，便于清理 */
const registeredCustomAccentNames = new Set<AccentColor>();

/**
 * 语义色配置
 * 成功、警告、错误按昼夜模式配置，信息色跟随当前模式的强调色。
 */
const semanticColors = {
  light: { success: '#347452', warning: '#92601c', error: '#b94255' },
  dark: { success: '#9ed8b8', warning: '#f0ca8b', error: '#ffa4b0' },
};

/** 当前主题状态 */
let currentMode: ThemeMode = 'light';
let currentAccent: AccentColor = 'rosegold';

/**
 * 将主题配置应用到 CSS 变量
 */
function applyCSSVariables(mode: ModeTheme, accent: AccentTheme, isDark: boolean): void {
  const root = document.documentElement;
  root.dataset.maanikkiAccent = accent.name;

  // 背景色
  root.style.setProperty('--color-bg-primary', mode.bg.primary);
  root.style.setProperty('--color-bg-secondary', mode.bg.secondary);
  root.style.setProperty('--color-bg-tertiary', mode.bg.tertiary);
  root.style.setProperty('--color-bg-hover', mode.bg.hover);
  root.style.setProperty('--color-bg-active', mode.bg.active);

  // 文字色
  root.style.setProperty('--color-text-primary', mode.text.primary);
  root.style.setProperty('--color-text-secondary', mode.text.secondary);
  root.style.setProperty('--color-text-tertiary', mode.text.tertiary);
  root.style.setProperty('--color-text-muted', mode.text.muted);

  // 边框色
  root.style.setProperty('--color-border', mode.border.default);
  root.style.setProperty('--color-border-strong', mode.border.strong);

  // 阴影
  root.style.setProperty('--shadow-sm', mode.shadow.sm);
  root.style.setProperty('--shadow-md', mode.shadow.md);
  root.style.setProperty('--shadow-lg', mode.shadow.lg);

  // 强调色（根据模式选择浅色或深色背景）
  const accentColor = isDark ? accent.dark?.default ?? accent.default : accent.default;
  const accentHover = isDark ? accent.dark?.hover ?? accent.hover : accent.hover;
  root.style.setProperty('--color-accent', accentColor);
  root.style.setProperty('--color-accent-hover', accentHover);
  root.style.setProperty('--color-accent-light', isDark ? accent.lightDark : accent.light);
  root.style.setProperty('--color-on-accent', onAccentColor(accentColor));
  root.style.setProperty(
    '--color-accent-secondary',
    (isDark ? accent.dark?.secondary : accent.secondary) ?? accentColor,
  );

  // 语义色
  const semantic = semanticColors[isDark ? 'dark' : 'light'];
  root.style.setProperty('--color-success', semantic.success);
  root.style.setProperty('--color-info', accentColor);
  root.style.setProperty('--color-warning', semantic.warning);
  root.style.setProperty('--color-error', semantic.error);
  // Logs have their own readable information color even with a dark custom accent.
  root.style.setProperty('--color-log-info', isDark ? '#f0a8bd' : '#9c4560');
}

/**
 * 应用主题
 * @param mode 主题模式 (light/dark)
 * @param accent 强调色名称
 */
export function applyTheme(mode: ThemeMode, accent: AccentColor): void {
  const modeTheme = modeThemes[mode];
  const accentTheme = customAccentThemes[accent] || accentThemes[accent] || accentThemes[DEFAULT_ACCENT];

  if (!modeTheme || !accentTheme) {
    logger.warn(`Theme not found: mode=${mode}, accent=${accent}`);
    return;
  }

  currentMode = mode;
  currentAccent = accent;

  // 切换 dark class
  document.documentElement.classList.toggle('dark', mode === 'dark');

  // 应用 CSS 变量
  applyCSSVariables(modeTheme, accentTheme, mode === 'dark');
}

/**
 * 获取当前主题模式
 */
function getCurrentMode(): ThemeMode {
  return currentMode;
}

/**
 * 获取当前强调色
 */
function getCurrentAccent(): AccentColor {
  return currentAccent;
}

/**
 * 获取所有可用的主题模式
 */
function getAvailableModes(): ThemeMode[] {
  return Object.keys(modeThemes) as ThemeMode[];
}

/**
 * 获取所有可用的强调色
 */
function getAvailableAccents(): AccentColor[] {
  return [
    ...(Object.keys(accentThemes) as AccentColor[]),
    ...(Object.keys(customAccentThemes) as AccentColor[]),
  ];
}

/**
 * 获取强调色信息列表（用于 UI 展示）
 * @param lang 语言代码
 */
export function getAccentInfoList(lang: string, customAccents?: CustomAccent[]): AccentInfo[] {
  const resolvedLang = resolveLanguagePreference(lang as LanguagePreference);
  const langKey = resolvedLang as keyof AccentTheme['label'];
  const base = Object.keys(accentThemes) as AccentColor[];
  const customOrdered = (customAccents ?? []).map((a) => a.name as AccentColor);
  const seen = new Set<string>();
  const all = [...base, ...customOrdered].filter((name) => {
    if (seen.has(String(name))) return false;
    seen.add(String(name));
    // only include if exists (custom accents can be empty during boot)
    return !!(
      customAccentThemes[name as string] || accentThemes[name as keyof typeof accentThemes]
    );
  });

  return all.map((name) => {
    const accent =
      customAccentThemes[name as string] || accentThemes[name as keyof typeof accentThemes];
    if (!accent) {
      // 理论上不会发生，仅做兜底保护
      return {
        name,
        label: String(name),
        color: '#ffffff',
        isCustom: false,
      };
    }

    const isCustom = !!customAccentThemes[name as string];

    return {
      name,
      label: accent.label[langKey] || accent.label['en-US'],
      color: accent.default,
      secondary: accent.secondary,
      isCustom,
    };
  });
}

/**
 * 获取指定强调色的主题配置
 */
function getAccentTheme(accent: AccentColor): AccentTheme | undefined {
  return customAccentThemes[accent] || accentThemes[accent];
}

/**
 * 获取指定模式的主题配置
 */
function getModeTheme(mode: ThemeMode): ModeTheme | undefined {
  return modeThemes[mode];
}

/**
 * 解析主题模式，将 'system' 解析为 'light' 或 'dark'
 */
export function resolveThemeMode(theme: ThemeMode | 'system'): ThemeMode {
  return theme === 'system'
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light'
    : theme;
}

// 默认导出便于使用
export default {
  applyTheme,
  getCurrentMode,
  getCurrentAccent,
  getAvailableModes,
  getAvailableAccents,
  getAccentInfoList,
  getAccentTheme,
  getModeTheme,
  resolveThemeMode,
};

// 重新导出类型
export type {
  ModeTheme,
  AccentTheme,
  ThemeMode,
  AccentColor,
  AccentInfo,
  CustomAccent,
} from './types';

/**
 * 注册自定义强调色
 * - 会在运行时将其加入主题映射中
 */
export function registerCustomAccent(accent: CustomAccent): void {
  const name = accent.name;

  customAccentThemes[name] = {
    name,
    label: accent.label,
    default: accent.colors.default,
    hover: accent.colors.hover,
    light: accent.colors.light,
    lightDark: accent.colors.lightDark,
  };

  registeredCustomAccentNames.add(name);

  // 如果当前正在使用这个强调色，则重新应用主题
  if (currentAccent === name) {
    applyTheme(currentMode, name);
  }
}

/**
 * 取消注册自定义强调色
 */
export function unregisterCustomAccent(name: AccentColor): void {
  delete customAccentThemes[name];
  registeredCustomAccentNames.delete(name);
}

/**
 * 清空所有自定义强调色
 */
export function clearCustomAccents(): void {
  for (const name of registeredCustomAccentNames) {
    delete customAccentThemes[name];
  }
  registeredCustomAccentNames.clear();
}
