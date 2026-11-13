import { create } from 'zustand';
import { useAppStore } from './appStore';

export type CollectionTab = 'closet' | 'crafting' | 'renewal' | 'blessing';

/** The 收集进度 hub remembers its tab so other pages can link straight to a plan. */
export const useCollectionHubStore = create<{ tab: CollectionTab; setTab: (tab: CollectionTab) => void }>((set) => ({
  tab: 'closet',
  setTab: (tab) => set({ tab }),
}));

export function openCollectionHub(tab: CollectionTab) {
  useCollectionHubStore.getState().setTab(tab);
  useAppStore.getState().setCurrentPage('blessing');
}
