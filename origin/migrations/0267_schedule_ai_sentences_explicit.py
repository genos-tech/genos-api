"""AI sentences in scheduled reports become an opt-in (MIP-1969).

Until now `what_to_do_today` read a missing `ai_sentences` as ON. From now on a missing key means
OFF, so every schedule that existed (and so was getting AI sentences) is given an explicit `true`:
its behaviour does not change. A schedule that already says `false` stays false. New schedules
start off.
"""

from django.db import migrations


def pin_current_behaviour(apps, schema_editor):
    MemberSchedule = apps.get_model("origin", "MemberSchedule")
    for sched in MemberSchedule.objects.filter(job="what_to_do_today").iterator():
        params = sched.params if isinstance(sched.params, dict) else {}
        if "ai_sentences" in params:
            continue
        sched.params = {**params, "ai_sentences": True}
        sched.save(update_fields=["params"])


class Migration(migrations.Migration):
    dependencies = [("origin", "0266_storage_size_bytes")]

    operations = [migrations.RunPython(pin_current_behaviour, migrations.RunPython.noop)]
