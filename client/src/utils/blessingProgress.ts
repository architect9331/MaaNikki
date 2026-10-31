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
  const owned = new Set<string>();
  let known = false;
  if (snapshot && snapshot.catalog_version === data?.catalog?.version && target > 0) {
    const definitions = new Map(sets.map((s) => [s.name, s]));
    const add = (row: CollectionKey) => {
      if (collectionSlots.includes(row.slot) && definitions.get(row.name)?.colors.includes(row.color)) {
        owned.add(collectionKey(row));
      }
    };
    const pursuit = snapshot.pursuit;
    if (snapshot.scan?.identity_complete && snapshot.locks?.complete) {
      snapshot.locks.groups?.filter((r) => r.level === 10 && r.locked_count > 0).forEach(add);
      known = true;
    } else if (pursuit?.existing_colors_checked && pursuit.parts?.length === sets.length * 3) {
      pursuit.parts.forEach((part) => part.locked_colors.forEach((color) => add({ ...part, color })));
      known = true;
    } else if (snapshot.protection?.complete) {
      snapshot.protection.keepers?.forEach(add);
      known = true;
    }
  }
  return {
    sets, target, owned, known,
    percentage: known ? Math.round(owned.size / target * 100) : null,
    rewardPending: rewardPending && !protectionUncertain,
    uncertain: (operation != null && !rewardPending) || protectionUncertain,
  };
}
