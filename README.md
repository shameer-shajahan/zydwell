# ZyDwell

Bedspace, room and building rental management. Floors, rooms, beds, guests, rent and
reports in one system instead of notebooks and chat threads.

Built with Django 5. The interface is hand-written HTML and CSS in an iOS design
language — no CDN, no build step, so it works on site without internet.

---

## Quick start

```bash
cd zydwell
../mac_env/bin/pip install -r requirements.txt
../mac_env/bin/python manage.py migrate
../mac_env/bin/python manage.py seed_platform          # status templates + platform owner
../mac_env/bin/python manage.py seed_demo              # optional: a demo client with data
../mac_env/bin/python manage.py runserver
```

Then open http://127.0.0.1:8000/.

### Logins

A clean database starts with one platform owner account created by `seed_platform`.
Sign in, create a client from the platform console, and the owner account you name there
becomes the first admin.

`seed_demo` additionally creates `admin` / `admin`, `owner` / `owner12345` and
`collector` / `staff12345` against a demo client.

A platform owner lands on the platform console and picks a client to work inside; an
admin lands straight on their own dashboard.

> These are development credentials. Change them before the system is reachable by
> anyone else — `docs/RUNBOOK.md` section 3 covers go-live.

To set the platform owner password yourself:

```bash
python manage.py seed_platform --username '<username>' --password 'your-password'
# or, for an account that already exists:
python manage.py changepassword '<username>'
```

---

## The three access levels

| Tier | Sees | Can do |
| --- | --- | --- |
| **Platform owner** | every client | create/suspend clients, set per-client limits, subscriptions and auto-lock, global settings, branding, feature flags, global audit, impersonation, system health |
| **Admin** (building owner) | one client | floors, rooms, beds, staff accounts and per-module grants, discounts and refunds, categories, statuses, every report and the audit log |
| **Staff** (supervisor / collector) | assigned floors or buildings | check in/out, collect rent and print receipts, record expenses, send WhatsApp reminders. Cannot delete records or edit rent amounts |

Staff permissions are per module and per action (view / create / edit / delete), set by
the admin on the staff member's access screen. The grid is plain checkboxes, with a
**Select everything** tick in the header that sets the lot, a tick at the head of each
column (view / create / edit / delete) for that action across every module, and one at the
end of each row for that module. They all drive the same boxes, so what the admin sees
ticked is exactly what gets saved. `staff` and `settings` are admin-only and never appear
in the grid at all, so no tick-all can reach them.

Everyone lands on the dashboard, because everyone has one — it just shows each person
something different. An owner gets the business view: income, occupancy, arrears, the
twelve-month chart. Someone without the reports grant gets a **staff dashboard** built from
whatever they *can* open — free beds, the guests they look after, what they collected
today, rent still owed, reminders waiting to go out — every tile scoped to their assigned
floors, and the money tiles counting only their own collections. Someone with no grants at
all gets the same screen saying so in plain words, rather than a refusal page on their
first sign-in. Other screens still refuse when they should; only the dashboard adapts.

---

## Modules

1. **Property** — Building → Floor → Apartment → Room → Bed, with a visual floor plan
   and bed board. A floor holds apartments; an apartment holds rooms; a room holds beds.
   Room types and rent basis (per bed / per room / per flat) are data, not code.
2. **Guests & tenancy** — profiles, documents with expiry alerts, check-in, notice,
   check-out with deposit settlement, mid-month bed transfer with pro-rata split.
3. **Rent & invoicing** — automatic invoices, extra charges, discounts with admin
   approval, late-fee rule, partial payments, advance credit. Each tenancy is billed
   **in advance** (invoice as the period opens) or **post-paid** (as it closes), and the
   first invoice charges the full rent, only the days stayed, or an amount the office
   agreed with the guest. Part periods divide by the real length of the month, never a
   flat 30.
4. **Payment monitoring** — receipt numbers that are never reused, per-guest history and
   dues aging, per-collector daily cash report and handover confirmation, admin-only
   reversal, everything in the audit log.
5. **Income & expense** — one ledger, user-created categories, every entry tagged to a
   building, floor or room, recurring monthly bills, bill photo attachments.
6. **Status management** — bed, tenancy and invoice statuses live in a table the admin
   edits: name, colour and behaviour flag (counts as available / occupied / settled).
7. **Dashboard & reports** — occupancy, collected, pending with aging, expense split,
   net profit, a 12-month income-vs-expense chart, and Excel/PDF exports.
8. **POS billing** — 58 mm and 80 mm thermal printers over USB or network (IP:port) with
   ESC/POS output, a test print, and a PDF fallback when no printer answers.
