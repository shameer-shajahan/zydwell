"""Letting whole apartments to company teams."""
import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _

from core import audit
from core.models import Status
from guests.models import Guest, Tenancy
from guests.services import _occupy, _release, record_deposit, with_deposit_held, with_outstanding

from .models import TeamLet

MONEY = DecimalField(max_digits=14, decimal_places=2)

#: Fields a team let form or import may set, beyond the company and apartment.
TEAM_FIELDS = ['team_name', 'joining_date', 'billing_day', 'billing_cycle', 'members',
               'per_head', 'monthly_rent', 'deposit_amount', 'agreement_note',
               'prorate_first_period']


def normalise_mobile(number):
    """Digits only, so '+971 50 514 6578' and '971505146578' are one company."""
    return re.sub(r'\D', '', number or '')


def find_company(tenant, name, mobile=''):
    """The company with this collector mobile, else this exact name, else ``None``.

    The mobile wins: one company sends several teams (EFS batch 1, 2, 3 …)
    under different names but always through the same collector.
    """
    companies = Guest.objects.filter(tenant=tenant, is_company=True)
    digits = normalise_mobile(mobile)
    if digits:
        for company in companies.filter(mobile__icontains=digits[-7:]):
            if normalise_mobile(company.mobile) == digits:
                return company
    if name:
        return companies.filter(full_name__iexact=name.strip()).first()
    return None


def get_or_create_company(tenant, *, name, mobile='', contact_person='', whatsapp_number='',
                          email='', notes=''):
    """Returns ``(company, created)``."""
    company = find_company(tenant, name, mobile)
    if company is not None:
        return company, False
    company = Guest.objects.create(
        tenant=tenant, is_company=True, full_name=name.strip(), mobile=mobile.strip(),
        contact_person=contact_person, whatsapp_number=whatsapp_number, email=email,
        notes=notes,
    )
    audit.log(audit.AuditLog.ACTION_CREATE, company, tenant=tenant,
              summary=_('Company %(name)s added') % {'name': company.full_name})
    return company, True


def individual_stays_in(apartment):
    """Guests checked in one by one to this flat — they block a team let."""
    return Tenancy.objects.filter(
        apartment=apartment, room__isnull=False, status__code__in=Tenancy.LIVE_STATUS_CODES,
    ).select_related('guest')


def check_apartment_free(apartment):
    stays = list(individual_stays_in(apartment)[:3])
    if stays:
        raise ValidationError(_(
            'Flat %(flat)s still has guests checked in one by one (%(names)s). Check them '
            'out or move them before letting the flat to a team.') % {
            'flat': apartment.name, 'names': ', '.join(t.guest.full_name for t in stays)})


@transaction.atomic
def create_team_let(*, tenant, user, company, apartment, team_name, monthly_rent,
                    billing_day=1, billing_cycle=Tenancy.CYCLE_MONTHLY, members=0,
                    per_head=None, joining_date=None, starts_on=None,
                    deposit_amount=Decimal('0'), deposit_received=Decimal('0'),
                    agreement_note='', prorate_first_period=False):
    """Let ``apartment`` to ``company``'s team and fill its beds.

    ``joining_date`` may be unknown; billing then runs from ``starts_on``
    (the first rent date), or today.
    """
    check_apartment_free(apartment)
    team = TeamLet(
        tenant=tenant, guest=company, apartment=apartment, team_name=team_name.strip(),
        joining_date=joining_date,
        check_in_date=joining_date or starts_on or timezone.localdate(),
        billing_day=billing_day, billing_cycle=billing_cycle, members=members,
        per_head=per_head, monthly_rent=monthly_rent, deposit_amount=deposit_amount or 0,
        agreement_note=agreement_note, rent_mode=Tenancy.MODE_ADVANCE,
        prorate_first_period=prorate_first_period, created_by=user,
    )
    team.full_clean(exclude=['floor', 'building', 'status', 'room', 'bed'])
    team.save()
    _occupy(team)
    if deposit_received and deposit_received > 0:
        record_deposit(tenancy=team, user=user, amount=deposit_received,
                       reason=_('Deposit received when the team moved in'))
    audit.log(audit.AuditLog.ACTION_CREATE, team, tenant=tenant,
              summary=_('Let flat %(flat)s to team %(team)s (%(company)s), %(n)s members '
                        'at %(rent)s/month') % {
                  'flat': apartment.name, 'team': team.team_name,
                  'company': company.full_name, 'n': members, 'rent': monthly_rent})
    return team


