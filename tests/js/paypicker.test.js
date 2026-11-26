/* The payment form lists one guest's unpaid invoices, all ticked, amount filled in. */
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const SCRIPT = fs.readFileSync(
  require('path').join(__dirname, '..', '..', 'static', 'js', 'app.js'), 'utf8');

let passed = 0, failed = 0;
function check(name, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '  → ' + detail : '')); }
}

function build(fillIn, tenancy) {
  const dom = new JSDOM(`<!doctype html><html><body><form>
    <select name="tenancy" data-invoice-tenancy="1">
      <option value="">—</option><option value="7"${tenancy === '7' ? ' selected' : ''}>Shameer</option>
      <option value="8">Ravi</option></select>
    <select name="invoices" multiple data-invoice-picker="1"${fillIn ? ' data-fill-in="1"' : ''}>
      <option value="1" data-tenancy="7" data-balance="500.00">Aug</option>
      <option value="2" data-tenancy="7" data-balance="500.00">Sep</option>
      <option value="3" data-tenancy="8" data-balance="650.00">Ravi Nov</option></select>
    <input id="id_amount" name="amount" value="">
  </form></body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true,
                           virtualConsole: new VirtualConsole() });
  dom.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  dom.window.eval(SCRIPT);
  dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
  return dom;
}
const opt = (doc, v) => doc.querySelector(`[name=invoices] option[value="${v}"]`);

{
  const dom = build(true, '7'), doc = dom.window.document;
  check('a fresh form ticks the guest\'s unpaid invoices', opt(doc, '1').selected && opt(doc, '2').selected);
  check('another guest\'s invoice is hidden', opt(doc, '3').hidden && !opt(doc, '3').selected);
  check('the amount is their total', doc.querySelector('#id_amount').value === '1000.00',
        doc.querySelector('#id_amount').value);
  opt(doc, '2').selected = false;
  doc.querySelector('[name=invoices]').dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  check('unticking a month lowers the amount', doc.querySelector('#id_amount').value === '500.00');
  const tenancy = doc.querySelector('[name=tenancy]');
  tenancy.value = '8';
  tenancy.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  check('changing the guest ticks theirs', opt(doc, '3').selected && !opt(doc, '1').selected);
  check('and fills their amount', doc.querySelector('#id_amount').value === '650.00');
}
{
  const dom = build(false, '7'), doc = dom.window.document;
  check('a form sent back with an error keeps nothing ticked', !opt(doc, '1').selected &&
        !opt(doc, '2').selected);
  check('but still hides other guests', opt(doc, '3').hidden);
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
