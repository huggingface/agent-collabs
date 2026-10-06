// Agents roster, trace counts and leaderboard badges, driven through the real
// app.js with injected state and a scripted fetch.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { loadDashboard, fragment } from './harness.mjs';

function withAgents(window, ids) {
  for (const id of ids) {
    window.eval('agentMap').set(id, { agent: id, hf_user: id, model: 'm', harness: 'h', joined: '2026-10-01 10:00 UTC' });
  }
}

const entry = (agent, score, status, verification) =>
  ({ agent, score, status, verification, filename: `${agent}-${score}.md`, date: '2026-10-01T10:00:00Z' });

describe('leaderboard badges', () => {
  const window = loadDashboard();
  const badge = verification => {
    const tr = window.lbRow(entry('alice', 10, 'agent-run', verification), 1);
    return tr.querySelector('.lb-verified');
  };

  test('verified results carry the badge', () => {
    assert.equal(badge('valid').textContent, '✓ verified');
  });

  test('pending results carry no pill', () => {
    assert.equal(badge('pending'), null);
  });

  test('invalid results keep their pill', () => {
    assert.equal(badge('invalid').textContent, 'invalid');
  });
});

describe('best result per agent', () => {
  const window = loadDashboard();

  test('only leaderboard-eligible agent-run results count', () => {
    window.eval(`leaderboardEntries = ${JSON.stringify([
      entry('alice', 10, 'agent-run', 'valid'),
      entry('alice', 100, 'negative', 'pending'),
      entry('alice', 200, 'baseline', 'pending'),
      entry('alice', 300, 'agent-run', 'invalid'),
      entry('bob', 5, 'negative', 'pending'),
    ])}`);
    const best = window.bestResultByAgent();
    assert.equal(best.get('alice').score, 10);
    assert.equal(best.has('bob'), false);
  });
});

describe('presence: only the roster shows stale, and offline is unflagged', () => {
  const window = loadDashboard();
  withAgents(window, ['alpha', 'beta', 'gamma']);
  window.eval(`watchPresence = { freshS: 60, longpoll: {}, map: {
    alpha: { last_poll_age_s: 10 }, beta: { last_poll_age_s: 120 }, gamma: { last_poll_age_s: 9999 } } }`);

  test('header shows "N online" with no stale/offline chip', () => {
    window.renderTopSubtext();
    const sub = window.document.getElementById('topSubtext');
    assert.equal(sub.querySelector('button'), null);
    assert.doesNotMatch(sub.textContent, /stale|offline/);
    assert.match(sub.textContent, /1 online/);
  });

  test('roster shows pills for online and stale, none for offline', () => {
    window.renderAgentsPanel();
    const pills = [...window.document.querySelectorAll('#agentsBody .presence')].map(p => p.textContent.split(' ')[0]);
    assert.deepEqual(pills.sort(), ['online', 'stale']);
    assert.equal(window.document.querySelector('#agentsBody .presence.offline'), null);
  });
});

describe('trace session counts', () => {
  const ids = [...Array.from({ length: 60 }, (_, i) => `alice/s${i}`), 'bob/s0'];
  const page = ids.slice(0, 50).map(id => ({ agent: id.split('/')[0], id }));

  // Statuses per endpoint; 'down' = network error.
  function scriptFetch(window, { stats = 200, list = 200, ids: idStatus = 200 }) {
    const reply = (status, body) => status === 'down'
      ? Promise.reject(new window.TypeError('down'))
      : Promise.resolve({ ok: status === 200, status, headers: new Map(), json: async () => body });
    window.fetch = async url => {
      if (url === '/api/stats') return reply(stats, { sessions_counted: 61, by_agent: {} });
      if (url === '/api/traces?limit=0') return reply(idStatus, { items: ids, count: ids.length });
      if (url.startsWith('/api/traces')) return reply(list, { items: page, count: ids.length });
      return new Promise(() => {});
    };
  }
  const setup = () => {
    const window = loadDashboard();
    withAgents(window, ['alice', 'bob']);
    const counts = () => window.eval('traceSessions') && Object.fromEntries(window.eval('traceSessions'));
    return { window, counts, hint: () => window.document.getElementById('tracesHint').textContent };
  };

  test('complete counts from the id listing', async () => {
    const { window, counts, hint } = setup();
    scriptFetch(window, {});
    await window.refreshTraces();
    assert.deepEqual(counts(), { alice: 60, bob: 1 });
    assert.match(hint(), /^2 of 2 agents have shared a session/);
  });

  test('id listing failing keeps the last complete snapshot', async () => {
    const { window, counts, hint } = setup();
    scriptFetch(window, {});
    await window.refreshTraces();
    scriptFetch(window, { ids: 500 });
    await window.refreshTraces();
    assert.deepEqual(counts(), { alice: 60, bob: 1 });
    assert.match(hint(), /^2 of 2 agents/);
  });

  test('both listings failing with stats up keeps the snapshot too', async () => {
    const { window, counts } = setup();
    scriptFetch(window, {});
    await window.refreshTraces();
    scriptFetch(window, { list: 'down', ids: 'down' });
    await window.refreshTraces();
    assert.deepEqual(counts(), { alice: 60, bob: 1 });
  });

  test('no complete listing yet: counts stay unknown, not partial', async () => {
    const { window, counts, hint } = setup();
    scriptFetch(window, { ids: 500 });
    await window.refreshTraces();
    assert.equal(counts(), null);
    assert.doesNotMatch(hint(), /agents have shared/);
    const traceCells = [...window.document.querySelectorAll('#agentsBody tr')].map(tr => tr.children[6].textContent);
    assert.deepEqual(traceCells, ['—', '—']);
  });
});
