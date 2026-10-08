/* Offline service regressions: node scripts/test_task_restart.cjs.
 * Uses the client's installed TypeScript compiler; never connects to the game.
 */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const ts = require('../client/node_modules/typescript');

const source = readFileSync(resolve(__dirname, '..', process.argv[2] ||
  'client/src/services/maaService.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
}).outputText;
const developerSource = readFileSync(resolve(__dirname, '..', 'client/src/utils/developerTasks.ts'), 'utf8');
const developerContext = { exports: {} };
vm.runInNewContext(ts.transpileModule(developerSource, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
}).outputText, developerContext);

function instance(running, status = 'succeeded') {
  return {
    connected: true, resource_loaded: true, tasker_inited: true, is_running: running,
    task_run_state: {
      statuses: { previous: status }, mappings: { 101: 'previous' },
      pending_task_ids: [101], current_task_index: 0,
      overall_status: running ? 'Running' : status === 'succeeded' ? 'Succeeded' : 'Failed',
    },
  };
}

function harness(native = true) {
  const snapshot = { instances: { default: instance(false) } };
  const calls = [];
  const dataset = {};
  const settings = { devMode: false, projectInterface: { task: [] } };
  let onStart = async () => [201];
  const start = async (args) => {
    calls.push(args);
    return onStart(args);
  };
  const invoke = async (command, args) => {
    if (command === 'maa_get_all_states') return snapshot;
    if (command === 'maa_start_tasks') return start(args);
    if (command === 'maa_stop_task') {
      snapshot.instances[args.instanceId].is_running = false;
      snapshot.instances[args.instanceId].task_run_state.statuses.previous = 'failed';
      return;
    }
    throw new Error(`Unexpected invoke: ${command}`);
  };
  const modules = {
    '@tauri-apps/api/core': { invoke },
    '@tauri-apps/api/event': {},
    '@/utils/logger': { loggers: { maa: { info() {}, debug() {}, error() {} } } },
    '@/utils/passwordOptionValues': { redactSecretsInText: (text) => text },
    '@/utils/paths': { isTauri: () => native },
    '@/i18n': {},
    '@/utils/backendApi': {
      apiGet: async () => snapshot,
      apiPost: async (url, args) => {
        assert.match(url, /\/tasks\/start$/);
        return { taskIds: await start(args) };
      },
    },
    '@/services/wsService': {},
    '@/stores/appStore': { useAppStore: { getState: () => settings } },
    '@/utils/developerTasks': developerContext.exports,
  };
  const context = {
    exports: {}, Error, crypto: webcrypto,
    document: { documentElement: { dataset } },
    require: (name) => {
      assert.ok(Object.hasOwn(modules, name), `Unexpected import: ${name}`);
      return modules[name];
    },
  };
  vm.runInNewContext(compiled, context, { filename: 'maaService.js' });
  return {
    service: context.exports.maaService, snapshot, calls, dataset, settings,
    setStart: (callback) => { onStart = callback; },
  };
}

const tasks = [{ entry: 'MaaNikki_Xinghai', pipeline_override: '{}' }];

