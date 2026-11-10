/* Drive the searchable select in a real DOM. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const SCRIPT = fs.readFileSync(
  require('path').join(__dirname, '..', '..', 'static', 'js', 'app.js'), 'utf8');

let passed = 0, failed = 0;
function check(name, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + name); }
  else { failed++; console.log('  FAIL ' + name + (detail ? '  → ' + detail : '')); }
}

function build(html) {
  const dom = new JSDOM(
    `<!doctype html><html lang="en"><body><form id="f">${html}</form></body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true });
  dom.window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  dom.window.eval(SCRIPT);
  dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
  return dom;
}

const OPTIONS = ['Sunrise Tower', 'Marina Residency', 'Palm Heights', 'Cedar House',
  'Oasis Block A', 'Oasis Block B', 'Garden Villas', 'Harbour Point'];

// ---- 1. enhancement ------------------------------------------------------
{
  const dom = build(`<label for="id_building">Building</label>
    <select name="building" id="id_building">
      <option value="">All buildings</option>
      ${OPTIONS.map((o, i) => `<option value="${i + 1}">${o}</option>`).join('')}
    </select>`);
  const doc = dom.window.document;
  const select = doc.querySelector('select');
  const button = doc.querySelector('.picker-button');

  check('the native select survives', !!select);
  check('a combobox button is added', !!button);
  check('it is announced as a combobox', button.getAttribute('role') === 'combobox');
  check('it starts closed', button.getAttribute('aria-expanded') === 'false');
  check('the button shows the current choice',
    button.textContent.includes('All buildings'), button.textContent.trim());
  check('the field label is linked',
    (button.getAttribute('aria-labelledby') || '').includes('id_building-label') ||
    (button.getAttribute('aria-labelledby') || '').includes(doc.querySelector('label').id));

  // ---- 2. opening ---------------------------------------------------------
  button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  const panel = doc.querySelector('.picker-panel');
  check('clicking opens a panel', !!panel);
  check('it is announced as open', button.getAttribute('aria-expanded') === 'true');
  check('the list is a listbox',
    !!panel.querySelector('.picker-list[role="listbox"]'));
  check('every option is offered',
    panel.querySelectorAll('.picker-option').length === OPTIONS.length + 1,
    String(panel.querySelectorAll('.picker-option').length));

  // ---- 3. searching -------------------------------------------------------
  const search = panel.querySelector('.picker-search');
  check('a long list gets a search box', !search.classList.contains('is-hidden'));
  search.value = 'oasis';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  const shown = [...panel.querySelectorAll('.picker-option')].map(n => n.textContent);
  check('typing filters the list', shown.length === 2, shown.join(' | '));
  check('the filter ignores case', shown.every(t => t.toLowerCase().includes('oasis')));

  search.value = 'zzz';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('no matches says so',
    panel.querySelector('.picker-empty').style.display !== 'none');

  // ---- 4. choosing --------------------------------------------------------
  search.value = 'marina';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  let changeFired = 0;
  select.addEventListener('change', () => { changeFired++; });
  panel.querySelector('.picker-option').dispatchEvent(
    new dom.window.MouseEvent('click', { bubbles: true }));

  check('the native select receives the value', select.value === '2', select.value);
  check('a change event fires so filters still submit', changeFired === 1);
  check('the panel closes', !doc.querySelector('.picker-panel'));
  check('the button shows the new choice',
    button.textContent.includes('Marina Residency'), button.textContent.trim());
  check('the form would submit the chosen value',
    new dom.window.FormData(doc.getElementById('f')).get('building') === '2');
}

// ---- 5. keyboard ---------------------------------------------------------
{
  const dom = build(`<select id="s">${
    OPTIONS.map((o, i) => `<option value="${i}">${o}</option>`).join('')}</select>`);
  const doc = dom.window.document;
  const button = doc.querySelector('.picker-button');
  button.dispatchEvent(new dom.window.KeyboardEvent('keydown',
    { key: 'ArrowDown', bubbles: true }));
  const panel = doc.querySelector('.picker-panel');
  check('arrow down opens it from the keyboard', !!panel);

  const search = panel.querySelector('.picker-search');
  search.dispatchEvent(new dom.window.KeyboardEvent('keydown',
    { key: 'ArrowDown', bubbles: true }));
  check('arrow keys move the highlight',
    panel.querySelectorAll('.picker-option')[1].classList.contains('on'));
  check('the highlight is announced',
    search.getAttribute('aria-activedescendant') ===
    panel.querySelectorAll('.picker-option')[1].id);

  search.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  check('enter chooses the highlighted option',
    doc.querySelector('select').value === '1', doc.querySelector('select').value);
  check('and closes the panel', !doc.querySelector('.picker-panel'));

  button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  doc.querySelector('.picker-search').dispatchEvent(
    new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  check('escape closes without choosing', !doc.querySelector('.picker-panel'));
}

// ---- 6. short lists, disabled and hidden options -------------------------
{
  const dom = build(`<select id="s">
      <option value="a">Cash</option><option value="b">Card</option>
      <option value="c" disabled>Cheque</option>
      <option value="d" hidden>Hidden</option></select>`);
  const doc = dom.window.document;
  doc.querySelector('.picker-button').dispatchEvent(
    new dom.window.MouseEvent('click', { bubbles: true }));
  const panel = doc.querySelector('.picker-panel');
  check('a short list hides the search box',
    panel.querySelector('.picker-search').classList.contains('is-hidden'));
  const labels = [...panel.querySelectorAll('.picker-option')].map(n => n.textContent);
  check('disabled options are not offered', !labels.includes('Cheque'), labels.join('|'));
  check('hidden options are not offered', !labels.includes('Hidden'), labels.join('|'));
}

// ---- 7. a disabled select, and the dependent-select chain -----------------
{
  const dom = build(`<select id="s" disabled><option>One</option></select>`);
  const doc = dom.window.document;
  check('a disabled select gives a disabled button',
    doc.querySelector('.picker-button').disabled);
  doc.querySelector('.picker-button').dispatchEvent(
    new dom.window.MouseEvent('click', { bubbles: true }));
  check('and refuses to open', !doc.querySelector('.picker-panel'));
}
{
  const dom = build(`<select id="src" data-chain-source="#dst"><option value="1">A</option>
      <option value="2">B</option></select>
    <select id="dst"><option value="">—</option>
      <option value="x" data-parent="1">Room 1</option>
      <option value="y" data-parent="2">Room 2</option></select>`);
  const doc = dom.window.document;
  const src = doc.getElementById('src');
  src.value = '2';
  src.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  doc.querySelectorAll('.picker-button')[1].dispatchEvent(
    new dom.window.MouseEvent('click', { bubbles: true }));
  const labels = [...doc.querySelectorAll('.picker-panel .picker-option')]
    .map(n => n.textContent);
  check('the picker respects the dependent-select filtering',
    labels.includes('Room 2') && !labels.includes('Room 1'), labels.join('|'));
}

// ---- 8. auto-submitting filter bars --------------------------------------
{
  const dom = build(`<select id="s" data-autosubmit><option value="a">A</option>
      <option value="b">B</option></select>`);
  const doc = dom.window.document;
  let submitted = 0;
  doc.getElementById('f').submit = () => { submitted++; };
  doc.querySelector('.picker-button').dispatchEvent(
    new dom.window.MouseEvent('click', { bubbles: true }));
  doc.querySelectorAll('.picker-panel .picker-option')[1].dispatchEvent(
    new dom.window.MouseEvent('click', { bubbles: true }));
  check('choosing in a filter bar submits the form', submitted === 1, String(submitted));
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
