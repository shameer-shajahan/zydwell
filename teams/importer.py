"""Bringing a team sheet (CSV) in as team lets.

One pass reads and checks every row and says what it would do — create,
update, unchanged or error — without saving. ``apply`` saves, and only when
no row has an error, so a file goes in whole or not at all. Re-running the
same file changes nothing: a row matches the live team let with the same
company, apartment and team name.
"""
import csv
import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.translation import gettext as _

from core import audit
from guests.models import DepositTransaction
from properties.models import Apartment, Building, Floor

from . import services
from .models import TeamLet

REQUIRED_COLUMNS = ['team_type', 'building', 'floor', 'apartment', 'company_name',
                    'collector_mobile', 'joining_date', 'rent_date', 'deposit', 'members',
                    'per_head', 'monthly_rent', 'received', 'pending', 'data_note']
#: Optional: ``team_name`` when the company name is not the team's own name
#: (EFS → "EFS TEAM BATCH 1"), ``collector_name`` for the contact person.
OPTIONAL_COLUMNS = ['team_name', 'collector_name']

CREATE, UPDATE, UNCHANGED, ERROR = 'create', 'update', 'unchanged', 'error'
UPDATABLE = ['joining_date', 'billing_day', 'members', 'per_head', 'monthly_rent',
             'deposit_amount', 'agreement_note']


@dataclass
class Row:
    line: int
    raw: dict
    action: str = CREATE
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    apartment: object = None
    company: object = None
    team: object = None
    values: dict = field(default_factory=dict)
    changes: list = field(default_factory=list)

    @property
    def label(self):
        return self.values.get('team_name') or self.raw.get('company_name') or '—'


@dataclass
class Plan:
    rows: list
    file_errors: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.file_errors and not any(r.action == ERROR for r in self.rows)

    def count(self, action):
        return sum(1 for r in self.rows if r.action == action)

    # For templates, which cannot pass an argument.
    count_create = property(lambda self: self.count(CREATE))
    count_update = property(lambda self: self.count(UPDATE))
    count_unchanged = property(lambda self: self.count(UNCHANGED))
    count_error = property(lambda self: self.count(ERROR))

    @property
    def totals(self):
        good = [r for r in self.rows if r.action != ERROR]
        zero = Decimal('0')
        return {
            'members': sum(r.values.get('members', 0) for r in good),
            'rent': sum((r.values.get('monthly_rent', zero) for r in good), zero),
            'deposit': sum((r.values.get('deposit_amount', zero) for r in good), zero),
        }