for (const native of [true, false]) {
  const mode = native ? 'desktop' : 'browser';
  test(`${mode}: all metadata-based developer subtasks require mode`, async () => {
    const h = harness(native);
    const definitions = JSON.parse(readFileSync(resolve(__dirname, '..', 'tasks/SubtaskTest.json'), 'utf8')).task;
    h.settings.projectInterface.task = definitions;
    for (const definition of definitions) {
      await assert.rejects(h.service.startTasks('default', [{ entry: definition.entry, pipeline_override: '{}' }]), /开发模式/);
      await assert.rejects(h.service.runTask('default', definition.entry), /任务列表启动/);
    }
    assert.equal(h.calls.length, 0);
    h.settings.devMode = true;
    await h.service.startTasks('default', [{ entry: definitions[0].entry, pipeline_override: '{}' }]);
    assert.equal(h.calls.length, 1);
  });
  test(`${mode}: developer task requires mode and forwards it to the agent`, async () => {
    const h = harness(native);
    const routeTask = [{ entry: 'MaaNikki_RouteTest', pipeline_override: '{}' }];
    await assert.rejects(h.service.startTasks('default', routeTask), /开发模式/);
    await assert.rejects(h.service.runTask('default', 'MaaNikki_RouteTest'), /任务列表启动/);
    assert.equal(h.calls.length, 0);
    h.settings.devMode = true;
    await h.service.startTasks('default', routeTask);
    // Agent environments are forwarded only when an agent is configured.
    await h.service.startTasks('default', routeTask, [{ child_exec: 'python', child_args: [] }]);
    assert.equal((native ? h.calls[1].piEnvs : h.calls[1].pi_envs).PI_MAANIKKI_DEV_MODE, '1');
    h.settings.devMode = false;
    await assert.rejects(h.service.startTasks('default', routeTask), /开发模式/);
    assert.equal(h.calls.length, 2);
  });
  for (const status of ['succeeded', 'failed']) {
    test(`${mode}: restart after ${status} with retained history`, async () => {
      const h = harness(native);
      h.snapshot.instances.default = instance(false, status);
      await h.service.startTasks('default', tasks);
      await h.service.startTasks('default', tasks);
      assert.equal(h.calls.length, 2);
      assert.equal(h.snapshot.instances.default.task_run_state.pending_task_ids.length, 1);
      assert.equal(h.dataset.maanikkiStarting, undefined);
    });
  }
  test(`${mode}: another stopped instance's history does not block`, async () => {
    const h = harness(native);
    h.snapshot.instances.other = instance(false, 'failed');
    await h.service.startTasks('default', tasks);
    assert.equal(h.calls.length, 1);
  });
  test(`${mode}: a running instance still blocks startup`, async () => {
    const h = harness(native);
    h.snapshot.instances.other = instance(true, 'running');
    await assert.rejects(h.service.startTasks('default', tasks), /请先停止正在运行/);
    assert.equal(h.calls.length, 0);
    assert.equal(h.dataset.maanikkiStarting, undefined);
  });
}

test('developer task visibility follows the setting without affecting regular tasks', () => {
  const { isTaskAvailable } = developerContext.exports;
  assert.equal(isTaskAvailable({ entry: 'MaaNikki_RouteTest' }, false), false);
  assert.equal(isTaskAvailable({ developer_only: true }, false), false);
  assert.equal(isTaskAvailable({ entry: 'MaaNikki_RouteTest' }, true), true);
  assert.equal(isTaskAvailable({ entry: 'MaaNikki_Xinghai' }, false), true);
});

test('stop in progress blocks; restart succeeds after framework stops', async () => {
  const h = harness();
  h.snapshot.instances.default = instance(true, 'running');
  await assert.rejects(h.service.startTasks('default', tasks), /请先停止正在运行/);
  await h.service.stopTask('default');
  await h.service.startTasks('default', tasks);
  assert.equal(h.calls.length, 1);
});

test('parallel startup is blocked and lock released after completion', async () => {
  const h = harness();
  let finish;
  h.setStart(() => new Promise((resolve) => { finish = resolve; }));
  const first = h.service.startTasks('default', tasks);
  // Keep an early assertion failure from leaving an unhandled startup rejection.
  first.catch(() => {});
  await assert.rejects(h.service.startTasks('default', tasks), /已有任务正在启动/);
  assert.equal(h.calls.length, 1);
  finish([201]);
  await first;
  assert.equal(h.dataset.maanikkiStarting, undefined);
});

test('backend startup failure releases lock for retry', async () => {
  const h = harness();
  h.setStart(async () => { throw new Error('agent unavailable'); });
  await assert.rejects(h.service.startTasks('default', tasks), /agent unavailable/);
  assert.equal(h.dataset.maanikkiStarting, undefined);
  h.setStart(async () => [202]);
  await h.service.startTasks('default', tasks);
  assert.equal(h.calls.length, 2);
});

test('unknown backend state blocks startup and releases lock', async () => {
  const h = harness();
  h.service.getAllStates = async () => null;
  await assert.rejects(h.service.startTasks('default', tasks), /无法确认任务运行状态/);
  assert.equal(h.calls.length, 0);
  assert.equal(h.dataset.maanikkiStarting, undefined);
});
