/* The page loader: starts on a link or form that loads a new page, never on
   one that stays (downloads, new tabs, #fragments, cancelled confirms). */
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

// jsdom cannot navigate; it says so on the console. That is expected here.
const quiet = new VirtualConsole();
const dom = new JSDOM(`<!doctype html><html lang="en"><body>
  <a id="page" href="/guests/">Guests</a>
  <a id="export" href="/reports/export/rent-roll/?format=pdf">PDF</a>
  <a id="escpos" href="/billing/payments/4/receipt/escpos/">Print</a>
  <a id="blank" href="/billing/receipts/4/" target="_blank">Preview</a>
  <a id="hash" href="#notes">Notes</a>
  <a id="away" href="https://wa.me/971500000000">WhatsApp</a>
  <a id="tel" href="tel:+971500000000">Call</a>
  <a id="opt" href="/guests/" data-no-loader>Quiet</a>
  <form id="search" method="get" action="/guests/"><input name="q"></form>
  <form id="confirm" method="post" data-confirm="Sure?"><button>Delete</button></form>
  <form id="send" method="post" target="_blank"><button>Open in WhatsApp</button></form>
</body></html>`, { url: 'https://app.example/billing/', runScripts: 'outside-only',
                    pretendToBeVisual: true, virtualConsole: quiet });
const win = dom.window, doc = win.document;
win.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
win.confirm = () => false;
win.eval(SCRIPT);
doc.dispatchEvent(new win.Event('DOMContentLoaded'));

const bar = () => doc.querySelector('.page-loader');
const pill = () => doc.querySelector('.page-loader-pill');
const on = () => !!bar() && bar().classList.contains('on');
const reset = () => {
  const back = new win.Event('pageshow');
  back.persisted = true;
  win.dispatchEvent(back);
};
const click = id => doc.getElementById(id).dispatchEvent(
  new win.MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
const submit = id => doc.getElementById(id).dispatchEvent(
  new win.Event('submit', { bubbles: true, cancelable: true }));

(async () => {
  await sleep(50);   // let jsdom's own load-time pageshow pass
  click('page');
  win.dispatchEvent(new win.Event('pageshow'));
  check('the load-time pageshow does not cancel a tap already made', on());
  check('a link to another page starts the bar', on());
  check('the pill waits a moment', !pill().classList.contains('on'));
  await sleep(600);
  check('then says it is loading', pill().classList.contains('on') &&
        pill().textContent.includes('Loading'));
  check('the pill is announced to screen readers', pill().getAttribute('role') === 'status');
  reset();
  check('coming back from the cache clears it', !on() && !pill().classList.contains('on'));

  for (const id of ['export', 'escpos', 'blank', 'hash', 'away', 'tel', 'opt']) {
    reset();
    click(id);
    check(`no loader for ${id}`, !on());
  }

  reset();
  win.document.getElementById('page').dispatchEvent(
    new win.MouseEvent('click', { bubbles: true, cancelable: true, button: 0, metaKey: true }));
  check('no loader for a link opened in a new tab with ⌘', !on());

  reset();
  submit('search');
  check('a search form starts the bar', on());

  reset();
  submit('confirm');
  check('no loader when the confirm is cancelled', !on());

  reset();
  submit('send');
  check('no loader for a form that opens a new tab', !on());

  reset();
  click('page');
  doc.dispatchEvent(new win.Event('visibilitychange'));
  check('returning to the tab (WhatsApp app, a download) clears it', !on());

  console.log(`\n${passed} passed, ${failed} failed`);
  win.close();
  process.exit(failed ? 1 : 0);
})();
