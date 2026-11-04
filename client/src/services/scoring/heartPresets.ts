import { ATTRIBUTES, HEART_GROUPS, type HeartSettings } from './types';

export interface HeartUnlockNode {
  id: number;
  order: number;
  region: 'wishfield' | 'starsea';
  label: string;
  apply: (heart: HeartSettings) => void;
}

export interface HeartNodeSelection {
  unlocked: number[];
  flatLevels: number[]; // 0..10 for each of the 5 attributes (+10 per level)
}

export const WISHFIELD_NODES: HeartUnlockNode[] = [
  { id: 200001, order: 1, region: 'wishfield', label: '衣服属性 +6%', apply: (h) => { h.stat[2] += 6; } },
  { id: 200002, order: 2, region: 'wishfield', label: '外套属性 +6%', apply: (h) => { h.stat[1] += 6; } },
  { id: 200003, order: 3, region: 'wishfield', label: '发型属性 +6%', apply: (h) => { h.stat[0] += 6; } },
  { id: 200004, order: 4, region: 'wishfield', label: '袜子属性 +6%', apply: (h) => { h.stat[3] += 6; } },
  { id: 200005, order: 5, region: 'wishfield', label: '鞋子属性 +6%', apply: (h) => { h.stat[4] += 6; } },
  { id: 200006, order: 6, region: 'wishfield', label: '饰品属性 +6%', apply: (h) => { h.stat[5] += 6; } },
  { id: 200007, order: 7, region: 'wishfield', label: '全服装属性 +2%（一）', apply: (h) => { for (let i = 0; i < 6; i++) h.stat[i] += 2; } },
  { id: 200008, order: 8, region: 'wishfield', label: '全服装属性 +2%（二）', apply: (h) => { for (let i = 0; i < 6; i++) h.stat[i] += 2; } },
  { id: 200009, order: 9, region: 'wishfield', label: '全服装属性 +2%（三）', apply: (h) => { for (let i = 0; i < 6; i++) h.stat[i] += 2; } },
  { id: 200010, order: 10, region: 'wishfield', label: '发型评分 +8%', apply: (h) => { h.part[0] += 8; } },
  { id: 200011, order: 11, region: 'wishfield', label: '外套评分 +8%', apply: (h) => { h.part[1] += 8; } },
  { id: 200012, order: 12, region: 'wishfield', label: '衣服评分 +8%', apply: (h) => { h.part[2] += 8; } },
  { id: 200013, order: 13, region: 'wishfield', label: '袜子评分 +8%', apply: (h) => { h.part[3] += 8; } },
  { id: 200014, order: 14, region: 'wishfield', label: '鞋子评分 +8%', apply: (h) => { h.part[4] += 8; } },
  { id: 200015, order: 15, region: 'wishfield', label: '饰品评分 +8%', apply: (h) => { h.part[5] += 8; } },
];

export const STARSEA_NODES: HeartUnlockNode[] = [
  { id: 210001, order: 1, region: 'starsea', label: '全关卡展示评分 +2%（一）', apply: (h) => { h.display += 2; } },
  { id: 210002, order: 2, region: 'starsea', label: '全关卡展示评分 +2%（二）', apply: (h) => { h.display += 2; } },
  { id: 210003, order: 3, region: 'starsea', label: '全关卡展示评分 +2%（三）', apply: (h) => { h.display += 2; } },
  { id: 210004, order: 4, region: 'starsea', label: '全关卡展示评分 +2%（四）', apply: (h) => { h.display += 2; } },
  { id: 210005, order: 5, region: 'starsea', label: '全关卡展示评分 +2%（五）', apply: (h) => { h.display += 2; } },
];

export const ALL_HEART_NODES: HeartUnlockNode[] = [...WISHFIELD_NODES, ...STARSEA_NODES];

export function computeHeartFromNodes(selection: HeartNodeSelection): HeartSettings {
  const heart: HeartSettings = {
    flat: selection.flatLevels.slice(0, 5).map((lv) => Math.max(0, Math.min(10, Math.round(lv))) * 10),
    stat: Array(6).fill(0),
    part: Array(6).fill(0),
    display: 0,
  };
  const active = new Set(selection.unlocked);
  for (const node of ALL_HEART_NODES) {
    if (active.has(node.id)) node.apply(heart);
  }
  return heart;
}

/** Infer node checkboxes and levels from saved HeartSettings so existing accounts reflect their state. */
export function inferNodesFromHeart(heart: HeartSettings): HeartNodeSelection {
  const unlocked: number[] = [];
  const minStat = Math.min(...HEART_GROUPS.map((_, i) => heart.stat[i] ?? 0));
  const globalCount = Math.min(3, Math.floor((minStat >= 6 ? minStat - 6 : minStat) / 2));
  for (let i = 0; i < globalCount; i++) unlocked.push(200007 + i);
  const baseBonus = globalCount * 2;
  const statNodeIds = [200003, 200002, 200001, 200004, 200005, 200006]; // hair, coat, dress, sock, shoe, acc
  for (let group = 0; group < 6; group++) {
    if ((heart.stat[group] ?? 0) >= baseBonus + 6) unlocked.push(statNodeIds[group]);
    if ((heart.part[group] ?? 0) >= 8) unlocked.push(200010 + group);
  }
  const starseaCount = Math.min(5, Math.floor((heart.display ?? 0) / 2));
  for (let i = 0; i < starseaCount; i++) unlocked.push(210001 + i);
  const flatLevels = ATTRIBUTES.map((_, i) => Math.max(0, Math.min(10, Math.round((heart.flat[i] ?? 0) / 10))));
  return { unlocked, flatLevels };
}

export interface RealmMaterialTarget {
  realmType: 'Jihua' | 'Monster';
  optionKey: 'JihuaTarget' | 'MonsterTarget';
  caseName: string;
  label: string;
}

/** Map all 13 wardrobe upgrade material IDs to their exact Realm Challenge options. */
export const MATERIAL_REALM_TARGETS: Record<string, RealmMaterialTarget> = {
  '50361309': { realmType: 'Jihua', optionKey: 'JihuaTarget', caseName: 'Bubble', label: '素材激化 · 闪亮泡泡' },
  '992617511': { realmType: 'Jihua', optionKey: 'JihuaTarget', caseName: 'Bling', label: '素材激化 · 噗灵' },
  '3448636403': { realmType: 'Jihua', optionKey: 'JihuaTarget', caseName: 'Thread', label: '素材激化 · 丝线' },
  '2431685856': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Climb', label: '魔物试炼 · 攀越（典雅 1.0）' },
  '102807154': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Shock', label: '魔物试炼 · 震荡（帅气 1.0）' },
  '201359675': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Wind', label: '魔物试炼 · 风舞（甜美 1.0）' },
  '2201002702': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Spin', label: '魔物试炼 · 飞旋（性感 1.0）' },
  '2299563415': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Flip', label: '魔物试炼 · 翻转（清新 1.0）' },
  '2628798194': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Assault', label: '魔物试炼 · 强袭（清新 2.0）' },
  '333473796': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Violence', label: '魔物试炼 · 暴行（帅气 2.0）' },
  '432042829': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Weightless', label: '魔物试炼 · 失重（甜美 2.0）' },
  '530602518': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Lost', label: '魔物试炼 · 迷踪（典雅 2.0）' },
  '2530246569': { realmType: 'Monster', optionKey: 'MonsterTarget', caseName: 'Expel', label: '魔物试炼 · 驱逐（性感 2.0）' },
};
