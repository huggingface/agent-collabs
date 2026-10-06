// Leaderboard table: every attempt by default, an opt-in best-per-agent view
// with the backend's eligibility, and deterministic tie order. Driven through
// the real renderLeaderboard.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { loadDashboard } from './harness.mjs';

// stamp: the server stamp the backend gives the result file name.
const entry = (agent, score, stamp, { status = 'agent-run', verification = 'pending' } = {}) => ({
  agent, score, status, verification,
  filename: `${stamp}_${agent}.md`, date: '2026-10-01T10:00:00Z', method: 'm', run: '', links: [],
});

// [agent, score] for each table row, top to bottom (invalid section excluded).
function rows(window) {
  return [...window.document.querySelectorAll('#lbBody tr:not(.invalid-row):not(.lb-invalid-sep)')]
    .filter(tr => tr.querySelector('td[data-lb-agent]'))
    .map(tr => [tr.querySelector('td[data-lb-agent]').dataset.lbAgent, Number(tr.children[1].firstChild.textContent.replace(/,/g, ''))]);
}

function render(window, entries, { bestPerAgent = false } = {}) {
  const box = window.document.getElementById('lbBestPerAgent');
  box.checked = bestPerAgent;
  window.renderLeaderboard(entries);
  return rows(window);
}

describe('default view', () => {
  test('a fresh visitor sees every attempt, failed experiments included', () => {
    const window = loadDashboard();
    assert.equal(window.document.getElementById('lbBestPerAgent').checked, false);
    const shown = render(window, [
      entry('alice', 10, '20261001-100000-000'),
      entry('alice', 100, '20261001-110000-000', { status: 'negative' }),
      entry('alice', 5, '20261001-120000-000'),
    ]);
    assert.deepEqual(shown, [['alice', 100], ['alice', 10], ['alice', 5]]);
    assert.equal(window.document.getElementById('lbStatus').textContent, '3 attempts');
  });

  test('a saved "best per agent" choice is restored, and changing it is saved', () => {
    const window = loadDashboard({ storage: { collab_lb_best_per_agent: '1' } });
    const box = window.document.getElementById('lbBestPerAgent');
    assert.equal(box.checked, true);
    box.checked = false;
    box.dispatchEvent(new window.Event('change'));
    assert.equal(window.localStorage.getItem('collab_lb_best_per_agent'), '0');
  });
});

describe('best per agent', () => {
  const window = loadDashboard();

  test('a failed experiment never hides the agent-run best', () => {
    const shown = render(window, [
      entry('alice', 10, '20261001-100000-000', { verification: 'valid' }),
      entry('alice', 100, '20261001-110000-000', { status: 'negative' }),
    ], { bestPerAgent: true });
    assert.deepEqual(shown, [['alice', 10]]);
    assert.equal(window.document.getElementById('lbStatus').textContent, '1 agents · 2 attempts');
  });

  test('agents with only failed experiments get no row; baselines always stay', () => {
    const shown = render(window, [
      entry('alice', 10, '20261001-100000-000'),
      entry('bob', 50, '20261001-100500-000', { status: 'negative' }),
      entry('baseline', 7, '20261001-090000-000', { status: 'baseline' }),
    ], { bestPerAgent: true });
    assert.deepEqual(shown, [['alice', 10], ['baseline', 7]]);
  });

  test('equal best scores go to the earlier submission', () => {
    assert.deepEqual([...window.bestPerAgent([
      entry('alice', 10, '20261001-120000-000'), entry('alice', 10, '20261001-100000-000'),
    ])].map(e => e.filename), ['20261001-100000-000_alice.md']);
  });
});

describe('tie order', () => {
  // Alice=1 at t0, Bob=10 at t1, Alice=10 at t2: Bob reached 10 first.
  const entries = () => [
    entry('alice', 1, '20261001-100000-000'),
    entry('bob', 10, '20261001-110000-000'),
    entry('alice', 10, '20261001-120000-000'),
  ];

  test('collapsing keeps the earlier submission first among equal scores', () => {
    const window = loadDashboard();
    assert.deepEqual(render(window, entries(), { bestPerAgent: true }), [['bob', 10], ['alice', 10]]);
  });

  test('every-attempt view orders ties the same way, whatever the arrival order', () => {
    const window = loadDashboard();
    assert.deepEqual(render(window, entries().reverse()), [['bob', 10], ['alice', 10], ['alice', 1]]);
  });

  test('same score and stamp fall back to agent id', () => {
    const window = loadDashboard();
    const shown = render(window, [
      entry('zed', 10, '20261001-100000-000'), entry('amy', 10, '20261001-100000-000'),
    ]);
    assert.deepEqual(shown, [['amy', 10], ['zed', 10]]);
  });
});
