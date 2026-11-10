"""Users, roles, per-module permissions and login history."""
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.constants import ACTIONS, ADMIN_ONLY_MODULES, PERMISSION_MODULES
from core.models import Tenant, TimeStampedModel


class User(AbstractUser):
    ROLE_SUPER_ADMIN = 'super_admin'
    ROLE_ADMIN = 'admin'
    ROLE_STAFF = 'staff'
    ROLE_CHOICES = [
        (ROLE_SUPER_ADMIN, _('Super admin')),
        (ROLE_ADMIN, _('Admin (building owner)')),
        (ROLE_STAFF, _('Staff (supervisor / collector)')),
    ]

    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_STAFF)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name='users',
        help_text=_('Empty only for the platform super admin.'),
    )
    phone = models.CharField(max_length=32, blank=True)
    job_title = models.CharField(max_length=80, blank=True)
    language = models.CharField(max_length=8, choices=[('en', 'English'), ('ar', 'العربية')], default='en')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    two_factor_enabled = models.BooleanField(default=False)
    ip_allow_list = models.CharField(
        max_length=255, blank=True,
        help_text=_('Comma-separated IPs. Super admin only; empty means no restriction.'),
    )
    must_change_password = models.BooleanField(default=False)

    class Meta:
        ordering = ['first_name', 'last_name', 'username']

    def __str__(self):
        name = self.get_full_name().strip()
        return f'{name or self.username} ({self.get_role_display()})'

    @property
    def is_super_admin(self):
        return self.role == self.ROLE_SUPER_ADMIN

    @property
    def is_admin(self):
        return self.role == self.ROLE_ADMIN

    @property
    def is_staff_member(self):
        return self.role == self.ROLE_STAFF

    def display_name(self):
        return self.get_full_name().strip() or self.username

    def active_tenant(self, session=None):
        """The tenant this user is working inside right now.

        A super admin has no tenant of their own; they pick one from the
        platform console and it is remembered in the session.
        """
        if self.is_super_admin:
            if session is None:
                return None
            tenant_id = session.get('active_tenant_id')
            if not tenant_id:
                return None
            return Tenant.objects.filter(pk=tenant_id).first()
        return self.tenant

    # --- authorisation -------------------------------------------------
    def can(self, module, action='view'):
        if not self.is_active:
            return False
        if self.is_super_admin or self.is_admin:
            return True
        if module in ADMIN_ONLY_MODULES:
            return False
        perm = self.module_permissions.filter(module=module).first()
        if perm is None:
            return False
        return getattr(perm, f'can_{action}', False)

    def allowed_building_ids(self):
        """``None`` means every building in the tenant."""
        if not self.is_staff_member:
            return None
        rows = list(self.assignments.values_list('building_id', 'floor_id'))
        if not rows:
            return None
        return [b for b, f in rows if b and not f]

    def scope_queryset(self, queryset, building_field='building', floor_field='floor',
                       apartment_field='apartment'):
        """Narrow a queryset to what this staff member looks after.

        An assignment is read at its narrowest: an apartment beats a floor, and a
        floor beats the whole building.
        """
        if not self.is_staff_member:
            return queryset
        assignments = list(self.assignments.all())
        if not assignments:
            return queryset
        buildings = [a.building_id for a in assignments
                     if a.building_id and not a.floor_id and not a.apartment_id]
        floors = [a.floor_id for a in assignments if a.floor_id and not a.apartment_id]
        apartments = [a.apartment_id for a in assignments if a.apartment_id]
        q = models.Q()
        if buildings:
            q |= models.Q(**{f'{building_field}__in': buildings})
        if floors:
            q |= models.Q(**{f'{floor_field}__in': floors})
        if apartments and apartment_field:
            q |= models.Q(**{f'{apartment_field}__in': apartments})
        if not (buildings or floors or apartments):
            return queryset
        return queryset.filter(q)


class StaffPermission(TimeStampedModel):
    """One row per staff member per module, toggled by the admin."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='module_permissions')
    module = models.CharField(max_length=32, choices=PERMISSION_MODULES)
    can_view = models.BooleanField(default=False)
    can_create = models.BooleanField(default=False)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_approve = models.BooleanField(default=False)

    class Meta:
        ordering = ['module']
        constraints = [
            models.UniqueConstraint(fields=['user', 'module'], name='uniq_permission_per_module'),
        ]

    def __str__(self):
        granted = [a for a in ACTIONS if getattr(self, f'can_{a}')]
        return f'{self.user.username} / {self.module}: {", ".join(granted) or "none"}'


class StaffAssignment(TimeStampedModel):
    """Restricts a staff member to particular buildings or floors."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assignments')
    building = models.ForeignKey(
        'properties.Building', on_delete=models.CASCADE, related_name='staff_assignments'
    )
    floor = models.ForeignKey(
        'properties.Floor', on_delete=models.CASCADE, null=True, blank=True,
        related_name='staff_assignments', help_text=_('Leave empty for the whole building.'),
    )
    apartment = models.ForeignKey(
        'properties.Apartment', on_delete=models.CASCADE, null=True, blank=True,
        related_name='staff_assignments',
        help_text=_('Leave empty for the whole floor.'),
    )

    class Meta:
        ordering = ['building__name', 'floor__sort_order', 'apartment__sort_order']
        constraints = [
            models.UniqueConstraint(fields=['user', 'building', 'floor', 'apartment'],
                                    name='uniq_assignment'),
        ]

    def __str__(self):
        return f'{self.user.username} → {self.apartment or self.floor or self.building}'

    @property
    def scope_label(self):
        return self.apartment or self.floor or self.building


class LoginHistory(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='login_history'
    )
    username_tried = models.CharField(max_length=150, blank=True)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name='login_history'
    )
    successful = models.BooleanField(default=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = _('login history')

    def __str__(self):
        state = 'ok' if self.successful else 'failed'
        return f'{self.username_tried or self.user} {state} {self.created_at:%Y-%m-%d %H:%M}'
