# ZyDwell operations runbook

Backup, restore and go-live. Written for whoever runs the server.

---

## 1. Backups

`python manage.py backup_db` writes a timestamped, gzipped dump into `backups/`
(configurable with `ZYDWELL["BACKUP_DIR"]`) and prunes older files.

```bash
python manage.py backup_db --keep 14        # binary dump only
python manage.py backup_db --keep 14 --json # plus a portable JSON dump of every model
```

* SQLite → `backups/zydwell-YYYYMMDD-HHMMSS.sqlite3.gz`
* PostgreSQL → `backups/zydwell-YYYYMMDD-HHMMSS.sql.gz` (uses `pg_dump`)
* `--json` also writes `zydwell-YYYYMMDD-HHMMSS.json`, which restores across database
  engines and across Django versions.

**Uploaded files are not in the dump.** `media/` holds guest photos, document scans,
bill photos and logos. Back it up alongside the database:

```bash
tar czf backups/media-$(date +%Y%m%d).tar.gz media/
```

Copy both off the server — a backup on the same disk is not a backup.

### Schedule

```
30 2 * * *  cd /srv/zydwell && .venv/bin/python manage.py backup_db --keep 14
40 2 * * *  cd /srv/zydwell && tar czf backups/media-$(date +\%Y\%m\%d).tar.gz media/
0  3 * * 0  rsync -a /srv/zydwell/backups/ backup-host:/backups/zydwell/
```

---

## 2. Restore

> Always restore into a **scratch** database first and check it before touching
> production. Never restore over a live database that has not itself been backed up.

### 2.1 Check what you have

```bash
ls -lh backups/
gzip -t backups/zydwell-20260929-023000.sqlite3.gz   # verify the archive
```

### 2.2 SQLite

```bash
# 1. Stop the application
sudo systemctl stop zydwell

# 2. Keep the current file, in case the restore is the mistake
mv db.sqlite3 db.sqlite3.before-restore

# 3. Unpack the backup into place
gunzip -c backups/zydwell-20260929-023000.sqlite3.gz > db.sqlite3

# 4. Bring the schema up to the running code, then check
.venv/bin/python manage.py migrate
.venv/bin/python manage.py check

# 5. Start again
sudo systemctl start zydwell
```

### 2.3 PostgreSQL

```bash
sudo systemctl stop zydwell

# Restore into a scratch database first
createdb zydwell_restore
gunzip -c backups/zydwell-20260929-023000.sql.gz | psql zydwell_restore

# Point the app at it and sanity-check
ZYDWELL_DB_NAME=zydwell_restore .venv/bin/python manage.py migrate --check
ZYDWELL_DB_NAME=zydwell_restore .venv/bin/python manage.py shell -c \
  "from billing.models import Payment; print(Payment.objects.count())"

# Promote it
psql -c 'ALTER DATABASE zydwell RENAME TO zydwell_old;'
psql -c 'ALTER DATABASE zydwell_restore RENAME TO zydwell;'
sudo systemctl start zydwell
```

### 2.4 From the JSON dump (cross-engine)

```bash
python manage.py migrate                  # empty database, schema only
python manage.py loaddata backups/zydwell-20260929-023000.json
```

### 2.5 Media

```bash
tar xzf backups/media-20260929.tar.gz -C /srv/zydwell/
chown -R zydwell:zydwell /srv/zydwell/media
```

### 2.6 After any restore — verify

1. Sign in as a building owner; the dashboard shows the expected occupancy.
2. Open the last receipt issued before the backup: `/billing/payments/` → newest row.
3. Check `/settings/audit-log/` — the trail should end near the backup timestamp.
4. Confirm the next receipt number does not collide:
   `python manage.py shell -c "from core.models import Sequence; print(list(Sequence.objects.values()))"`
   Receipt numbers are drawn from `core.Sequence`. If receipts were issued *after* the
   backup you restored from, bump `last_value` past the highest number already printed,
   otherwise a number could be reused.

**Restore drill:** run 2.2 or 2.3 into a scratch database once a quarter. A backup you
have never restored is a guess.

---

## 3. Going live

1. `ZYDWELL_DEBUG=0`, a real `ZYDWELL_SECRET_KEY`, and `ZYDWELL_ALLOWED_HOSTS` set.
2. PostgreSQL (`ZYDWELL_DB_ENGINE=postgres`) rather than SQLite once more than a couple
   of people use it at once.
3. HTTPS at the proxy. Then in `settings.py` add `SECURE_SSL_REDIRECT = True`,
   `SESSION_COOKIE_SECURE = True`, `CSRF_COOKIE_SECURE = True`.
4. `python manage.py collectstatic` and serve `STATIC_ROOT` and `MEDIA_ROOT` from the
   proxy (or add whitenoise). Run `python manage.py compilemessages -l ar` on every
   deploy — the compiled `.mo` files are not in version control.
5. `python manage.py seed_platform` once, then change the super admin password and set
   its IP allow-list on the user record.
   Then reset **every** account carried over from development — the README's `admin` /
   `admin` and `superadmin` / `admin` logins exist for local work only, and any owner
   account given a throwaway password while testing still has it. List them and check:

   ```bash
   python manage.py shell -c "
   from accounts.models import User
   for u in User.objects.filter(is_active=True):
       print(u.id, u.username, u.role, u.last_login)"
   ```

   A super admin can reset any owner's password at `/platform/users/<id>/password/`
   without knowing the old one; the owner is then forced to choose a new one at their
   next sign-in. Note that database backups contain these password hashes, so a backup
   taken before the reset still carries the development credentials — protect or rotate
   those too.
6. Cron entries from section 1 plus `run_daily_jobs`.
7. WhatsApp: register the templates in Meta Business Manager first, tick **approved**
   on each template in ZyDwell, then set `WHATSAPP_ENABLED=1`. Until then every message
   is logged as *queued* and nothing is sent — safe to run in that state.
8. Printers: add each one under POS printers and press **Test print**. Network printers
   need `IP:port`, normally port 9100.

---

## 4. Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| A client sees "Account locked" | subscription past its grace period, or the client was suspended | platform console → client → subscription, or set the status back to Active |
| Reminders are logged as *skipped* | the template is not marked approved | messaging → templates → tick **Approved** after Meta approves it |
| Reminders are logged as *queued* | `WHATSAPP_ENABLED` is not `1` | set it and restart |
| "Printer unreachable" | wrong IP:port, or the printer is off the network | check with `nc -vz <ip> 9100`, then **Test print** |
| Staff cannot see a building | they have a floor/building assignment that excludes it | staff → access → remove or widen the assignment |
| Invoices are not appearing | `run_daily_jobs` is not scheduled | run it by hand, or use **Run billing now** on the invoices screen |
