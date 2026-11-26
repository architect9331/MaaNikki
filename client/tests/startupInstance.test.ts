import assert from 'node:assert/strict';
import { test } from 'node:test';
import { selectStartupInstance } from '../src/utils/startupInstance.ts';

const instances = [
  { id: 'inst-daily', name: '日常' },
  { id: 'inst-event', name: '活动' },
];

test('命令行实例名唯一命中时使用该实例', () => {
  assert.deepEqual(selectStartupInstance(instances, '日常', 'inst-event'), {
    kind: 'cli',
    instanceId: 'inst-daily',
  });
});

test('命令行实例名不存在时不回退到配置的默认实例', () => {
  const selection = selectStartupInstance(instances, '不存在的账号', 'inst-daily');
  assert.deepEqual(selection, { kind: 'invalid', reason: 'not-found', name: '不存在的账号' });
  assert.equal('instanceId' in selection, false);
});

test('命令行实例名重名时不启动，避免驱动错误账号', () => {
  const duplicated = [...instances, { id: 'inst-daily-2', name: '日常' }];
  const selection = selectStartupInstance(duplicated, '日常', 'inst-event');
  assert.deepEqual(selection, { kind: 'invalid', reason: 'ambiguous', name: '日常' });
  assert.equal('instanceId' in selection, false);
});

test('命令行传入空实例名视为无效，不回退到配置实例', () => {
  assert.deepEqual(selectStartupInstance(instances, '', 'inst-daily'), {
    kind: 'invalid',
    reason: 'not-found',
    name: '',
  });
});

test('实例名按原文精确匹配，不忽略大小写或空白', () => {
  assert.equal(selectStartupInstance(instances, '日常 ', 'inst-daily').kind, 'invalid');
  assert.equal(selectStartupInstance([{ id: 'x', name: 'Daily' }], 'daily', 'x').kind, 'invalid');
});

test('未指定命令行实例时使用配置中的自动执行实例', () => {
  assert.deepEqual(selectStartupInstance(instances, null, 'inst-event'), {
    kind: 'config',
    instanceId: 'inst-event',
  });
});

test('未指定命令行实例且未配置自动执行实例时不启动', () => {
  assert.deepEqual(selectStartupInstance(instances, null, undefined), { kind: 'none' });
  assert.deepEqual(selectStartupInstance(instances, null, ''), { kind: 'none' });
});

test('配置的自动执行实例已被删除时仍返回该 id，由调用方按 id 查找后跳过', () => {
  assert.deepEqual(selectStartupInstance(instances, null, 'inst-removed'), {
    kind: 'config',
    instanceId: 'inst-removed',
  });
});