9. **WhatsApp** — two ways out, both logged, and every message carries the name of the
   admin or staff member who sent it:
   * **Open in WhatsApp** (no setup): every Remind / Send button opens WhatsApp — the app
     on a phone, WhatsApp Web on a desktop — with the message already written. Staff read
     it and press send. No API keys, no Meta template approval.
   * **Business Cloud API** (optional): sends by itself once `WHATSAPP_ENABLED=1` and the
     templates are approved. This is what the nightly reminder schedule uses
     (5 days before, on the day, then 3 / 7 / 15 days after).

   With the API off, everything the nightly job prepares lands in **Waiting to send**
   rather than disappearing, and staff work down that list one tap at a time.

   The log keeps **30 days** (`ZYDWELL_MESSAGE_LOG_DAYS`). Older entries are deleted by
   the nightly job and, where that is not scheduled, the first time someone opens the
   log each day — except messages still waiting to send, which stay until they go out.
   Anyone with the messaging *delete* grant can remove a single entry; both are audited.

### Finding things

Every list — guests, tenancies, invoices, payments, overdue, the bed board, the ledger,
the WhatsApp log and the reports — has the same **Building → Floor → Apartment → Room →
Bed** picker. Each choice opens the next level and clears the ones below it; a level with
a single option opens straight away. Filters and searches run in the database over the
whole list, never just the page on screen, and Excel / PDF exports follow them.

The bar is the same everywhere: one row of compact pills. Each pill is only as wide as
its name — "Search", "Status", "Floor" — until you use it: the search widens and shows
its full hint while you type, a chosen value reads "Floor 2" in the tint colour and opens
out on hover or tap, and a level that cannot be used yet says why. When the row runs out
of room it scrolls sideways (swipe, or the mouse wheel) with a faded edge, and the
Search / Apply button stays in place. Dropdowns apply as soon as they change.

---

## What a guest can reach

A guest never gets an account. They get one link, and it opens a small area holding
**only their own data**:

| At `/g/<token>/` | They can |
| --- | --- |
| Their details | **Edit** — name, contact, nationality, emergency contact, employer, passport / Emirates ID / visa numbers, expiry dates and photos |
| Their receipts | **Read only** — rent receipts and deposit movements, each on its own page |

Nothing else. No invoices, no balances, no other guest, no part of the office — and no
field the office owns: rent, bed, deposit amount, notes and blacklisting are not on their
form, and posting those names changes nothing. A receipt belonging to someone else returns
*not found* rather than *forbidden*, because a refusal would confirm it exists. A reversed
receipt disappears from their list. When the link expires or is revoked, the whole area
closes and the page names no one.

`guests/tests_portal.py` holds 16 tests for exactly this, most of them about what a guest
*cannot* see.

## Guests completing their own profile

Admin or staff press **Create link** on a guest's page. That issues a random, expiring
link (1, 7 or 30 days) at `/g/<token>/` which the guest opens on their phone — no account,
no password. They fill in what they know: contact details, nationality, emergency contact,
employer, and their passport / Emirates ID / visa numbers, expiry dates and photos. Only
the name is required, so a half-filled form is accepted and they can return to the same
link later.

Issuing a new link revokes the previous one, and a link can be revoked by hand. The page
shows that guest and nothing else in the system, uploads are limited to images or PDF
under 5 MB, and the submission lands in the audit log attributed to the guest rather than
to a member of staff.

Send it with **Send on WhatsApp**, which uses the `profile_link` template and the guest's
WhatsApp number.

## Deleting a guest

Admin only, from the guest's page. The confirmation screen totals what will be destroyed —
tenancies, invoices, receipts, the rent already collected, documents and messages — warns
if they are still checked in, still owe money, or still have a deposit with you, and makes
you type the guest's full name. Deleting releases any bed they held and removes the income
those receipts posted, so the ledger does not keep money with no source behind it. Receipt
numbers already issued stay retired, and one audit entry survives with the totals.

Checking out or blacklisting is usually the better answer; the screen offers both.

## Passwords

Nobody needs an old password to set a new one for someone below them:

* **Platform owner → building owner** — platform console → client → **Set password**. Type
  one, or leave it blank and a generated one is shown **once**.
* **Building owner → staff** — Staff → Edit, fill in the password field. Blank keeps the
  current one.

A password set this way is single-use: the account is flagged, every screen sends them to
choose their own, and until they do they can only reach their profile or sign out. The
platform owner is therefore never left holding a working password for someone else's
account. Existing sessions stop working the moment the password changes, and the audit log
records that a reset happened — never the password.

