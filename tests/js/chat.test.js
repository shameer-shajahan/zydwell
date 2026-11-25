/* The assistant chat: opening messages, sending, reply buttons, failures,
   and that nothing a person typed can turn into markup. */
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const SCRIPT = fs.readFileSync(
  require('path').join(__dirname, '..', '..', 'static', 'js', 'app.js'), 'utf8');

let passed = 0, failed = 0;
function check(name, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '  → ' + detail : '')); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

function build(opening, respond) {
  const dom = new JSDOM(`<!doctype html><html lang="en"><body>
    <div class="chat" data-chat data-url="/assistant/message/" data-error="No connection"
         data-retry="Try again" data-signed-out="Signed out">
      <div class="chat-log" data-chat-log></div>
      <form data-chat-form data-no-loader>
        <input type="hidden" name="csrfmiddlewaretoken" value="tok123">
        <input type="text" name="text" data-chat-input>
        <button type="submit">Send</button>
      </form>
    </div>
    <script type="application/json" id="chat-opening">${JSON.stringify(opening)}</script>
  </body></html>`, { url: 'https://app.example/assistant/', runScripts: 'outside-only',
                     pretendToBeVisual: true, virtualConsole: new VirtualConsole() });
  const win = dom.window;
  win.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  win.scrollTo = () => {};
  win.sent = [];
  win.fetch = (url, options) => {
    win.sent.push({ url, options });
    return respond(options.body);
  };
  win.eval(SCRIPT);
  win.document.dispatchEvent(new win.Event('DOMContentLoaded'));
  return dom;
}

function json(replies) {
  return Promise.resolve({
    ok: true, status: 200, redirected: false,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve({ replies }),
  });
}

(async () => {
  // ---- 1. opening, typing, replies ----------------------------------------
  {
    const dom = build([{ text: 'Hi!', buttons: [{ label: 'Add a guest', send: 'add guest' }] }],
      () => json([{ text: 'Please check', rows: [['Name', '<b>Ravi</b>']],
                    buttons: [{ label: 'Yes, add guest', send: 'yes' },
                              { label: 'Open', href: '/guests/5/' },
                              { label: 'Evil', href: 'javascript:alert(1)' }] }]));
    const doc = dom.window.document;
    const log = doc.querySelector('[data-chat-log]');
    check('opening message is shown', log.textContent.includes('Hi!'));

    const input = doc.querySelector('[data-chat-input]');
    input.value = 'add guest <img src=x onerror=alert(1)>';
    doc.querySelector('[data-chat-form]').dispatchEvent(
      new dom.window.Event('submit', { bubbles: true, cancelable: true }));
    const mine = log.querySelector('.chat-msg.me .chat-bubble');
    check('typed message shows as a bubble', mine && mine.textContent.includes('add guest'));
    check('typed markup stays text', !log.querySelector('img'));
    check('input is cleared', input.value === '');
    check('typing dots show while waiting', !!log.querySelector('.typing'));

    const sent = dom.window.sent[0];
    check('posts to the endpoint', sent.url === '/assistant/message/' && sent.options.method === 'POST');
    check('sends the CSRF token', sent.options.headers['X-CSRFToken'] === 'tok123');
    check('sends the text form-encoded', sent.options.body.startsWith('text=add%20guest'));

    await sleep(20);
    check('typing dots go away', !log.querySelector('.typing'));
    const dd = log.querySelector('dd');
    check('reply rows are shown as text', dd && dd.textContent === '<b>Ravi</b>' && !dd.querySelector('b'));
    check('a site link becomes a link', !!log.querySelector('a[href="/guests/5/"]'));
    check('a javascript: link is dropped', !log.querySelector('a[href^="javascript"]') &&
          !log.textContent.includes('Evil'));
    check('earlier answer buttons are disabled', log.querySelector(
      'button[data-chat-send="add guest"]').disabled);

    // ---- 2. a reply button sends its value, shows its label ----
    log.querySelector('button[data-chat-send="yes"]').click();
    check('button sends its value', dom.window.sent[1].options.body === 'text=yes');
    const bubbles = log.querySelectorAll('.chat-msg.me .chat-bubble');
    check('button label shows as the user bubble',
          bubbles[bubbles.length - 1].textContent === 'Yes, add guest');
  }

  // ---- 3. failures ---------------------------------------------------------
  {
    let calls = 0;
    const dom = build([], () => {
      calls++;
      return calls === 1 ? Promise.reject(new Error('offline')) : json([{ text: 'Back again' }]);
    });
    const doc = dom.window.document;
    const log = doc.querySelector('[data-chat-log]');
    doc.querySelector('[data-chat-input]').value = 'hi';
    doc.querySelector('[data-chat-form]').dispatchEvent(
      new dom.window.Event('submit', { bubbles: true, cancelable: true }));
    await sleep(20);
    const error = log.querySelector('.chat-msg.error');
    check('a dropped connection shows an error', error && error.textContent.includes('No connection'));
    error.querySelector('button').click();
    await sleep(20);
    check('Try again resends the same text', dom.window.sent[1].options.body === 'text=hi');
    check('the retry does not repeat the user bubble', log.querySelectorAll('.chat-msg.me').length === 1);
    check('the error goes and the reply shows', !log.querySelector('.chat-msg.error') &&
          log.textContent.includes('Back again'));
  }
  {
    const dom = build([], () => Promise.resolve({
      ok: true, status: 200, redirected: true, headers: { get: () => 'text/html; charset=utf-8' },
      json: () => Promise.reject(new Error('not json')) }));
    const doc = dom.window.document;
    doc.querySelector('[data-chat-input]').value = 'hi';
    doc.querySelector('[data-chat-form]').dispatchEvent(
      new dom.window.Event('submit', { bubbles: true, cancelable: true }));
    await sleep(20);
    check('a login page instead of JSON says signed out',
          doc.querySelector('.chat-msg.error').textContent.includes('Signed out'));
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
