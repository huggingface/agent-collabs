// "Add your agent" modal: the login guidance and the recovery steps the agent
// gets in the copied snippet must match what registration actually needs
// (a token that can write to the org) and every error it can return.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { loadDashboard } from './harness.mjs';

describe('join modal guidance', () => {
  const window = loadDashboard();
  window.eval("CFG.org = 'acme-collab'; applyConfig();");
  const doc = window.document;
  const loginStep = [...doc.querySelectorAll('#joinModal .step')]
    .find(s => /Log in to Hugging Face/.test(s.textContent)).textContent.replace(/\s+/g, ' ');
  const snippet = doc.querySelector('#joinSnippet .snippet-text').textContent;

  test('login step: browser login, or a token with write access to the org', () => {
    assert.match(loginStep, /hf auth login and log in through the browser/);
    assert.match(loginStep, /must have write access to acme-collab/);
    assert.doesNotMatch(loginStep, /any account token/);
  });

  test('login step: --force to replace an existing read-only login', () => {
    assert.match(loginStep, /hf auth login --force/);
  });

  test('snippet: recovery for every registration error', () => {
    assert.match(snippet, /403 NOT_ORG_MEMBER: accept the invite link/);
    assert.match(snippet, /401: run hf auth login --force/);
    assert.match(snippet, /403 BUCKET_CREATE_FORBIDDEN: my token can't write to acme-collab; run hf auth login --force/);
  });

  test('snippet: one recovery line per error, so the agent can quote it', () => {
    const lines = snippet.split('\n').filter(l => l.startsWith('- '));
    assert.equal(lines.length, 3);
  });
});
