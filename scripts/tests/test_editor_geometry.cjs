/* Behavioral tests for the editor's local geometry helpers. No browser/server. */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = readFileSync(path.join(__dirname, '../../assets/webapp/index.html'), 'utf8');
function section(start, end) {
  const from = html.indexOf(start);
  const to = html.indexOf(end, from);
  assert.ok(from >= 0 && to > from, 'Editor helper must be present');
  return html.slice(from, to);
}
let lane;
const context = vm.createContext({ document: { querySelector: () => lane } });
vm.runInContext(
  section('function studioSnapPipPosition(', '\nlet _vamPipDrag=') + '\n' +
  section('function studioAutoScrollTimeline(', '\n/* The shared playhead'), context
);
const snap = (...args) => JSON.parse(JSON.stringify(context.studioSnapPipPosition(...args)));
const canvas = { w: 100, h: 100 };
function scroller(overrides = {}) {
  lane = {
    scrollLeft: 100, scrollTop: 71, scrollWidth: 1000, clientWidth: 400,
    getBoundingClientRect: () => ({ left: 100, right: 500, width: 400, top: 50, bottom: 150 }),
    ...overrides,
  };
  return lane;
}

test('snapping tolerance excludes the exact boundary', () => {
  assert.deepEqual(snap(17.999, 23, canvas, 1, 20, 20),
    { x: 10, y: 23, guideX: 0, guideY: null });
  assert.deepEqual(snap(18, 23, canvas, 1, 20, 20),
    { x: 18, y: 23, guideX: null, guideY: null });
});

test('snapping keeps an eight-display-pixel tolerance at different zooms', () => {
  assert.equal(snap(13.9, 23, canvas, 2, 20, 20).x, 10);
  assert.equal(snap(14, 23, canvas, 2, 20, 20).guideX, null);
  assert.equal(snap(23, 13.9, canvas, 2, 20, 20).y, 10);
});

test('equal distances preserve target and rectangle-anchor priorities', () => {
  assert.deepEqual(snap(-45, 23, canvas, 1, 20, 20),
    { x: -40, y: 23, guideX: -50, guideY: null });
  assert.deepEqual(snap(0, 0, canvas, 1, 100, 100),
    { x: 0, y: 0, guideX: -50, guideY: -50 });
});

test('axes snap independently and ignore invalid geometry', () => {
  assert.deepEqual(snap(23, 17, canvas, 1, 20, 20),
    { x: 23, y: 10, guideX: null, guideY: 0 });
  for (const unit of [0, -1, Infinity, NaN]) {
    assert.deepEqual(snap(12, 23, canvas, unit, 20, 20),
      { x: 12, y: 23, guideX: null, guideY: null });
  }
  assert.equal(snap(12, 23, { w: 0, h: 100 }, 1, 20, 20).guideX, null);
});

test('edge scrolling scales with pointer penetration and never scrolls vertically', () => {
  scroller();
  assert.equal(context.studioAutoScrollTimeline(124, 100), -11);
  assert.equal(lane.scrollLeft, 89);
  assert.equal(lane.scrollTop, 71);
  scroller();
  assert.equal(context.studioAutoScrollTimeline(52, 100), -44);
});

test('scroll delta reflects the available range, including short tracks', () => {
  scroller({ scrollLeft: 595 });
  assert.equal(context.studioAutoScrollTimeline(500, 100), 5);
  assert.equal(lane.scrollLeft, 600);
  scroller({ scrollLeft: 0 });
  assert.equal(context.studioAutoScrollTimeline(100, 100), 0);
  scroller({ scrollLeft: 0, scrollWidth: 200 });
  assert.equal(context.studioAutoScrollTimeline(500, 100), 0);
});

test('scrolling stops outside the vertical band or away from either edge', () => {
  scroller();
  assert.equal(context.studioAutoScrollTimeline(124, 162), -11);
  scroller();
  assert.equal(context.studioAutoScrollTimeline(124, 162.001), 0);
  assert.equal(context.studioAutoScrollTimeline(300, 100), 0);
  assert.equal(lane.scrollLeft, 100);
});

test('overlapping edge zones prioritize the left side', () => {
  scroller({ clientWidth: 60,
    getBoundingClientRect: () => ({ left: 100, right: 160, width: 60, top: 50, bottom: 150 }) });
  assert.equal(context.studioAutoScrollTimeline(130, 100), -8);
});

test('scrolling handles missing elements and malformed coordinates without writes', () => {
  lane = null;
  assert.equal(context.studioAutoScrollTimeline(100, 100), 0);
  scroller();
  assert.equal(context.studioAutoScrollTimeline(NaN, 100), 0);
  assert.equal(context.studioAutoScrollTimeline(124, Infinity), 0);
  assert.equal(lane.scrollLeft, 100);
});

test('per-frame speed scaling remains bounded', () => {
  scroller();
  assert.equal(context.studioAutoScrollTimeline(100, 100, 0.1), -2);
  scroller();
  assert.equal(context.studioAutoScrollTimeline(100, 100, 10), -22);
});

test('all inline application scripts still parse', () => {
  let count = 0;
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (/\bsrc\s*=/.test(match[1]) || !match[2].trim()) continue;
    new vm.Script(match[2]);
    count++;
  }
  assert.ok(count > 0);
});
