import { bindings, defineConfig } from 'cf/config';

export default defineConfig({
  worker: {
    name: 'maanikki-stats',
    entrypoint: './src/index.js',
    compatibilityDate: '2026-10-31',
    workersDev: true,
    domains: ['stats.maanikki.cn'],
    env: {
      // Replace with the id printed when creating the D1 database.
      DB: bindings.d1({ id: 'REPLACE_WITH_D1_ID', name: 'maanikki-stats' }),
    },
  },
});
