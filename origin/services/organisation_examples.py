"""An example department for a brand-new REAL team (MIP-1690, under MIP-1447).

A new team lands in an app where nothing shows how a company can keep many people, chats,
projects and notes tidy: features nobody has seen (a private group chat, a private project,
a project tag, a mention group, a team folder) are never found. The decision (Ken, 2026-11-18)
is that a department is a project tag + a mention group + a team folder + a public group chat,
and that a smaller group inside it uses the private version of each. This module builds one
example department that shows exactly that, so people meet every piece in their first minute.

Called from `starter_seeder.create_starter_workspace`, i.e. only when the person ticked the
sample-data box at team creation. Everything is made for the owner alone, named "Sample dept"
so it is obvious what to delete, and in ONE language (the account's own, English when it is not
Japanese), unlike the two starter projects that are always both.

Not made here, on purpose: nothing sets `is_demo`, nothing is created for other people, and no
row is a copy of the demo's content (the eval corpus, `test_demo_v1_golden`, stays untouched).
Group chats cannot be deleted by their owner yet (only left); the how-to text says so.
"""

from __future__ import annotations

import logging

from django.db import transaction

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.services.demo_blocks import _body, _text_body
from origin.services.demo_seeder import (
    _create_blueprint_tasks,
    _create_milestone_with_backing_task,
    _create_sprint,
)
from origin.services.note_folder_manage import create_team_folder
from origin.services.user_time import user_today

log = logging.getLogger(__name__)

NOTE_TYPE_PERSONAL = 1
ROLE_OWNER = 1
LABEL_COLOR, LABEL_TEXT_COLOR = "#0ea5e9", "#ffffff"

# The group name is the same in every language: a mention is typed, and the app asks for a
# lowercase name.
GROUP_NAME = "sample-dept"


def _task(title, body, due_offset_days=3, priority="Normal"):
    return {
        "title": title,
        "status": "Open",
        "priority": priority,
        "assignee": "demo",
        "is_milestone_child": True,
        "parent_idx": None,
        "due_offset_days": due_offset_days,
        "body": body,
    }


def _milestone(title, goal, how, due_offset_days=14):
    return {
        "title": title,
        "body": _body(("🎯 Goal", [goal]), ("🪜 How to use this", [how])),
        "status": "Open",
        "priority": "Normal",
        "due_offset_days": due_offset_days,
    }


