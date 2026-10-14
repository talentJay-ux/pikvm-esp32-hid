import {test} from 'node:test';
import assert from 'node:assert/strict';
import {InputQueue} from '../tools/input.mjs';
const tick = () => new Promise(resolve => setImmediate(resolve));

test('slow transport coalesces motion and preserves key-release ordering', async () => {
  let release;
  const sent = [];
  const first = new Promise(resolve => { release = resolve; });
  const queue = new InputQueue(async data => { sent.push(data); if (sent.length === 1) await first; }, () => 'a', assert.fail);
  queue.push({kind: 'move', dx: 1, dy: 0});
  for (let i = 0; i < 1000; i++) queue.push({kind: 'move', dx: 1, dy: -1});
  queue.push({kind: 'key', code: 78, down: false});
  release(); await tick();
  assert.equal(sent.length, 3);
  assert.deepEqual(sent[1], {kind: 'move', dx: 127, dy: -127});
  assert.deepEqual(sent[2], {kind: 'key', code: 78, down: false});
});

test('pause and rearm discard queued events from the old lease', async () => {
  let lease = 'old', release;
  const sent = [];
  const first = new Promise(resolve => { release = resolve; });
  const queue = new InputQueue(async (data, token) => { sent.push({data, token}); if (sent.length === 1) await first; }, () => lease, assert.fail);
  queue.push({kind: 'key', code: 1, down: true});
  queue.push({kind: 'move', dx: 50, dy: 0});
  lease = null; queue.clear(); lease = 'new';
  queue.push({kind: 'key', code: 2, down: true});
  release(); await tick();
  assert.equal(sent.length, 2);
  assert.equal(sent[1].token, 'new');
  assert.equal(sent[1].data.code, 2);
});

test('old in-flight failure cannot cancel a newly armed session', async () => {
  let lease = 'old', fail;
  const sent = [], failures = [];
  const first = new Promise((resolve, reject) => { fail = reject; });
  const queue = new InputQueue(async (data, token) => { sent.push(token); if (sent.length === 1) await first; }, () => lease, (error, token) => failures.push(token));
  queue.push({kind: 'key', code: 1, down: true});
  lease = 'new'; queue.push({kind: 'key', code: 2, down: true});
  fail(new Error('old connection')); await tick();
  assert.deepEqual(sent, ['old', 'new']);
  assert.deepEqual(failures, ['old']);
});
