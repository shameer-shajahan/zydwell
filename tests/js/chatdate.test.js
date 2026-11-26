/* The calendar in the chat: picking a date answers the open question. */
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const SCRIPT = fs.readFileSync(
  require('path').join(__dirname, '..', '..', 'static', 'js', 'app.js'), 'utf8');
let passed = 0, failed = 0;
function check(name, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '  → ' + detail : '')); }
}

const opening = [{ text: 'Which day does Shameer check in?', date: { key: 'date', value: '2026-11-26' },
                   buttons: [{ label: 'Yesterday', send: '@pick:date:2026-11-25' }] }];
const dom = new JSDOM(`<!doctype html><html><body>
  <div class="chat" data-chat data-url="/assistant/message/" data-date-use="Use this date">
    <div class="chat-log" data-chat-log></div>
    <form data-chat-form><input type="hidden" name="csrfmiddlewaretoken" value="t">
      <input type="text" name="text" data-chat-input></form></div>
  <script type="application/json" id="chat-opening">${JSON.stringify(opening)}</script>
</body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: new VirtualConsole() });
const win = dom.window, doc = win.document;
win.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
win.scrollTo = () => {};
win.sent = [];
win.fetch = (url, options) => {
  win.sent.push(options.body);
  return Promise.resolve({ ok: true, headers: { get: () => 'application/json' },
                           json: () => Promise.resolve({ replies: [{ text: 'ok' }] }) });
};
win.eval(SCRIPT);
doc.dispatchEvent(new win.Event('DOMContentLoaded'));

const field = doc.querySelector('input[data-chat-date]');
check('a calendar is shown with the suggested date', field && field.type === 'date' && field.value === '2026-11-26');
field.value = '2026-08-01';
doc.querySelector('button[data-chat-date-use]').click();
check('using it answers the question', win.sent[0] === 'text=' + encodeURIComponent('@pick:date:2026-08-01'),
      win.sent[0]);
check('the picker is closed afterwards', field.disabled);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
