import type { AccountView, CollectionKey } from '@/stores/accountStateStore';

export const collectionSlots = ['头部', '手部', '脚部'];
export const collectionKey = (row: CollectionKey) => `${row.name}/${row.slot}/${row.color}`;

/** Shared by the progress page and its header shortcut. */
export function getBlessingProgress(data: AccountView | null) {
  const sets = data?.catalog?.sets.filter((s) => s.rarity === 5 && s.collection_target !== false) ?? [];
  const target = sets.reduce((n, s) => n + s.colors.length * collectionSlots.length, 0);
  const snapshot = data?.blessing;
  const operation = snapshot?.pursuit?.pending_operation;
  const rewardPending = operation != null && typeof operation === 'object'
    && 'kind' in operation && operation.kind === 'realm'
    && 'realm' in operation && operation.realm === 'bless'
    && 'status' in operation && operation.status === 'submitted_unconfirmed'
    && 'facts' in operation && operation.facts === null;
  const protectionUncertain = snapshot?.pursuit?.protection_valid === false;
  const definitions = new Map(sets.map((s) => [s.name, s]));
  const isValidKey = (row: CollectionKey) =>
    collectionSlots.includes(row.slot) && Boolean(definitions.get(row.name)?.colors.includes(row.color));

  const owned = new Set<string>();
  const partEmbryos: Record<string, number> = { ...(snapshot?.part_embryos ?? {}) };
  let known = false;
  if (snapshot && snapshot.catalog_version === data?.catalog?.version && target > 0) {
    const add = (row: CollectionKey) => {
      if (isValidKey(row)) {
        owned.add(collectionKey(row));
      }
    };
    const pursuit = snapshot.pursuit;
    if (snapshot.scan?.identity_complete && snapshot.locks?.complete) {
      snapshot.locks.groups?.filter((r) => r.level === 10 && r.locked_count > 0).forEach(add);
      snapshot.unlocked_max_colors?.forEach(add);
      known = true;
    } else if (pursuit?.existing_colors_checked && pursuit.parts?.length === sets.length * 3) {
      pursuit.parts.forEach((part) => {
        part.locked_colors.forEach((color) => add({ ...part, color }));
        if (typeof part.embryos === 'number') {
          partEmbryos[`${part.name}/${part.slot}`] = part.embryos;
        }
      });
      snapshot.unlocked_max_colors?.forEach(add);
      known = true;
    } else if (snapshot.protection?.complete) {
      snapshot.protection.keepers?.forEach(add);
      snapshot.unlocked_max_colors?.forEach(add);
      known = true;
    }
  }

  const pearpalOwned = new Set<string>();
  const pearpalNotebook = data?.observations?.pearpal_notebook;
  if (pearpalNotebook?.five_star_colors && target > 0) {
    for (const row of pearpalNotebook.five_star_colors) {
      if (isValidKey(row)) {
        pearpalOwned.add(collectionKey(row));
      }
    }
  }

  const localKnown = known;
  if (!known && pearpalOwned.size > 0) {
    pearpalOwned.forEach((k) => owned.add(k));
    known = true;
  } else if (known && snapshot?.pursuit?.mode === 'farm_only') {
    pearpalOwned.forEach((k) => owned.add(k));
  }

  let pearpalOnlyCount = 0;
  let localOnlyCount = 0;
  if (localKnown && pearpalOwned.size > 0) {
    for (const k of pearpalOwned) {
      if (!owned.has(k)) pearpalOnlyCount++;
    }
    for (const k of owned) {
      if (!pearpalOwned.has(k)) localOnlyCount++;
    }
  }

  return {
    sets, target, owned, known, localKnown,
    partEmbryos,
    pearpalCheck: {
      synced: Boolean(pearpalNotebook?.updated_at && pearpalNotebook?.five_star_colors),
      updatedAt: pearpalNotebook?.updated_at,
      pearpalCount: pearpalOwned.size,
      pearpalOnlyCount,
      localOnlyCount,
    },
    percentage: known ? Math.round(owned.size / target * 100) : null,
    rewardPending: rewardPending && !protectionUncertain,
    uncertain: (operation != null && !rewardPending) || protectionUncertain,
  };
}