COPY = {
    "en": {
        "label": "Sample dept",
        "group_description": "Everyone in the sample department",
        "folder": "Sample dept",
        "subfolder": "Meeting notes",
        "private_folder": "Sample dept: leads only (private)",
        "overview_title": "How the sample dept keeps its work tidy",
        "overview": _body(
            (
                "What this is",
                [
                    "This is example data, made when you created the team, so you can see how "
                    "a department can be set up. Look around, copy what is useful, delete the "
                    "rest.",
                ],
            ),
            (
                "One department, four things with one name",
                [
                    (
                        "bullet",
                        "Project tag “Sample dept”: groups the department's projects in the "
                        "Tasks sidebar (open a project, then Edit tags).",
                    ),
                    (
                        "bullet",
                        "Mention group @sample-dept: reaches everyone in the department "
                        "(Settings, Mention groups).",
                    ),
                    (
                        "bullet",
                        "Team folder “Sample dept”: this folder, under Team Notes, holds what "
                        "the department knows.",
                    ),
                    (
                        "bullet",
                        "Group chat “sample-dept · general”: the open chat for the department.",
                    ),
                ],
            ),
            (
                "Keeping something inside a smaller group",
                [
                    "Make it private. A private project or group chat is joined only with the "
                    "owner's approval, and a private team folder opens only for the people you "
                    "add to it. The “Sample dept: leads only (private)” folder, the “Hiring "
                    "plan (private)” project and the “sample-dept · leads (private)” chat are "
                    "examples.",
                ],
            ),
            (
                "Where does it go?",
                [
                    ("bullet", "Talking about one piece of work: that project's chat."),
                    ("bullet", "A decision or spec for one task: the task's note."),
                    (
                        "bullet",
                        "What the whole department should know: a note in this folder.",
                    ),
                    ("bullet", "A meeting recap: a note, like the example in Meeting notes."),
                    ("bullet", "Your own drafts and your day: My Notes and To-Do."),
                ],
            ),
            (
                "Deleting the samples",
                [
                    "Delete the two “Sample dept” projects (project menu, Delete Project), the "
                    "two “Sample dept” folders (Delete folder), the “Sample dept” tag (open a "
                    "project, Edit tags, Delete tag) and the @sample-dept mention group "
                    "(Settings, Mention groups, Delete group). The two group chats can only be "
                    "left for now (Leave group).",
                ],
            ),
        ),
        "meeting_title": "Meeting notes (example)",
        "meeting": _body(
            ("Date and who was there", [("bullet_placeholder", "Date, names")]),
            ("Decisions", [("bullet_placeholder", "What we decided, and why")]),
            ("Actions", [("bullet_placeholder", "Who does what by when")]),
        ),
        "private_title": "Only the people you add can open this folder",
        "private": _body(
            (
                "Private on purpose",
                [
                    "This folder is private: it opens only for the people you add to it. Put "
                    "here what one smaller group, such as the department's leads, keeps to "
                    "itself. Everything in a team folder that is not private is open to the "
                    "whole team.",
                ],
            ),
        ),
        "project": "Sample dept: Website refresh",
        "project_milestone": _milestone(
            "Sample milestone: refresh the website",
            "Show how one piece of the department's work is planned in a project.",
            "Each task is an example. Tick them off, change them, or delete this project.",
        ),
        "project_tasks": [
            _task(
                "Draft the new homepage text",
                _body(
                    (
                        "🧾 What to do",
                        [
                            "Write the draft in the note of this task, so the text stays with "
                            "the work.",
                        ],
                    ),
                ),
            ),
            _task(
                "Ask for feedback in the sample-dept chat",
                _body(
                    (
                        "🧾 What to do",
                        [
                            "Talk about this one piece of work in the project's chat. Use the "
                            "“sample-dept · general” group chat for what concerns the whole "
                            "department.",
                        ],
                    ),
                ),
            ),
            _task(
                "Publish it and tell the department",
                _body(
                    (
                        "🧾 What to do",
                        [
                            "Mention @sample-dept in a comment or a chat message: everyone in "
                            "the group is notified.",
                        ],
                    ),
                ),
                due_offset_days=7,
            ),
        ],
        "private_project": "Sample dept: Hiring plan (private)",
        "private_project_milestone": _milestone(
            "Sample milestone: open roles",
            "Show a project only a few people may join.",
            "This project is private: it is joined only with your approval.",
        ),
        "private_project_tasks": [
            _task(
                "Write the job description",
                _body(("🧾 What to do", ["An example task in a private project."])),
            ),
            _task(
                "Agree the salary range",
                _body(
                    (
                        "🧾 What to do",
                        ["The kind of work that belongs in a private project, not in the open."],
                    ),
                ),
                due_offset_days=7,
            ),
        ],
        "chat_general": "sample-dept · general",
        "chat_general_hello": (
            "👋 This is the sample department's open group chat. Talk here about what "
            "concerns the whole department; talk about one piece of work in that project's "
            "chat. When you no longer need it, leave the group."
        ),
        "chat_private": "sample-dept · leads (private)",
        "chat_private_hello": (
            "🔒 This is a private group chat: only people you add, or whose request you "
            "approve, can join. Use one for what a small group, such as the leads, keeps to "
            "itself."
        ),
        "todo_category": "Sample data",
        "todo_title": "Look through the sample dept, then delete what you do not need",
        "todo_notes": (
            "Open the “How the sample dept keeps its work tidy” note in Team Notes: it says "
            "what each sample is for and how to delete it."
        ),
    },
    "ja": {
        "label": "サンプル部署",
        "group_description": "サンプル部署の全員",
        "folder": "サンプル部署",
        "subfolder": "議事録",
        "private_folder": "サンプル部署：リーダー限定（非公開）",
        "overview_title": "サンプル部署の仕事の整理のしかた",
        "overview": _body(
            (
                "これは何か",
                [
                    "チームを作ったときに用意した例のデータです。部署をどう整えられるかを"
                    "見るためのものです。眺めて、使えるものは真似して、残りは削除してください。",
                ],
            ),
            (
                "ひとつの部署＝同じ名前の4つ",
                [
                    (
                        "bullet",
                        "プロジェクトのタグ「サンプル部署」：部署のプロジェクトをタスクの"
                        "サイドバーでまとめます（プロジェクトを開いて「タグを編集する」）。",
                    ),
                    (
                        "bullet",
                        "メンショングループ @sample-dept：部署の全員に届きます"
                        "（設定の「メンショングループ」）。",
                    ),
                    (
                        "bullet",
                        "チームのフォルダ「サンプル部署」：このフォルダ（チームノート内）に、"
                        "部署が知っておくことを置きます。",
                    ),
                    (
                        "bullet",
                        "グループチャット「サンプル部署 · general」：部署のオープンなチャットです。",
                    ),
                ],
            ),
            (
                "もっと小さなグループの中だけにしたいとき",
                [
                    "非公開にします。非公開のプロジェクトやグループチャットは、オーナーが"
                    "承認した人だけが参加でき、非公開のチームフォルダは追加した人だけが開けます。"
                    "「サンプル部署：リーダー限定（非公開）」フォルダ、「サンプル部署：採用計画"
                    "（非公開）」プロジェクト、「サンプル部署 · leads（非公開）」チャットが例です。",
                ],
            ),
            (
                "どこに書く？",
                [
                    ("bullet", "ひとつの仕事の話：そのプロジェクトのチャット。"),
                    ("bullet", "ひとつのタスクの決定や仕様：そのタスクのノート。"),
                    ("bullet", "部署の全員が知っておくこと：このフォルダのノート。"),
                    ("bullet", "会議のまとめ：議事録フォルダの例のようなノート。"),
                    ("bullet", "自分の下書きと一日の予定：マイノートとTo-Do。"),
                ],
            ),
            (
                "サンプルを削除するには",
                [
                    "「サンプル部署」の2つのプロジェクト（プロジェクトのメニューの"
                    "「プロジェクトを削除」）、2つのフォルダ（「フォルダを削除」）、"
                    "「サンプル部署」タグ（プロジェクトを開いて「タグを編集する」から「タグの削除」）、"
                    "メンショングループ @sample-dept（設定の「メンショングループ」から"
                    "「グループの削除」）を削除してください。2つのグループチャットは、今のところ"
                    "退出のみ可能です。",
                ],
            ),
        ),
        "meeting_title": "議事録（例）",
        "meeting": _body(
            ("日付と参加者", [("bullet_placeholder", "日付、名前")]),
            ("決定事項", [("bullet_placeholder", "何を、なぜ決めたか")]),
            ("アクション", [("bullet_placeholder", "誰が、いつまでに、何をするか")]),
        ),
        "private_title": "追加した人だけがこのフォルダを開けます",
        "private": _body(
            (
                "あえて非公開",
                [
                    "このフォルダは非公開で、追加した人だけが開けます。部署のリーダーなど、"
                    "小さなグループの中だけにしたいことをここに置きます。非公開でない"
                    "チームフォルダは、チーム全員に開かれています。",
                ],
            ),
        ),
        "project": "サンプル部署：Webサイト刷新",
        "project_milestone": _milestone(
            "サンプルのマイルストーン：Webサイトを刷新する",
            "部署の仕事のひとつを、プロジェクトでどう進めるかを示します。",
            "各タスクは例です。完了にしても、変更しても、プロジェクトごと削除してもかまいません。",
        ),
        "project_tasks": [
            _task(
                "トップページの文章の案を書く",
                _body(
                    (
                        "🧾 やること",
                        [
                            "このタスクのノートに案を書くと、文章が仕事と一緒に残ります。",
                        ],
                    ),
                ),
            ),
            _task(
                "sample-deptのチャットで意見をもらう",
                _body(
                    (
                        "🧾 やること",
                        [
                            "ひとつの仕事の話は、そのプロジェクトのチャットで。部署全体に関わる"
                            "ことは「サンプル部署 · general」グループチャットで話します。",
                        ],
                    ),
                ),
            ),
            _task(
                "公開して、部署に知らせる",
                _body(
                    (
                        "🧾 やること",
                        [
                            "コメントやチャットで @sample-dept にメンションすると、グループの"
                            "全員に通知が届きます。",
                        ],
                    ),
                ),
                due_offset_days=7,
            ),
        ],
        "private_project": "サンプル部署：採用計画（非公開）",
        "private_project_milestone": _milestone(
            "サンプルのマイルストーン：募集ポジション",
            "一部の人だけが参加できるプロジェクトを示します。",
            "このプロジェクトは非公開で、あなたが承認した人だけが参加できます。",
        ),
        "private_project_tasks": [
            _task(
                "求人票を書く",
                _body(("🧾 やること", ["非公開プロジェクトのタスクの例です。"])),
            ),
            _task(
                "給与レンジを決める",
                _body(
                    (
                        "🧾 やること",
                        ["オープンにせず、非公開のプロジェクトで扱うべき仕事の例です。"],
                    ),
                ),
                due_offset_days=7,
            ),
        ],
        "chat_general": "サンプル部署 · general",
        "chat_general_hello": (
            "👋 サンプル部署のオープンなグループチャットです。部署全体に関わることはここで、"
            "ひとつの仕事の話はそのプロジェクトのチャットで話します。不要になったら退出して"
            "ください。"
        ),
        "chat_private": "サンプル部署 · leads（非公開）",
        "chat_private_hello": (
            "🔒 非公開のグループチャットです。あなたが追加した人、または参加リクエストを"
            "承認した人だけが参加できます。リーダーなど、小さなグループの中だけにしたい話に"
            "使います。"
        ),
        "todo_category": "サンプルデータ",
        "todo_title": "サンプル部署を見て回り、不要なものを削除する",
        "todo_notes": (
            "チームノートの「サンプル部署の仕事の整理のしかた」を開くと、それぞれのサンプルの"
            "目的と削除のしかたが書いてあります。"
        ),
    },
}

