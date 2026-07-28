"""Allow a user to connect several Google accounts.

Three moving parts:
  1. `is_login_identity` replaces the `provider == primary_auth_provider`
     comparison that used to stand in for "this is the row they sign in
     with". That comparison breaks the moment a Google-signup user
     connects a second Google account — both rows would claim to be the
     login identity, and the disconnect endpoint would refuse to delete
     either one.
  2. The (user, provider) unique constraint becomes conditional so it
     still holds for GitHub (whose integration code assumes one account)
     while Google is free to have many.
  3. Backfill. Today exactly one row per user can match their primary
     provider — the old unique constraint guaranteed it — so the
     backfill is unambiguous and needs no tie-breaking.

The constraint drop is ordered before the backfill purely so a failure
in the (slower) data migration leaves the schema in a state the reverse
migration can restore.
"""

from django.db import migrations, models


def set_login_identity(apps, schema_editor):
    """Flag each user's signup row. `primary_auth_provider` of "email"
    matches no ConnectedAccount, which is correct: those users signed up
    with a password and any provider row they hold is a pure API
    connection they're allowed to disconnect."""
    ConnectedAccount = apps.get_model("origin", "ConnectedAccount")
    # Resolved to a list of pks first: `filter(...).update(...)` on a
    # queryset whose condition spans a join raises "Cannot update a
    # query that contains joins", and comparing a column to a related
    # column is exactly such a join.
    pks = list(
        ConnectedAccount.objects.filter(
            provider=models.F("user__primary_auth_provider")
        ).values_list("pk", flat=True)
    )
    if pks:
        ConnectedAccount.objects.filter(pk__in=pks).update(is_login_identity=True)


def unset_login_identity(apps, schema_editor):
    """Reverse leg. The column is dropped by the AddField's own reverse,
    so this only has to be a no-op that keeps the operation reversible
    when someone unwinds just the data step."""
    ConnectedAccount = apps.get_model("origin", "ConnectedAccount")
    ConnectedAccount.objects.update(is_login_identity=False)


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0165_customuser_preferred_llm_effort"),
    ]

    operations = [
        migrations.AddField(
            model_name="connectedaccount",
            name="is_login_identity",
            field=models.BooleanField(default=False),
        ),
        migrations.RemoveConstraint(
            model_name="connectedaccount",
            name="connected_account_unique_per_user_provider",
        ),
        migrations.RunPython(set_login_identity, unset_login_identity),
        migrations.AddConstraint(
            model_name="connectedaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(("provider__in", ("google",)), _negated=True),
                fields=("user", "provider"),
                name="connected_account_unique_per_user_single_provider",
            ),
        ),
        migrations.AddConstraint(
            model_name="connectedaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_login_identity", True)),
                fields=("user",),
                name="connected_account_one_login_identity_per_user",
            ),
        ),
        migrations.AlterModelOptions(
            name="connectedaccount",
            options={"ordering": ["-is_login_identity", "ts_created_at", "id"]},
        ),
    ]
