"""A new account starts with the default skills installed (MIP-2589): every way of creating one
(email, Google or GitHub sign-up, an invitation, the demo) goes through this one signal."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from origin.models.common.user_models import CustomUser
from origin.services.skills import defaults


@receiver(post_save, sender=CustomUser)
def install_default_skills(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        defaults.install_for_new_account(instance)
