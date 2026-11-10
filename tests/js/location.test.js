/* Building → Floor → Apartment → Room → Bed: a change clears the levels below
   (so a stale room is never sent with a new floor) and reloads the list once. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const SCRIPT = fs.readFileSync(
  require('path').join(__dirname, '..', '..', 'static', 'js', 'app.js'), 'utf8');

let passed = 0, failed = 0;
function check(name, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '  → ' + detail : '')); }
}

function level(name, index, options, value, disabled) {
  return `<div class="field"><label for="loc-${name}">${name}</label>
    <select id="loc-${name}" name="${name}" data-loc-level="${index}"${disabled ? ' disabled' : ''}>
      <option value="">All</option>
      ${options.map(([v, t]) => `<option value="${v}"${v === value ? ' selected' : ''}>${t}</option>`).join('')}
    </select></div>`;
}

const dom = new JSDOM(`<!doctype html><html lang="en"><body>
  <form id="f" method="get"><input name="q" value="ali">
  <div class="loc-filter">
    ${level('building', 0, [['1', 'Tower'], ['2', 'West']], '1')}
    ${level('floor', 1, [['10', 'Floor 1'], ['11', 'Floor 2']], '10')}
    ${level('apartment', 2, [['20', '101'], ['21', '102']], '20')}
    ${level('room', 3, [['30', 'A']], '30')}
    ${level('bed', 4, [['40', 'A1']], '40')}
  </div></form></body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true });
dom.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
dom.window.eval(SCRIPT);
dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));

const doc = dom.window.document;
const form = doc.getElementById('f');
let submits = 0;
form.submit = () => { submits++; };
const pick = name => doc.getElementById('loc-' + name);

check('every level becomes a searchable picker',
  doc.querySelectorAll('.loc-filter .picker-button').length === 5);

// Choose another floor through the picker, the way a person would.
const button = pick('floor').closest('.picker').querySelector('.picker-button');
button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
const option = [...doc.querySelectorAll('.picker-option')].find(n => n.textContent === 'Floor 2');
option.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));

check('the new floor is set', pick('floor').value === '11', pick('floor').value);
check('the building above is kept', pick('building').value === '1' && !pick('building').disabled);
check('apartment, room and bed are emptied',
  ['apartment', 'room', 'bed'].every(n => pick(n).value === ''),
  ['apartment', 'room', 'bed'].map(n => pick(n).value).join(','));
check('and left out of the request',
  ['apartment', 'room', 'bed'].every(n => pick(n).disabled));
check('the list reloads exactly once', submits === 1, String(submits));

const sent = new dom.window.FormData(form);
check('the request carries the search and the new chain only',
  sent.get('q') === 'ali' && sent.get('building') === '1' && sent.get('floor') === '11' &&
  !sent.has('apartment') && !sent.has('room') && !sent.has('bed'),
  [...sent.entries()].map(e => e.join('=')).join('&'));

// ---- the pill shows its name until used, then "Floor 2" ----------------
{
  const pills = new JSDOM(`<!doctype html><html lang="en"><body><form>
    <select id="b" name="building" data-placeholder="Building">
      <option value="">All buildings</option><option value="1">Tower</option></select>
    <select id="f" name="floor" data-placeholder="Floor" data-prefix="Floor">
      <option value="">All floors</option><option value="10" selected>2</option></select>
    <select id="r" name="room" data-placeholder="Room" data-prefix="Room" disabled title="Apartment first">
      <option value="">Apartment first</option></select>
  </form></body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true });
  pills.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  pills.window.eval(SCRIPT);
  pills.window.document.dispatchEvent(new pills.window.Event('DOMContentLoaded'));
  const d = pills.window.document;
  const face = id => d.getElementById(id).closest('.picker').querySelector('.picker-button');

  check('an empty pill shows its name, not "All buildings"',
    face('b').textContent.trim() === 'Building', face('b').textContent.trim());
  check('and is not tinted', !face('b').classList.contains('has-value'));
  check('a chosen pill reads "Floor 2"',
    face('f').textContent.replace(/\s+/g, ' ').trim() === 'Floor 2',
    face('f').textContent.replace(/\s+/g, ' ').trim());
  check('and is tinted', face('f').classList.contains('has-value'));
  check('a level that cannot be used yet is disabled', face('r').disabled);
  check('and says why on hover', face('r').title === 'Apartment first', face('r').title);

  const sel = d.getElementById('b');
  sel.value = '1';
  sel.dispatchEvent(new pills.window.Event('change', { bubbles: true }));
  check('choosing repaints it with the value', face('b').textContent.trim() === 'Tower'
    && face('b').classList.contains('has-value'), face('b').textContent.trim());
}

// ---- a search pill says "Search" until tapped -------------------------
{
  const page = new JSDOM(`<!doctype html><html lang="en"><body><form>
    <input id="q" type="search" name="q" placeholder="Search" data-hint="Search name, mobile or room…">
  </form></body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true });
  page.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  page.window.eval(SCRIPT);
  const q = page.window.document.getElementById('q');
  q.focus();
  check('tapping in shows the full hint', q.placeholder === 'Search name, mobile or room…', q.placeholder);
  q.blur();
  check('leaving puts the short one back', q.placeholder === 'Search', q.placeholder);
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
