// renderMarkdownInline is the single point where agent-authored markdown
// becomes HTML for innerHTML. These pin both halves of its contract: hostile
// markup never survives, and ordinary markdown, @mentions and artifact refs
// still render.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { loadDashboard, fragment, indexHtml, installedVersion } from './harness.mjs';

const DANGEROUS_TAGS = ['script', 'iframe', 'object', 'embed', 'base', 'meta', 'link', 'frame', 'frameset'];
const URL_ATTRS = ['href', 'src', 'xlink:href', 'action', 'formaction', 'srcdoc', 'background', 'poster'];

// Nothing executable may reach the DOM: no dangerous elements, no on* handler,
// no script-bearing URL.
function assertInert(window, html) {
  const root = fragment(window, html);
  for (const el of root.querySelectorAll('*')) {
    const tag = el.localName.toLowerCase();
    assert.ok(!DANGEROUS_TAGS.includes(tag), `<${tag}> survived in: ${html}`);
    for (const { name, value } of el.attributes) {
      assert.ok(!/^on/i.test(name), `${name}= survived on <${tag}> in: ${html}`);
      if (URL_ATTRS.includes(name.toLowerCase())) {
        const v = value.replace(/[\s\u0000-\u001f]/g, '').toLowerCase();
        assert.ok(!/^(javascript|vbscript|data:text\/html)/.test(v), `${name}="${value}" survived in: ${html}`);
      }
    }
  }
}

describe('pinned libraries', () => {
  // The suite runs against node_modules; if index.html moves to another
  // version without package.json following, these tests stop covering what
  // ships.
  for (const [pkg, cdnName] of [['marked', 'marked'], ['dompurify', 'dompurify']]) {
    test(`${pkg} in package.json matches the index.html CDN pin`, () => {
      const pinned = indexHtml.match(new RegExp(`cdn\\.jsdelivr\\.net/npm/${cdnName}@([^/"]+)/`));
      assert.ok(pinned, `no ${cdnName} <script> in index.html`);
      assert.equal(installedVersion(pkg), pinned[1]);
    });
  }

  test('DOMPurify loads before app.js', () => {
    const purify = indexHtml.indexOf('dompurify@');
    const app = indexHtml.indexOf('src="/app.js"');
    assert.ok(purify !== -1 && app !== -1 && purify < app);
  });
});

describe('renderMarkdownInline: hostile input', () => {
  const window = loadDashboard();
  const render = window.renderMarkdownInline;

  const payloads = {
    'script tag': '<script>alert(1)</script>',
    'img onerror': '<img src=x onerror=alert(1)>',
    'svg onload': '<svg onload=alert(1)><circle r=1></circle></svg>',
    'svg animate onbegin': '<svg><animate onbegin=alert(1) attributeName=x dur=1s></svg>',
    'iframe': '<iframe src="https://evil.test"></iframe>',
    'iframe srcdoc': '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    'object/embed': '<object data="x.swf"></object><embed src="x.swf">',
    'base href': '<base href="https://evil.test/">',
    'meta refresh': '<meta http-equiv="refresh" content="0;url=https://evil.test">',
    'markdown javascript: link': '[click](javascript:alert(1))',
    'markdown javascript: link, entity-obfuscated': '[click](jav&#x09;ascript:alert(1))',
    'markdown javascript: autolink': '<javascript:alert(1)>',
    'raw javascript: anchor': '<a href="javascript:alert(1)">x</a>',
    'raw javascript: anchor, mixed case + whitespace': '<a href=" JaVaScRiPt:alert(1)">x</a>',
    'markdown data: link': '[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)',
    'markdown image javascript:': '![x](javascript:alert(1))',
    'img onerror via markdown title': '![x](https://a.test/x.png "\\" onerror=\\"alert(1))',
    'form formaction': '<form><button formaction="javascript:alert(1)">go</button></form>',
    'details ontoggle': '<details open ontoggle=alert(1)>x</details>',
    'body onload': '<body onload=alert(1)>',
    'mXSS: svg/style reparse': '<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">',
    'mXSS: math/mglyph': '<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>',
    'mXSS: noscript reparse': '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
    'mXSS: form nesting': '<form><math><mtext></form><form><mglyph><style></math><img src onerror=alert(1)>',
    // The linkers re-parse sanitized output through <template>.innerHTML; a
    // mention or artifact ref inside an attribute must not reopen it.
    'mention inside attribute': '<a title="@alice onmouseover=alert(1)">x</a> @bob',
    'artifact ref inside attribute': '<img alt="artifacts/x.png onerror=alert(1)" src=x> artifacts/y.png',
    'handler smuggled past linkers': '<p>@alice artifacts/a.png<img src=x onerror=alert(1)></p>',
  };

  for (const [name, input] of Object.entries(payloads)) {
    test(name, () => assertInert(window, render(input)));
  }

  test('script content is dropped, not escaped into the text', () => {
    const out = render('before <script>alert(1)</script> after');
    assert.doesNotMatch(out, /alert/);
    assert.match(out, /before/);
    assert.match(out, /after/);
  });

  // applyConfig puts the org-configured tagline through the same renderer
  // into a live innerHTML sink.
  test('tagline sink stays inert', () => {
    window.eval(`CFG.tagline = ${JSON.stringify(payloads['img onerror'] + payloads['markdown javascript: link'])}; applyConfig();`);
    const tagline = window.document.getElementById('challengeTagline');
    assert.equal(tagline.hidden, false);
    assertInert(window, tagline.innerHTML);
    assert.ok(tagline.querySelector('img'), 'img kept, minus its handler');
  });

  test('inline code keeps markup as text', () => {
    const out = render('`<img src=x onerror=alert(1)>`');
    const code = fragment(window, out).querySelector('code');
    assert.equal(code.textContent, '<img src=x onerror=alert(1)>');
    assertInert(window, out);
  });
});

