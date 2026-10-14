import {test} from 'node:test';
import assert from 'node:assert/strict';
import {toAbsolute, LatestMotion} from '../tools/pointer.mjs';

const natural = {width: 1280, height: 720};

test('corners and centre of the drawn image map to the full firmware range', () => {
  const rect = {left: 10, top: 20, width: 1280, height: 720};
  assert.deepEqual(toAbsolute(rect, natural, 10, 20), {x: 0, y: 0});
  assert.deepEqual(toAbsolute(rect, natural, 1290, 740), {x: 65535, y: 65535});
  assert.deepEqual(toAbsolute(rect, natural, 650, 380), {x: 32768, y: 32768});
});

test('letterbox bars from object-fit contain are outside the screen', () => {
  // A 4:3 box holding a 16:9 frame leaves bars above and below.
  const rect = {left: 0, top: 0, width: 1280, height: 960};
  assert.equal(toAbsolute(rect, natural, 640, 100), null);
  assert.deepEqual(toAbsolute(rect, natural, 0, 120), {x: 0, y: 0});
  assert.deepEqual(toAbsolute(rect, natural, 1280, 840), {x: 65535, y: 65535});
});

test('a congested link sends only the newest position', () => {
  const sent = [];
  let congested = true;
  const motion = new LatestMotion(p => sent.push(p), () => congested);
  for (let i = 0; i < 1000; i++) motion.push({x: i, y: i});
  assert.equal(motion.flush(), false);
  congested = false;
  assert.equal(motion.flush(), true);
  assert.equal(motion.flush(), false);
  assert.deepEqual(sent, [{x: 999, y: 999}]);
});

test('clear drops a pending position so a click is not followed by a stale move', () => {
  const sent = [];
  const motion = new LatestMotion(p => sent.push(p), () => false);
  motion.push({x: 1, y: 1});
  motion.clear();
  assert.equal(motion.flush(), false);
  assert.deepEqual(sent, []);
});
