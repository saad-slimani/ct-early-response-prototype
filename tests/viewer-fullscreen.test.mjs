import assert from 'node:assert/strict';
import {test} from 'node:test';
import {ViewerFullscreen} from '../app/static/viewer-controls.mjs';

function fixture({supported = true, denied = false, webkit = false} = {}) {
  const document = new EventTarget();
  const classes = new Set(), changes = [];
  const target = {ownerDocument: document, classList: {toggle(name, value) {
    if (value) classes.add(name); else classes.delete(name);
  }}};
  const element = webkit ? 'webkitFullscreenElement' : 'fullscreenElement';
  const event = webkit ? 'webkitfullscreenchange' : 'fullscreenchange';
  let requests = 0, exits = 0, fallbacks = 0;
  if (supported) {
    target[webkit ? 'webkitRequestFullscreen' : 'requestFullscreen'] = async () => {
      requests++;
      if (denied) throw new Error('Not allowed');
      document[element] = target;
      document.dispatchEvent(new Event(event));
    };
  }
  document[webkit ? 'webkitExitFullscreen' : 'exitFullscreen'] = async () => {
    exits++;
    document[element] = null;
    document.dispatchEvent(new Event(event));
  };
  const controller = new ViewerFullscreen(target, value => changes.push(value), () => fallbacks++);
  return {document, target, controller, classes, changes,
    stats: () => ({requests, exits, fallbacks})};
}

test('requests native fullscreen synchronously and restores the workspace on toggle', async () => {
  const f = fixture();
  const entering = f.controller.toggle();
  assert.equal(f.stats().requests, 1, 'request must retain click user activation');
  await entering;
  assert.equal(f.controller.active, true);
  assert.equal(f.classes.has('annotation-fullscreen'), true);
  assert.deepEqual(f.changes.at(-1), {active: true, expanded: false, pending: false});
  await f.controller.toggle();
  assert.equal(f.controller.active, false);
  assert.equal(f.classes.size, 0);
  assert.equal(f.stats().exits, 1);
});

test('browser Escape updates the label and layout without another button click', async () => {
  const f = fixture();
  await f.controller.toggle();
  f.document.fullscreenElement = null;
  f.document.dispatchEvent(new Event('fullscreenchange'));
  assert.deepEqual(f.changes.at(-1), {active: false, expanded: false, pending: false});
  assert.equal(f.classes.size, 0);
});

for (const options of [{supported: false}, {denied: true}]) {
  test(`provides an escapable expanded view when fullscreen is ${options.denied ? 'denied' : 'unavailable'}`, async () => {
    const f = fixture(options);
    await f.controller.toggle();
    assert.equal(f.controller.active, true);
    assert.deepEqual(f.changes.at(-1), {active: true, expanded: true, pending: false});
    assert.equal(f.stats().fallbacks, 1);
    await f.controller.exit();
    assert.equal(f.classes.size, 0);
    assert.equal(f.stats().exits, 0);
  });
}

test('supports prefixed Safari fullscreen events', async () => {
  const f = fixture({webkit: true});
  await f.controller.toggle();
  assert.equal(f.controller.active, true);
  await f.controller.exit();
  assert.equal(f.controller.active, false);
});

test('ignores repeated toggles during the browser transition', async () => {
  const f = fixture();
  const entering = f.controller.toggle();
  await f.controller.toggle();
  await entering;
  assert.deepEqual(f.stats(), {requests: 1, exits: 0, fallbacks: 0});
  await f.controller.destroy();
  assert.equal(f.classes.size, 0);
});

test('unloading the iframe exits fullscreen and removes event listeners', async () => {
  const f = fixture();
  await f.controller.toggle();
  await f.controller.destroy();
  const count = f.changes.length;
  f.document.dispatchEvent(new Event('fullscreenchange'));
  await f.controller.toggle();
  assert.equal(f.changes.length, count);
  assert.equal(f.stats().requests, 1);
  assert.equal(f.controller.active, false);
});

test('unloading during a pending request cannot strand the workspace in fullscreen', async () => {
  const f = fixture();
  let finish;
  f.target.requestFullscreen = () => new Promise(resolve => {finish = () => {
    f.document.fullscreenElement = f.target;
    resolve();
  };});
  const entering = f.controller.toggle();
  await f.controller.destroy();
  finish();
  await entering;
  assert.equal(f.controller.active, false);
  assert.equal(f.classes.size, 0);
});

test('exit does not close fullscreen owned by another component', async () => {
  const f = fixture();
  f.document.fullscreenElement = {};
  await f.controller.exit();
  assert.equal(f.stats().exits, 0);
});
