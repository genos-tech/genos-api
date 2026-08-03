# Tier provenance: record who last wrote a tier / team plan.
#
# `reconcile_from_stripe` rewrites the tier from what Stripe says, and
# "Stripe says nothing" means free — so a hand-set comp is silently
# demoted on the owner's next return from checkout or the portal. Both
# cases look identical to the reconcile ("tier is pro, Stripe has
# nothing"), so it needs provenance to tell a comp from a lapsed
# subscriber. See SUBSCRIPTION_TIERS.md §8.3.
#
# The default is 'stripe' for every existing row: today's behaviour,
# deliberately. Backfilling core/pro/max as operator-set would be a
# guess, and guessing wrong freezes a genuinely lapsed subscriber on a
# paid tier forever — the exact failure the reconcile exists to prevent.
# Existing comps are re-pinned by re-running `feature_access set-tier`
# once, which is a no-op for anyone already correct.
#
# `enterprise` IS backfilled, because there it isn't a guess: no
# configured price maps to enterprise (`tier_for_price` only iterates
# PURCHASABLE_PLANS = core/pro/max) and the checkout webhook refuses any
# plan outside that tuple, so an enterprise row can only have been set by
# hand. This also closes a gap that predates the column: the reconcile
# skips enterprise, but `handle_event` never did, so a stray
# `subscription.deleted` could demote an enterprise account to free.

from django.db import migrations, models

_OPERATOR = "operator"
_STRIPE = "stripe"
_SOURCE_CHOICES = [(_STRIPE, "Stripe"), (_OPERATOR, "Operator")]


def pin_enterprise(apps, schema_editor):
    apps.get_model("origin", "CustomUser").objects.filter(tier="enterprise").update(
        tier_source=_OPERATOR
    )
    apps.get_model("origin", "TeamMaster").objects.filter(plan="enterprise").update(
        plan_source=_OPERATOR
    )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0181_webhookendpoint_channel_ids_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="tier_source",
            field=models.CharField(choices=_SOURCE_CHOICES, default=_STRIPE, max_length=16),
        ),
        migrations.AddField(
            model_name="teammaster",
            name="plan_source",
            field=models.CharField(choices=_SOURCE_CHOICES, default=_STRIPE, max_length=16),
        ),
        migrations.RunPython(pin_enterprise, migrations.RunPython.noop),
    ]