## Starting clean

`seed_demo` exists for looking around. To get back to an empty platform:

```bash
python manage.py reset_data --yes
```

It backs up first, then deletes every client and all their data — guests, tenancies,
invoices, receipts, the ledger, staff accounts and the audit trail. The platform owner
account and the 13 platform default statuses survive, so you can sign in and create your
first real client straight away.

| Flag | Effect |
| --- | --- |
| `--yes` | required; without it nothing is deleted |
| `--no-backup` | skip the automatic backup |
| `--all-users` | delete platform owner accounts too, leaving no way to sign in |
| `--platform-statuses` | also clear the default status templates |

Deletion order matters here: tenancies, invoices and payments protect the records they
point at, so a plain `Tenant.objects.all().delete()` raises `ProtectedError`. The command
walks children before parents; `core/tests_reset.py` covers that path.

## Scheduled work

```bash
# Nightly: raise due invoices, apply late fees, raise recurring expenses,
# send rent reminders and document-expiry alerts, clear WhatsApp log entries past 30 days.
python manage.py run_daily_jobs

# Daily backup, keeping the last 14
python manage.py backup_db --keep 14 --json
```

A sample crontab:

```
15 1 * * *  cd /srv/zydwell && /srv/zydwell/.venv/bin/python manage.py run_daily_jobs
30 2 * * *  cd /srv/zydwell && /srv/zydwell/.venv/bin/python manage.py backup_db --keep 14
```

---

## Configuration

Everything is read from the environment; nothing secret is committed.

| Variable | Purpose |
| --- | --- |
| `ZYDWELL_SECRET_KEY` | Django secret key. **Set this in production.** |
| `ZYDWELL_DEBUG` | `0` in production |
| `ZYDWELL_ALLOWED_HOSTS` | comma-separated hostnames, e.g. `zydwell.hostlanetechnolab.com` |
| `ZYDWELL_CSRF_TRUSTED_ORIGINS` | comma-separated origins with scheme; default `https://zydwell.hostlanetechnolab.com` |
| `ZYDWELL_DB_ENGINE` | `postgres` to leave SQLite behind |
| `ZYDWELL_DB_NAME` / `_USER` / `_PASSWORD` / `_HOST` / `_PORT` | PostgreSQL connection |
| `ZYDWELL_TIME_ZONE` | default `Asia/Dubai` |
| `ZYDWELL_CURRENCY` | default currency for new clients |
| `WHATSAPP_ENABLED` | `1` to actually send; otherwise messages are logged as queued |
| `WHATSAPP_API_URL` / `_PHONE_NUMBER_ID` / `_ACCESS_TOKEN` | WhatsApp Business Cloud API |

---

## Signing in

The sign-in screen is phone-first: on a phone the brand panel is dropped entirely so the
form is the first thing on screen, never below a hero you have to scroll past. It carries
the things that stop a failed attempt — a show-password button, a Caps Lock warning, a
one-submit guard, `autocapitalize="none"` so iOS does not capitalise a username, and a
trimmed username so an autocompleted trailing space does not lock someone out. Language
and appearance are both switchable *before* signing in, which matters for on-site staff
who need Arabic or dark mode and have no account context yet.

A wrong username and a wrong password give the same message on purpose — different
wording would let anyone probe for valid staff usernames. There is no self-service
password reset; the building owner sets a new one from Staff → Edit.

## Error pages

`404`, `403`, `400`, `500` and `403_csrf` (session expired) all share
`templates/error.html` and match the rest of the app, including dark mode. They only
render when `DEBUG=0`, which makes them awkward to work on, so in development each one
previews at `/__error__/<code>/` — that route does not exist in production.

The 403 matters more than usual here: the app raises `PermissionDenied` whenever a staff
member reaches past their grants, so it repeats the reason the view gave and says who can
widen their access.

`500.html` is the fragile one. Django renders it with **no request and no context
processors**, so it cannot touch `request`, `user` or the branding context, and the page
direction has to come from `{% get_current_language_bidi %}` rather than `LANGUAGE_CODE`.
`core/tests_errors.py` renders it with no arguments at all to prove it stays that way.

## Appearance

Light, dark, or follow the device. The switch is the circle icon in the top bar; it
cycles Automatic → Light → Dark, and Profile has a labelled picker. The choice is kept in
`localStorage`, so a shared office tablet and a supervisor's own phone can differ, and it
is applied before first paint so a dark screen never flashes white.

