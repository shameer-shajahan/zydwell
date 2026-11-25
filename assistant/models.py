"""What staff have taught the assistant."""
from django.conf import settings
from django.db import models

from core.models import TenantScopedModel


class LearnedPhrase(TenantScopedModel):
    """A sentence the assistant did not understand, and what the person meant.

    When it is unsure, the assistant asks "Did you mean…?". The answer is kept
    here and trained into this account's model, so the next time that sentence
    (or one like it) is understood straight away. Guest and building names are
    taken out first, so only the way of saying it is learnt.
    """

    text = models.CharField(max_length=300)
    intent = models.CharField(max_length=40)
    taught_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['tenant', 'text'],
                                               name='uniq_learned_phrase')]

    def __str__(self):
        return f'{self.text} → {self.intent}'