describe('renderMarkdownInline: ordinary markdown', () => {
  const window = loadDashboard();
  const render = window.renderMarkdownInline;
  const q = (html, sel) => fragment(window, html).querySelector(sel);

  test('empty input renders nothing', () => {
    assert.equal(render(''), '');
    assert.equal(render(null), '');
  });

  test('emphasis, inline code, and lists', () => {
    const out = render('**bold** _it_ `code`\n\n- one\n- two');
    assert.equal(q(out, 'strong').textContent, 'bold');
    assert.equal(q(out, 'em').textContent, 'it');
    assert.equal(q(out, 'code').textContent, 'code');
    assert.equal(fragment(window, out).querySelectorAll('li').length, 2);
  });

  test('fenced code block', () => {
    const out = render('```py\nprint("<hi>")\n```');
    assert.equal(q(out, 'pre code').textContent, 'print("<hi>")\n');
  });

  test('GFM table', () => {
    const out = render('| a | b |\n|---|---|\n| 1 | 2 |');
    assert.equal(fragment(window, out).querySelectorAll('td').length, 2);
  });

  test('https links keep their href', () => {
    const out = render('[docs](https://huggingface.co/docs)');
    assert.equal(q(out, 'a').getAttribute('href'), 'https://huggingface.co/docs');
  });

  test('images with https src survive', () => {
    const out = render('![plot](https://huggingface.co/x.png)');
    assert.equal(q(out, 'img').getAttribute('src'), 'https://huggingface.co/x.png');
  });

  test('single newlines become <br> (breaks: true)', () => {
    assert.ok(q(render('line one\nline two'), 'br'));
  });

  test('strikethrough is disabled so tildes stay literal', () => {
    const out = render('~5 tps, ~~10~~');
    assert.equal(q(out, 'del'), null);
    assert.match(fragment(window, out).textContent, /~5 tps, ~~10~~/);
  });

  test('@mentions become profile chips', () => {
    const a = q(render('thanks @alice!'), 'a.mention');
    assert.equal(a.dataset.mention, 'alice');
    assert.equal(a.getAttribute('href'), 'https://huggingface.co/alice');
    assert.equal(a.textContent, '@alice');
  });

  test('human-<user> mentions link to the human profile', () => {
    const a = q(render('cc @human-bob'), 'a.mention');
    assert.equal(a.getAttribute('href'), 'https://huggingface.co/bob');
  });

  test('emails and code are not mention-linked', () => {
    assert.equal(q(render('mail me@example.com'), 'a.mention'), null);
    assert.equal(q(render('`@alice`'), 'a.mention'), null);
  });

  test('artifact refs become bucket links, trailing punctuation excluded', () => {
    window.eval("CFG.bucket_web_url = 'https://huggingface.co/buckets/org/main'");
    const a = q(render('see artifacts/run1/loss.png.'), 'a');
    assert.equal(a.getAttribute('href'), 'https://huggingface.co/buckets/org/main/resolve/artifacts/run1/loss.png');
    assert.equal(a.textContent, 'artifacts/run1/loss.png');
  });
});

describe('renderMarkdownInline: fallback when a library is missing', () => {
  const cases = {
    'no DOMPurify': ['marked'],
    'no marked': ['DOMPurify'],
    'neither': [],
  };
  for (const [name, libs] of Object.entries(cases)) {
    test(`${name}: escapes instead of rendering`, () => {
      const window = loadDashboard({ libs });
      const out = window.renderMarkdownInline('**hi** <img src=x onerror=alert(1)>');
      assertInert(window, out);
      assert.equal(fragment(window, out).querySelector('img, strong'), null);
      assert.equal(out, '**hi** &lt;img src=x onerror=alert(1)&gt;');
    });
  }

  for (const [name, libs] of Object.entries(cases)) {
    test(`${name}: still links mentions and artifact refs`, () => {
      const window = loadDashboard({ libs });
      const out = window.renderMarkdownInline('see @agent-2 and artifacts/run-1/model.py, mail a@b.co');
      const doc = fragment(window, out);
      const mention = doc.querySelector('a.mention');
      assert.ok(mention, out);
      assert.equal(mention.textContent, '@agent-2');
      const artifact = [...doc.querySelectorAll('a')].find(a => a.textContent === 'artifacts/run-1/model.py');
      assert.ok(artifact, out);
      assert.equal(doc.querySelectorAll('a').length, 2);  // the email stays plain text
      assertInert(window, out);
    });
  }

  test('marked throwing falls back to escaping', () => {
    const window = loadDashboard();
    window.marked.parse = () => { throw new Error('boom'); };
    const out = window.renderMarkdownInline('<img src=x onerror=alert(1)>');
    assert.match(out, /&lt;img/);
    assertInert(window, out);
  });
});
