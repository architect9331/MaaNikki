import { bindings, defineConfig } from 'cf/config';
import * as entrypoint from './src/index.ts' with { type: 'cf-worker' };

export default defineConfig({
  worker: {
    name: 'maanikki-update-mirror',
    compatibilityDate: '2026-10-28',
    entrypoint,
    workersDev: false,
    previewUrls: false,
    domains: ['updates.arc9331.top'],
    observability: {
      enabled: true,
      headSamplingRate: 0.01,
      traces: { enabled: true, headSamplingRate: 0.01 },
    },
    env: {
      RELEASES: bindings.r2({ name: 'maanikki-updates' }),
    },
  },
});
