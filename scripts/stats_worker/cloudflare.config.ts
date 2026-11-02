import { bindings, defineConfig } from 'cf/config';

export default defineConfig({
  worker: {
    name: 'maanikki-stats',
    entrypoint: './src/index.js',
    compatibilityDate: '2026-10-31',
    workersDev: true,
    domains: ['stats.maanikki.cn'],
    env: {
      DB: bindings.d1({ id: '4cfe6a99-593b-43f6-b97c-0fa76dd45afb', name: 'maanikki-stats' }),
    },
  },
});