def read(text, tenant):
    """Check every row of ``text`` against ``tenant``'s places. Saves nothing."""
    text = text.lstrip('﻿')
    reader = csv.DictReader(io.StringIO(text))
    headers = [(h or '').strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        return Plan(rows=[], file_errors=[
            _('Missing columns: %(cols)s.') % {'cols': ', '.join(missing)}])
    reader.fieldnames = headers
    rows, seen = [], {}
    for line, raw in enumerate(reader, start=2):
        raw = {k: (v or '').strip() for k, v in raw.items() if k}
        if not any(raw.values()):
            continue
        row = Row(line=line, raw=raw)
        _check(row, tenant)
        if row.action != ERROR:
            key = (row.company.pk if row.company else row.raw['company_name'].lower(),
                   row.apartment.pk, row.values['team_name'].lower())
            if key in seen:
                row.errors.append(_('Same company, flat and team as line %(n)s.') % {
                    'n': seen[key]})
                row.action = ERROR
            seen[key] = line
        rows.append(row)
    if not rows:
        return Plan(rows=[], file_errors=[_('The file has no rows.')])
    return Plan(rows=rows)


def _check(row, tenant):
    raw, err = row.raw, row.errors

    kind = raw.get('team_type', '').lower()
    if kind and kind != 'team':
        err.append(_('Only TEAM rows can be imported (found “%(kind)s”).') % {
            'kind': raw['team_type']})

    row.apartment = _find_apartment(tenant, raw, err)

    name = raw.get('company_name', '')
    if not name:
        err.append(_('Company name is empty.'))
    team_name = raw.get('team_name') or name
    joining = _date(raw, 'joining_date', err)
    rent_date = _date(raw, 'rent_date', err)
    billing_day = 1
    if rent_date:
        billing_day = rent_date.day
        if billing_day > 28:
            row.warnings.append(_('Rent day %(d)s becomes the 28th — invoices run on days '
                                  '1 to 28.') % {'d': billing_day})
            billing_day = 28
    else:
        row.warnings.append(_('No rent date: invoices will be raised on the 1st.'))

    members = _int(raw, 'members', err)
    per_head = _money(raw, 'per_head', err, blank=None)
    rent = _money(raw, 'monthly_rent', err, blank=None)
    if not raw.get('monthly_rent'):
        err.append(_('Monthly rent is empty.'))
    deposit = _money(raw, 'deposit', err, blank=Decimal('0'))
    for column in ('received', 'pending'):
        amount = _money(raw, column, err, blank=Decimal('0'))
        if amount:
            row.warnings.append(_('%(col)s %(amount)s is not imported — raise the invoices, '
                                  'then record payments.') % {'col': column.title(),
                                                              'amount': amount})

    row.values = {
        'team_name': team_name, 'joining_date': joining, 'billing_day': billing_day,
        'starts_on': rent_date, 'members': members or 0, 'per_head': per_head,
        'monthly_rent': rent if rent is not None else Decimal('0'),
        'deposit_amount': deposit or Decimal('0'),
        'agreement_note': raw.get('data_note', ''),
    }
    if per_head is not None and rent is not None and members is not None \
            and per_head * members != rent:
        row.warnings.append(_('%(n)s × %(rate)s = %(calc)s, but the rent is %(rent)s. '
                              'The rent is what is billed.') % {
            'n': members, 'rate': per_head, 'calc': per_head * members, 'rent': rent})

    if err:
        row.action = ERROR
        return

    row.company = services.find_company(tenant, name, raw.get('collector_mobile', ''))
    if row.company is None:
        row.warnings.append(_('New company: %(name)s') % {'name': name})

    existing = None
    if row.company is not None:
        existing = TeamLet.objects.live().filter(
            guest=row.company, apartment=row.apartment, room__isnull=True,
            team_name__iexact=team_name).first()
    if existing is None:
        stays = list(services.individual_stays_in(row.apartment)[:3])
        if stays:
            err.append(_('Flat %(flat)s has guests checked in one by one: %(names)s.') % {
                'flat': row.apartment.name,
                'names': ', '.join(t.guest.full_name for t in stays)})
            row.action = ERROR
        return

    row.team = existing
    for name in UPDATABLE:
        old, new = getattr(existing, name), row.values[name]
        if old != new:
            row.changes.append((name, old, new))
    held = existing.deposit_held()
    if held and held != row.values['deposit_amount']:
        row.warnings.append(_('Deposit held is %(held)s; the sheet says %(sheet)s. Adjust it '
                              'on the team page.') % {'held': held,
                                                      'sheet': row.values['deposit_amount']})
    row.action = UPDATE if row.changes else UNCHANGED


def _find_apartment(tenant, raw, err):
    building_name, floor_name, flat = raw.get('building', ''), raw.get('floor', ''), \
        raw.get('apartment', '')
    if not building_name or not flat:
        err.append(_('Building and apartment are both needed.'))
        return None
    buildings = list(Building.objects.filter(tenant=tenant, is_archived=False,
                                             name__iexact=building_name))
    if not buildings:
        err.append(_('Building “%(b)s” not found.') % {'b': building_name})
        return None
    if len(buildings) > 1:
        err.append(_('%(n)s buildings are named “%(b)s”. Rename or archive the extras.') % {
            'n': len(buildings), 'b': building_name})
        return None
    flats = Apartment.objects.filter(tenant=tenant, is_archived=False,
                                     floor__building=buildings[0], name__iexact=flat)
    if floor_name:
        floor = Floor.objects.filter(building=buildings[0], is_archived=False,
                                     name__iexact=floor_name).first()
        if floor is None:
            err.append(_('Floor “%(f)s” not found in %(b)s.') % {
                'f': floor_name, 'b': buildings[0].name})
            return None
        flats = flats.filter(floor=floor)
    flats = list(flats.select_related('floor'))
    if not flats:
        err.append(_('Apartment “%(a)s” not found in %(b)s%(f)s.') % {
            'a': flat, 'b': buildings[0].name,
            'f': f' / {floor_name}' if floor_name else ''})
        return None
    if len(flats) > 1:
        err.append(_('Apartment “%(a)s” is on more than one floor (%(floors)s). Fill in the '
                     'floor column.') % {'a': flat,
                                         'floors': ', '.join(f.floor.name for f in flats)})
        return None
    return flats[0]


def _date(raw, column, err):
    value = raw.get(column, '')
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        err.append(_('%(col)s “%(v)s” is not a date like 2026-07-01.') % {
            'col': column, 'v': value})
        return None


def _money(raw, column, err, blank):
    value = raw.get(column, '').replace(',', '')
    if not value:
        return blank
    try:
        amount = Decimal(value).quantize(Decimal('0.01'))
    except InvalidOperation:
        err.append(_('%(col)s “%(v)s” is not a number.') % {'col': column, 'v': raw[column]})
        return blank
    if amount < 0:
        err.append(_('%(col)s cannot be negative.') % {'col': column})
    return amount


def _int(raw, column, err):
    value = raw.get(column, '').replace(',', '')
    if not value:
        return 0
    try:
        number = int(Decimal(value))
    except (InvalidOperation, ValueError):
        err.append(_('%(col)s “%(v)s” is not a whole number.') % {'col': column, 'v': value})
        return None
    if number < 0:
        err.append(_('%(col)s cannot be negative.') % {'col': column})
    return number


@transaction.atomic
def apply(plan, tenant, user=None):
    """Save a plan with no errors. Returns ``{'created': n, 'updated': n}``."""
    if not plan.ok:
        raise ValueError('The import has errors; nothing was saved.')
    created = updated = 0
    for row in plan.rows:
        if row.action == UNCHANGED:
            continue
        values = dict(row.values)
        starts_on = values.pop('starts_on')
        if row.action == UPDATE:
            fields = {name: values[name] for name, _old, _new in row.changes}
            services.update_team_let(team=row.team, user=user, **fields)
            if row.team.deposit_held() == 0 and values['deposit_amount'] > 0:
                _hold_deposit(row.team, user, values['deposit_amount'],
                              values['joining_date'] or starts_on)
            updated += 1
            continue
        company, _new = services.get_or_create_company(
            tenant, name=row.raw['company_name'], mobile=row.raw.get('collector_mobile', ''),
            contact_person=row.raw.get('collector_name', ''))
        team = services.create_team_let(
            tenant=tenant, user=user, company=company, apartment=row.apartment,
            starts_on=starts_on, **values)
        if values['deposit_amount'] > 0:
            _hold_deposit(team, user, values['deposit_amount'],
                          values['joining_date'] or starts_on)
        created += 1
    audit.log(audit.AuditLog.ACTION_CREATE, None, tenant=tenant, model_name='TeamLet',
              object_label='import',
              summary=_('Imported team lets: %(c)s new, %(u)s updated') % {
                  'c': created, 'u': updated})
    return {'created': created, 'updated': updated}


def _hold_deposit(team, user, amount, on_date):
    """The deposit the sheet says is held, dated when the team joined."""
    entry = DepositTransaction(
        tenant=team.tenant, tenancy=team, kind=DepositTransaction.KIND_HOLD, amount=amount,
        reason=_('Deposit held, from the imported team sheet'), recorded_by=user,
    )
    if on_date:
        entry.happened_on = on_date
    entry.save()
    return entry