PROJECT_TAGS = [("Sample", "#0ea5e9", "#ffffff")]


def example_locale(user) -> str:
    """`ja` for a Japanese account, English for everyone else (no other language is written)."""
    language = (getattr(user, "language", None) or "").strip().lower().split("-")[0]
    return "ja" if language == "ja" else "en"


def _create_team_note(team, user, folder, title, body) -> PersonalNoteMaster:
    """A note in a team folder, the way the notes API makes it: the note, the owner's explicit
    role row (the note APIs 403 without it) and the first version."""
    from origin.views.utils.note_version import snapshot_note_version

    note = PersonalNoteMaster.objects.create(
        team=team, owner=user, title=title, body=body, folder_id=folder.folder_id
    )
    NotePermissionMaster.objects.create(
        team=team, user=user, note_id=note.note_id, note_type=NOTE_TYPE_PERSONAL, role_id=ROLE_OWNER
    )
    snapshot_note_version(
        team=team,
        editor=user,
        note_type=NOTE_TYPE_PERSONAL,
        note_id=note.note_id,
        title=title,
        body=body,
    )
    return note


def _create_project(user, team, name, milestone_spec, task_specs, label, is_private, taken_codes):
    from origin.services.project_code import derive_project_code

    project = ProjectMaster.objects.create(
        team=team,
        project_name=name,
        code=derive_project_code(name, taken_codes),
        owner=user,
        project_system_user=user,
        is_private=is_private,
    )
    taken_codes.add(project.code)
    # Plain .create() so the project-chat membership signal fires; bulk_create would skip it.
    ProjectMembers.objects.create(team=team, project=project, attendee=user)
    ProjectTags.objects.bulk_create(
        [
            ProjectTags(
                team=team,
                project=project,
                tag_id=index + 1,
                tag_name=tag_name,
                tag_color=color,
                tag_text_color=text_color,
            )
            for index, (tag_name, color, text_color) in enumerate(PROJECT_TAGS)
        ]
    )
    ProjectLabelAssignment.objects.create(label=label, project=project)
    sprint = _create_sprint(team, project)
    milestone, backing_task = _create_milestone_with_backing_task(
        team, project, sprint, user, [], milestone_spec
    )
    _create_blueprint_tasks(team, project, sprint, milestone, backing_task, user, [], task_specs)
    return project


