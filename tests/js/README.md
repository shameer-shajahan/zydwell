# Browser-level tests

`static/js/app.js` has no build step and ships as-is, but the searchable select is
load-bearing — every form in the app depends on it putting the right value into the
native `<select>`. These tests drive it in a real DOM.

```bash
cd tests/js
npm install          # jsdom only, and only for this
npm test
```

Nothing here is needed to run ZyDwell.