@transaction.atomic
def update_team_let(*, team, user, company=None, apartment=None, **fields):
    """Change a live team let. Moving it to another flat moves the occupied beds."""
    tracked = ['team_name', 'apartment', 'members', 'per_head', 'monthly_rent',
               'billing_day', 'deposit_amount', 'joining_date']
    before = audit.snapshot(team, tracked)
    old = TeamLet.objects.get(pk=team.pk)
    moving = apartment is not None and apartment.pk != old.apartment_id
    if moving:
        check_apartment_free(apartment)
    for name, value in fields.items():
        if name not in TEAM_FIELDS:
            raise TypeError(f'Unexpected team let field {name!r}')
        setattr(team, name, value)
    if company is not None:
        team.guest = company
    if apartment is not None:
        team.apartment = apartment
    if 'joining_date' in fields and fields['joining_date']:
        team.check_in_date = fields['joining_date']
    team.full_clean(exclude=['floor', 'building', 'status', 'room', 'bed'])
    team.save()
    if moving and team.is_live:
        _release(old)
        _occupy(team)
    audit.log(audit.AuditLog.ACTION_UPDATE, team, tenant=team.tenant,
              summary=_('Edited team let %(team)s') % {'team': team.team_name},
              changes=audit.diff(before, audit.snapshot(team, tracked)))
    return team


def raise_invoices(*, tenant, user, teams, on_date=None):
    """This period's invoice for each live team that has none yet."""
    from billing.services import generate_due_invoices

    return generate_due_invoices(tenant, on_date=on_date, user=user,
                                 only=teams.live().values('pk'))


# --- the Teams list ----------------------------------------------------------
def with_money(teams, month_start, month_end):
    """Adds ``outstanding_total``, ``deposit_held_total`` and ``received_month``."""
    from billing.models import Payment

    received = Payment.objects.filter(
        tenancy=OuterRef('pk'), is_reversed=False, paid_on__range=(month_start, month_end),
    ).order_by().values('tenancy').annotate(s=Sum('amount')).values('s')
    teams = with_deposit_held(with_outstanding(teams))
    return teams.annotate(received_month=Coalesce(
        Subquery(received, output_field=MONEY), Value(Decimal('0')), output_field=MONEY))


def totals(rows):
    """Column totals for the Teams list, summed over already-annotated rows."""
    rows = list(rows)
    zero = Decimal('0')
    return {
        'count': len(rows),
        'members': sum(t.members for t in rows),
        'rent': sum((t.monthly_rent for t in rows), zero),
        'deposit': sum((t.deposit_held_total for t in rows), zero),
        'received': sum((t.received_month for t in rows), zero),
        'pending': sum((t.outstanding_total for t in rows), zero),
    }


def live_summary(teams):
    """Count, members and monthly rent of the live team lets in ``teams``."""
    figures = teams.live().aggregate(n=Count('pk'), members=Sum('members'),
                                     rent=Sum('monthly_rent'))
    return {'count': figures['n'] or 0, 'members': figures['members'] or 0,
            'rent': figures['rent'] or Decimal('0')}


def vacated_status(tenant):
    return Status.by_code(tenant, Status.KIND_TENANCY, 'vacated')


def search(teams, query):
    return teams.filter(Q(team_name__icontains=query) | Q(guest__full_name__icontains=query)
                        | Q(guest__mobile__icontains=query)
                        | Q(apartment__name__icontains=query))
