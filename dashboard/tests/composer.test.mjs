// The composer's idempotency key, driven through the real app.js: a retry of
// the same draft after a lost answer reuses the key (so the backend replays
// the first post instead of posting twice); a sent or edited draft gets a new one.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { loadDashboard } from './harness.mjs';

const SENT = {
  item: {
    filename: '20261012-090000-000_human-alice.md',
    content: '---\nagent: human-alice\ntype: user\ntimestamp: 2026-10-12 09:00 UTC\n---\n\nhello\n',
  },
  mentions_delivered: [],
};

function setup() {
  const window = loadDashboard();
  window.eval("me = { logged_in: true, user: 'alice', avatar: '', is_organizer: false }");
  const posts = [];
  const answers = [];  // one per POST: a status + body, or 'network' to throw
  window.fetch = async (url, init = {}) => {
    if (init.method !== 'POST') return new Promise(() => {});
    posts.push(JSON.parse(init.body));
    const a = answers.shift();
    if (a === 'network') throw new window.TypeError('Failed to fetch');
    return { ok: a.status < 300, status: a.status, headers: new Map(), json: async () => a.body };
  };
  const input = window.document.getElementById('humanMessage');
  const form = window.document.getElementById('messageComposer');
  const status = window.document.getElementById('composerStatus');
  const send = async (text, answer) => {
    answers.push(answer);
    input.value = text;
    form.dispatchEvent(new window.Event('submit', { cancelable: true }));
    for (let i = 0; i < 100 && window.eval('postingMessage'); i++) await new Promise(r => setTimeout(r, 5));
  };
  return { posts, send, input, status };
}

const UNCONFIRMED = {
  status: 504,
  body: { detail: "Couldn't confirm the message; it may have gone through. Check the feed before sending it again." },
};

const NOT_SENT = {
  status: 503,
  body: { detail: "Couldn't reach the server; this attempt wasn't sent. Try again shortly." },
};

describe('composer idempotency key', () => {
  test('a retry after an unconfirmed post reuses the key and keeps the draft', async () => {
    const { posts, send, input, status } = setup();
    await send('hello', UNCONFIRMED);
    assert.equal(input.value, 'hello');  // the draft survives the failure
    assert.match(status.textContent, /may have gone through/);
    await send('hello', { status: 200, body: SENT });  // the backend's replay
    assert.equal(posts.length, 2);
    assert.match(posts[0].idempotency_key, /^[A-Za-z0-9_-]{1,64}$/);
    assert.equal(posts[1].idempotency_key, posts[0].idempotency_key);
    assert.equal(input.value, '');  // the replay is a success
  });

  test('a network failure says the outcome is unknown, and the retry reuses the key', async () => {
    const { posts, send, status } = setup();
    await send('hello', 'network');
    assert.match(status.textContent, /may have gone through/);
    await send('hello', { status: 201, body: SENT });
    assert.equal(posts[1].idempotency_key, posts[0].idempotency_key);
  });

  test('the same text sent again after a success is a new message', async () => {
    const { posts, send } = setup();
    await send('hello', { status: 201, body: SENT });
    await send('hello', { status: 201, body: SENT });
    assert.notEqual(posts[1].idempotency_key, posts[0].idempotency_key);
  });

  test('editing the draft after a failure starts a new key', async () => {
    const { posts, send } = setup();
    await send('hello', UNCONFIRMED);
    await send('hello, world', { status: 201, body: SENT });
    assert.notEqual(posts[1].idempotency_key, posts[0].idempotency_key);
  });

  test('a later failure of an unconfirmed draft keeps the earlier-attempt warning', async () => {
    const { posts, send, status } = setup();
    await send('hello', UNCONFIRMED);
    await send('hello', NOT_SENT);  // this attempt never left
    assert.equal(posts[1].idempotency_key, posts[0].idempotency_key);
    assert.match(status.textContent, /this attempt wasn't sent/);
    assert.match(status.textContent, /An earlier attempt may have gone through/);
    await send('hello', 'network');
    await send('hello', NOT_SENT);  // a network failure is uncertain too
    assert.match(status.textContent, /An earlier attempt may have gone through/);
  });

  test('the earlier-attempt warning ends with a success or an edited draft', async () => {
    const { send, status } = setup();
    await send('hello', NOT_SENT);  // a first attempt: no earlier one to warn about
    assert.doesNotMatch(status.textContent, /earlier attempt/);
    await send('hello', UNCONFIRMED);
    await send('hello, edited', NOT_SENT);  // a new draft: its own key, no earlier attempt
    assert.doesNotMatch(status.textContent, /earlier attempt/);
    await send('again', UNCONFIRMED);
    await send('again', { status: 200, body: SENT });
    await send('again', NOT_SENT);  // after the success this is a new message
    assert.doesNotMatch(status.textContent, /earlier attempt/);
  });

  test('the error stays until the draft is edited', async () => {
    const { send, input, status } = setup();
    await send('hello', UNCONFIRMED);
    assert.match(status.textContent, /may have gone through/);
    assert.ok(status.classList.contains('error'));
    input.value = 'hello!';
    input.dispatchEvent(new input.ownerDocument.defaultView.Event('input'));
    assert.match(status.textContent, /posting as alice/);
  });
});
