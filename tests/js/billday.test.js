/* The check-in form's invoice day follows the check-in date until typed. */
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const SCRIPT = fs.readFileSync(
  require('path').join(__dirname, '..', '..', 'static', 'js', 'app.js'), 'utf8');

let passed = 0, failed = 0;
function check(name, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '  → ' + detail : '')); }
}

const dom = new JSDOM(`<!doctype html><html><body><form>
  <input type="date" name="check_in_date" value="2026-11-25">
  <input type="number" name="billing_day" value="25" data-bill-day-of="check_in_date">
</form></body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true,
                         virtualConsole: new VirtualConsole() });
const win = dom.window, doc = win.document;
win.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
win.eval(SCRIPT);
doc.dispatchEvent(new win.Event('DOMContentLoaded'));

const date = doc.querySelector('[name=check_in_date]');
const day = doc.querySelector('[name=billing_day]');
function pick(value) {
  date.value = value;
  date.dispatchEvent(new win.Event('change', { bubbles: true }));
}

pick('2026-12-18');
check('picking the 18th makes the bill day 18', day.value === '18', day.value);
pick('2026-12-31');
check('the 31st becomes the 28th', day.value === '28', day.value);
day.value = '5';
day.dispatchEvent(new win.Event('input', { bubbles: true }));
pick('2026-12-20');
check('a day typed by hand is kept', day.value === '5', day.value);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
