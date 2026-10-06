// The "backend unreachable" banner and the result-file score filter, driven
// through the real app.js with a scripted fetch.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { loadDashboard } from './harness.mjs';

// Answer each API path with the status scripted for it; null = network error.
function scriptFetch(window, statuses) {
  window.fetch = async url => {
    const path = new URL(url, 'https://dashboard.test/').pathname;
    const status = statuses[path];
    if (status === null) throw new window.TypeError('network down');
    const body = path === '/api/channels' ? { items: [] } : { fresh_s: 120, watching: {} };
    return { ok: status >= 200 && status < 300, status, headers: new Map(), json: async () => body };
  };
}

describe('backend banner', () => {
  const setup = () => {
    const window = loadDashboard();
    const banner = window.document.getElementById('backendBanner');
    const poll = async (statuses, order = ['refreshWatching', 'refreshChannels']) => {
      scriptFetch(window, statuses);
      for (const fn of order) await window[fn]();
    };
    return { banner, poll };
  };

  test('one endpoint recovering does not hide the other one\'s outage', async () => {
    const { banner, poll } = setup();
    await poll({ '/api/watching': 502, '/api/channels': 200 });
    assert.equal(banner.hidden, false);
    assert.match(banner.textContent, /presence may be stale/);
    assert.doesNotMatch(banner.textContent, /channels/);
  });

  test('the reverse: watching succeeding keeps a channels outage visible', async () => {
    const { banner, poll } = setup();
    await poll({ '/api/watching': 200, '/api/channels': null }, ['refreshChannels', 'refreshWatching']);
    assert.equal(banner.hidden, false);
    assert.match(banner.textContent, /channels may be stale/);
  });

  test('names every failing endpoint, and clears only when all recover', async () => {
    const { banner, poll } = setup();
    await poll({ '/api/watching': 502, '/api/channels': 502 });
    assert.match(banner.textContent, /presence and channels may be stale/);
    await poll({ '/api/watching': 200, '/api/channels': 502 });
    assert.equal(banner.hidden, false);
    assert.match(banner.textContent, /— channels may be stale/);
    await poll({ '/api/watching': 200, '/api/channels': 200 });
    assert.equal(banner.hidden, true);
  });

  test('503 means "no backend configured", not an outage', async () => {
    const { banner, poll } = setup();
    await poll({ '/api/watching': 503, '/api/channels': 503 });
    assert.equal(banner.hidden, true);
  });
});

describe('result scores', () => {
  const window = loadDashboard();
  const parse = score => window.parseResultFile(
    '20260930-120000_agent-a_run.md', `---\nscore: ${score}\n---\nbody\n`);

  test('ordinary positive scores are kept', () => {
    assert.equal(parse('0.873').score, 0.873);
    assert.equal(parse('1_234.5').score, 1234.5);
  });

  // Matches the backend validator, leaderboard and verifier, which all
  // require a positive score.
  for (const bad of ['0', '-1', 'abc', 'Infinity', '-Infinity', '1e309', '9'.repeat(400)]) {
    test(`rejects ${bad.length > 20 ? 'a 400-digit integer' : bad}`, () => {
      assert.equal(parse(bad), null);
    });
  }
});