def _create_group_chat(team, user, title, hello, is_private) -> Channel:
    channel = Channel.objects.create(
        team=team, kind=ChannelKind.GM, title=title, is_private=is_private, owner=user
    )
    ChannelMember.objects.create(channel=channel, user=user, role="owner")
    Message.objects.create(
        channel=channel,
        sender=user,
        seq=1,
        body=_text_body(hello),
        body_text=hello,
        metadata={},
    )
    return channel


def create_organisation_examples(user, team) -> None:
    """Make the example department for `user`'s new `team`. Raises on failure; the caller keeps
    the team and the rest of the starter either way."""
    copy = COPY[example_locale(user)]

    with transaction.atomic():
        label = ProjectLabel.objects.create(
            team=team,
            name=copy["label"],
            color=LABEL_COLOR,
            text_color=LABEL_TEXT_COLOR,
            created_by=user,
        )
        group = MentionGroupMaster.objects.create(
            team=team,
            group_name=GROUP_NAME,
            description=copy["group_description"],
            created_by=user,
        )
        MentionGroupMembers.objects.create(team=team, group=group, user=user, added_by=user)

        folder, _ = create_team_folder(
            team_id=str(team.team_id), user_id=user.id, name=copy["folder"], visibility="public"
        )
        _create_team_note(team, user, folder, copy["overview_title"], copy["overview"])
        subfolder, _ = create_team_folder(
            team_id=str(team.team_id),
            user_id=user.id,
            name=copy["subfolder"],
            parent_folder_id=folder.folder_id,
        )
        _create_team_note(team, user, subfolder, copy["meeting_title"], copy["meeting"])
        private_folder, _ = create_team_folder(
            team_id=str(team.team_id),
            user_id=user.id,
            name=copy["private_folder"],
            visibility="private",
        )
        _create_team_note(team, user, private_folder, copy["private_title"], copy["private"])

        taken_codes = set(
            ProjectMaster.objects.filter(team=team, code__isnull=False).values_list(
                "code", flat=True
            )
        )
        _create_project(
            user,
            team,
            copy["project"],
            copy["project_milestone"],
            copy["project_tasks"],
            label,
            False,
            taken_codes,
        )
        _create_project(
            user,
            team,
            copy["private_project"],
            copy["private_project_milestone"],
            copy["private_project_tasks"],
            label,
            True,
            taken_codes,
        )

        _create_group_chat(team, user, copy["chat_general"], copy["chat_general_hello"], False)
        _create_group_chat(team, user, copy["chat_private"], copy["chat_private_hello"], True)

        group_today = ToDoGroup.objects.filter(
            team=team, user=user, local_date=user_today(user)
        ).first()
        if group_today is None:
            group_today = ToDoGroup.objects.create(
                team=team, user=user, local_date=user_today(user), is_completed=False
            )
        category = ToDoCategory.objects.create(
            team=team, user=user, name=copy["todo_category"], sort_order=2
        )
        ToDoItem.objects.create(
            group=group_today,
            category=category,
            title=copy["todo_title"],
            sort_order=0,
            notes=_text_body(copy["todo_notes"]),
        )
