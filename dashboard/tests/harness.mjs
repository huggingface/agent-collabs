// Loads the real dashboard (index.html + app.js) into jsdom with the same
// marked and DOMPurify builds the page pins on the CDN, so tests exercise the
// shipped rendering path rather than a copy of it.
import { readFileSync } from 'node:fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const STATIC = new URL('../static/', import.meta.url);
const MODULES = new URL('node_modules/', import.meta.url);

export const indexHtml = readFileSync(new URL('index.html', STATIC), 'utf8');
const appJs = readFileSync(new URL('app.js', STATIC), 'utf8');
const LIBS = {
  marked: readFileSync(new URL('marked/marked.min.js', MODULES), 'utf8'),
  DOMPurify: readFileSync(new URL('dompurify/dist/purify.min.js', MODULES), 'utf8'),
};

// dompurify's `exports` hides its package.json from require(), so read it off disk.
export function installedVersion(pkg) {
  return JSON.parse(readFileSync(new URL(`${pkg}/package.json`, MODULES), 'utf8')).version;
}

// `libs` picks which CDN globals load before app.js, to cover the fallback
// taken when one of them fails to load. `storage` seeds localStorage first,
// as a returning visitor's saved preferences would be.
export function loadDashboard({ libs = ['marked', 'DOMPurify'], storage = {} } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', e => errors.push(e));
  // Without `resources: 'usable'` jsdom never fetches the <script src> and
  // <link> tags, so only the scripts injected below run.
  const { window } = new JSDOM(indexHtml, {
    runScripts: 'dangerously', url: 'https://dashboard.test/', virtualConsole,
  });
  // Boot fetches /api/config; leave it pending so no request or render races
  // the assertions.
  window.fetch = () => new Promise(() => {});
  for (const [k, v] of Object.entries(storage)) window.localStorage.setItem(k, v);
  // Classic <script>s, as in the page, so app.js's top-level let/const (CFG,
  // agentMap) are globals that window.eval can reach.
  for (const src of [...libs.map(name => LIBS[name]), appJs]) {
    const s = window.document.createElement('script');
    s.textContent = src;
    window.document.body.append(s);
  }
  if (errors.length) throw errors[0];
  return window;
}

// Parse rendered HTML into a fragment, the way the page's innerHTML sinks
// will, so assertions see the DOM the browser would build.
export function fragment(window, html) {
  const t = window.document.createElement('template');
  t.innerHTML = html;
  return t.content;
}