The palette lives in `static/css/app.css`: the full light set on bare `:root`, then the
same tokens redefined twice for dark — once under `prefers-color-scheme` and once under
`[data-theme="dark"]`, so the switch wins in both directions. `core/tests_ui.py` fails the
build if the two dark blocks ever drift apart or a colour ends up dark-only.

## Dropdowns

Every `<select>` is upgraded into a combobox you can type into — the list filters as you
type, arrow keys and Enter work, and on a phone it opens as a bottom sheet. The search box
appears once a list has seven or more options; below that it would be noise.

The native `<select>` stays in the DOM and keeps the value, so forms, validation and the
server see exactly what they saw before. If the script never runs, the plain select still
works. `tests/js/` drives the component in a real DOM (jsdom) — 32 assertions covering
filtering, keyboard use, disabled and hidden options, dependent selects and auto-submitting
filter bars.

## Deposits

Every deposit movement — received, deducted, refunded — gets its own numbered receipt
(`DEP…`), printable on the thermal printer and shareable on WhatsApp exactly like a rent
receipt, as a read-only page at `/d/<token>/`.

**Deposits and Deposit movement** sit on the dashboard, and *Reports → Deposits* shows
money in and out over any date range with an Excel/PDF export and a per-building
breakdown.

One deliberate accounting decision: taking a deposit and giving it back **never touches
profit**. It is the guest's money held, not earnings — counting it as income would
overstate what the building has actually made. Only a *deduction* posts to the income
ledger, under **Security deposit**, because that is money you keep.

## Sending a receipt

No PDFs. **Send on WhatsApp** sends the guest a private link to a read-only page showing
their receipt — `/r/<token>/` for rent, `/d/<token>/` for deposits. They open it on their
phone, read it, and that is all: nothing to download, no form, nothing they can change.

The link is signed rather than stored, so there is no table and nothing to clean up, and
it stops working after 30 days (`ZYDWELL_RECEIPT_LINK_DAYS`). Staff get a **Preview as the
guest sees it** button so they can check what was sent.

## On a phone

The sidebar becomes a slide-over drawer, the five main sections sit in a bottom tab bar,
page actions turn into a swipeable strip instead of a tall stack, and every list table
folds into label/value cards — `app.js` copies each column heading onto its cells, so
this works on all 77 screens without per-template markup. Safe-area insets are respected
on notched phones, and inputs are 16px so iOS does not zoom on focus.

## Language

English and Arabic, with a right-to-left layout in Arabic. The picker sits in the top
bar. To extend the Arabic catalogue:

```bash
python manage.py makemessages -l ar --ignore=mac_env
# edit locale/ar/LC_MESSAGES/django.po
python manage.py compilemessages -l ar
```

---

## Tests

```bash
python manage.py test
```

360 tests cover the rules that matter: bed capacity, archive-instead-of-delete, check-in
and check-out, deposit settlement, pro-rata transfers, invoice totals and statuses,
receipt numbering, payment reversal, advance credit, late fees, the ESC/POS output,
read-only receipt links, permission and floor scoping, who lands on which dashboard,
subscription locking, the audit trail and exports.

The Arabic catalogue is tested too, because a bad `msgmerge` is silent: no fuzzy entry
may survive, no two English strings may share one translation, and no translation may
drop or invent a `%(name)s` placeholder — that last one once turned `%(n)s receipts`
into plain "rent receipt", quietly losing the count.

The JavaScript is driven in a real DOM (jsdom), which is the only way to test it since
none of it runs on the server:

```bash
cd tests/js && npm install && npm test
```

47 assertions: 32 on the searchable dropdown (filtering, keyboard, disabled and hidden
options, dependent selects, auto-submitting filter bars) and 15 on the permission grid's
tick-all boxes — including that ticking a column does not cascade into whole rows, and
that the toggles themselves are never posted as permissions.

---

## Layout

```
zydwell/
├── core/          tenants, statuses, sequences, branding, feature flags, audit, platform console
├── accounts/      users, roles, per-module permissions, floor assignments, login history
├── properties/    building → floor → apartment → room → bed, occupancy maths
├── guests/        guests, documents, tenancies, transfers, deposits
├── billing/       invoices, payments, receipts, ESC/POS, printers, cash handover
├── accounting/    categories, the income/expense ledger, recurring bills
├── messaging/     WhatsApp templates, reminder rules, delivery log
├── reports/       dashboard, reports, exports, settings, audit log
├── templates/     every screen
├── static/css/    the iOS design system (one file, no build step)
└── docs/          RUNBOOK.md — backup, restore and go-live
```

Built by Hostlane Technolab.

# zydwell
