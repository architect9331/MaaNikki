import { bindings, defineConfig, triggers } from 'cf/config';

export default defineConfig({
  worker: {
    name: 'maanikki-qq-notifier',
    entrypoint: './src/index.js',
    compatibilityDate: '2026-10-31',
    compatibilityFlags: ['nodejs_compat'],
    workersDev: true,
    domains: ['qqbot.maanikki.cn'],
    env: {
      DB: bindings.d1({ id: '15439779-61ad-4ac0-af92-997212d4590d', name: 'maanikki-qq-notifier' }),
      REPOSITORY: bindings.text('architect9331/MaaNikki'),
      SHOW_LINKS: bindings.text('false'),
      DELIVERY_ENABLED: bindings.text('true'),
      QQ_APP_ID: bindings.secret(),
      QQ_APP_SECRET: bindings.secret(),
      GITHUB_WEBHOOK_SECRET: bindings.secret(),
      BINDING_CODE: bindings.secret(),
      ADMIN_TOKEN: bindings.secret(),
    },
    triggers: [triggers.scheduled({ schedule: '* * * * *' })],
    observability: {
      enabled: true,
      traces: { enabled: true },
    },
  },
});
