import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolveStartTaskTarget } from '../src/utils/startTaskTarget.ts';

const instances = [
  { id: 'inst-daily', name: '日常' },
  { id: 'inst-event', name: '活动' },
];

test('事件携带实例 ID 时按 ID 执行，即使活动实例已被切换', () => {
  const target = resolveStartTaskTarget(instances, 'inst-daily', instances[1]);
  assert.equal(target.kind, 'requested');
  assert.equal(target.kind === 'requested' ? target.instance.id : null, 'inst-daily');
});

test('事件携带的实例已被删除时停止，不回退到当前活动实例', () => {
  const target = resolveStartTaskTarget(instances, 'inst-removed', instances[1]);
  assert.deepEqual(target, { kind: 'missing', instanceId: 'inst-removed' });
});

test('未携带实例 ID 时保持原语义：使用当前活动实例', () => {
  const target = resolveStartTaskTarget(instances, undefined, instances[1]);
  assert.equal(target.kind, 'active');
  assert.equal(target.kind === 'active' ? target.instance.id : null, 'inst-event');
});

test('未携带实例 ID 且没有活动实例时为空', () => {
  assert.deepEqual(resolveStartTaskTarget(instances, null, undefined), { kind: 'none' });
});

test('请求的实例恰好是当前活动实例时仍按请求执行', () => {
  const target = resolveStartTaskTarget(instances, 'inst-event', instances[1]);
  assert.equal(target.kind, 'requested');
  assert.equal(target.kind === 'requested' ? target.instance.id : null, 'inst-event');
});

test('空字符串 ID 视为指定了但不存在的实例', () => {
  assert.deepEqual(resolveStartTaskTarget(instances, '', instances[0]), {
    kind: 'missing',
    instanceId: '',
  });
});

test('实例列表为空时请求的 ID 判为不存在', () => {
  assert.deepEqual(resolveStartTaskTarget([], 'inst-daily', undefined), {
    kind: 'missing',
    instanceId: 'inst-daily',
  });
});
