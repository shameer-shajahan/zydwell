/* Drive the staff permission grid's tick-all boxes in a real DOM.

   The grid is the only place an owner hands out access, and the three
   toggles (whole grid / one column / one row) all drive the same real
   checkboxes — so what the owner sees ticked has to be what gets posted. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const SCRIPT = fs.readFileSync(
  require('path').join(__dirname, '..', '..', 'static', 'js', 'app.js'), 'utf8');

let passed = 0, failed = 0;
function check(name, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '  → ' + detail : '')); }
}

const MODULES = ['Property', 'Guests', 'Billing', 'Messaging'];
const COLUMNS = ['View', 'Create', 'Edit', 'Delete'];

/* The same shape as templates/accounts/staff_permissions.html. */
function grid() {
  const head = COLUMNS.map((c, i) =>
    `<th class="center">${c}<div><input type="checkbox" data-check-col="${i + 1}"
       aria-label="Tick ${c} for every module"></div></th>`).join('');
  const body = MODULES.map((m, r) =>
    `<tr><td>${m}</td>${COLUMNS.map((c, i) =>
      `<td class="center"><input type="checkbox" name="${m.toLowerCase()}_${c.toLowerCase()}"></td>`
     ).join('')}<td class="center"><input type="checkbox" data-check-row
       aria-label="Tick everything for ${m}"></td></tr>`).join('');
  return `<label class="checkline tick-all"><span>Select everything</span>
      <input type="checkbox" data-check-all="#permission-grid" aria-label="Tick every permission"></label>
    <table class="data" id="permission-grid">
      <thead><tr><th>Module</th>${head}<th class="center">Row</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

function build() {
  const dom = new JSDOM(
    `<!doctype html><html lang="en"><body><form id="f">${grid()}</form></body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true });
  dom.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  dom.window.eval(SCRIPT);
  dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
  return dom;
}

function tick(box, on) {
  box.checked = on === undefined ? true : on;
  box.dispatchEvent(new box.ownerDocument.defaultView.Event('change', { bubbles: true }));
}

/* Only the boxes that actually carry a permission — not the toggles. */
function granted(doc) {
  return Array.from(doc.querySelectorAll('#permission-grid input[name]'))
    .filter(b => b.checked).map(b => b.name).sort();
}

// ---- 1. the whole grid ---------------------------------------------------
{
  const doc = build().window.document;
  const all = doc.querySelector('[data-check-all]');

  check('nothing is ticked to begin with', granted(doc).length === 0);

  tick(all, true);
  check('one tick grants every module and action',
    granted(doc).length === MODULES.length * COLUMNS.length, granted(doc).length);

  tick(all, false);
  check('and clearing it takes them all back', granted(doc).length === 0, granted(doc));
}

// ---- 2. the toggles are not themselves permissions -----------------------
{
  const doc = build().window.document;
  tick(doc.querySelector('[data-check-all]'), true);

  check('the toggles carry no name, so nothing extra is posted',
    doc.querySelectorAll('#permission-grid input[name]').length === 16,
    doc.querySelectorAll('#permission-grid input[name]').length);
  check('select-everything does not tick the column toggles into the form',
    Array.from(doc.querySelectorAll('[data-check-col]')).every(b => !b.hasAttribute('name')));
}

// ---- 3. a single column --------------------------------------------------
{
  const doc = build().window.document;
  tick(doc.querySelectorAll('[data-check-col]')[0], true);   // View

  check('the View column grants view on every module',
    granted(doc).join(',') === 'billing_view,guests_view,messaging_view,property_view',
    granted(doc).join(','));

  tick(doc.querySelectorAll('[data-check-col]')[3], true);   // Delete
  check('a second column adds to it rather than replacing it',
    granted(doc).length === 8, granted(doc));

  tick(doc.querySelectorAll('[data-check-col]')[0], false);
  check('clearing View leaves Delete alone',
    granted(doc).every(n => n.endsWith('_delete')), granted(doc));
}

// ---- 4. a single row -----------------------------------------------------
{
  const doc = build().window.document;
  tick(doc.querySelectorAll('[data-check-row]')[1], true);   // Guests

  check('the row grants every action on that one module',
    granted(doc).join(',') === 'guests_create,guests_delete,guests_edit,guests_view',
    granted(doc).join(','));
  check('and touches no other module', granted(doc).length === COLUMNS.length);

  tick(doc.querySelectorAll('[data-check-row]')[1], false);
  check('clearing the row revokes just that module', granted(doc).length === 0);
}

// ---- 5. the toggles do not fight each other ------------------------------
{
  const doc = build().window.document;
  /* A column toggle ticks boxes, each firing its own change event. If the
     row handler answered those it would cascade and grant the whole grid. */
  tick(doc.querySelectorAll('[data-check-col]')[1], true);   // Create

  check('ticking a column does not cascade into whole rows',
    granted(doc).length === MODULES.length, granted(doc));
  check('it granted Create and nothing else',
    granted(doc).every(n => n.endsWith('_create')), granted(doc));
}

// ---- 6. it leaves the rest of the page alone -----------------------------
{
  const dom = new JSDOM(
    `<!doctype html><html lang="en"><body><form id="f">
       <input type="checkbox" name="outside">
       ${grid()}</form></body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true });
  dom.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  dom.window.eval(SCRIPT);
  const doc = dom.window.document;
  doc.dispatchEvent(new dom.window.Event('DOMContentLoaded'));

  tick(doc.querySelector('[data-check-all]'), true);
  check('a checkbox outside the grid is untouched',
    doc.querySelector('[name=outside]').checked === false);
}

// ---- 7. an ordinary checkbox still behaves -------------------------------
{
  const doc = build().window.document;
  const one = doc.querySelector('#permission-grid input[name]');
  tick(one, true);
  check('ticking one permission by hand grants only that one',
    granted(doc).join(',') === 'property_view', granted(doc).join(','));
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
