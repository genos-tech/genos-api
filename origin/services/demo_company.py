"""The company layer of the v2 demo workspace (MIP-1692, under MIP-1447).

The visitor's demo (v2) is one small product team. A real company has departments, many more
projects and chats than any one person joins, and things only a few people may see. This layer
puts that on top of the SAME story and the SAME four people, so a visitor meets the model Genos
recommends (decided 2026-11-18: one team per company; a department is a project tag + a mention
group + a team folder + an open group chat; the private version of each for a smaller group):

* four departments, one per person in the story: Product (Alice), Engineering (Bob), Design
  (Carol), Quality (Dan). Each has a project tag, a mention group, an open team folder with a note,
  and an open chat;
* four more projects, so the workspace has six and the visitor is in three of them (the point of
  "join little, find everything"); one of the six is private;
* a private team folder and a private group chat, which the visitor is not in.

It runs ONLY for v2 (`create_demo_environment(version="v2")`, i.e. visitors). v1 is the eval
corpus and never imports this module, so `test_demo_v1_golden` and the eval fixture are
untouched. It adds no rows that the story's own tables describe (no change to a v1 constant) and
never changes the two projects the story already has, except that it gives them department tags.

Everything here is cleaned up with the team: `delete_demo_team_data` removes labels, mention
groups, team folders, chats and projects (MIP-1702 made the non-cascading ones sure).
"""

from __future__ import annotations

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.services import demo_v2
from origin.services.demo_blocks import _body, _text_body
from origin.services.demo_seeder import (
    _acting_as,
    _create_blueprint_tasks,
    _create_milestone_with_backing_task,
    _create_sprint,
)
from origin.services.note_folder_manage import create_team_folder
from origin.services.organisation_examples import _create_team_note

# The story's four people, by index into `bots` (see `BOT_PROFILES`): Alice Product Lead,
# Bob Engineer, Carol Designer, Dan QA. A department's lead is the person whose job it is.
PRODUCT, ENGINEERING, DESIGN, QUALITY = 0, 1, 2, 3

# What the visitor joins, out of what exists (the lesson is that it is a few of many).
VISITOR_GROUPS = (PRODUCT, DESIGN)
VISITOR_CHATS = (PRODUCT, DESIGN)

# The two projects the story already has, in blueprint order, and the departments that own them.
STORY_PROJECT_DEPARTMENTS = ((DESIGN, ENGINEERING), (PRODUCT,))

TAG_COLORS = ["#7c3aed", "#0ea5e9", "#10b981", "#f59e0b"]  # Product, Engineering, Design, Quality
TAG_TEXT = "#ffffff"
GROUP_SLUGS = ("product", "engineering", "design", "quality")


def _task(
    title, body, assignee, status="Open", priority="Normal", due=7, effort="Moderate", tags=(0,)
):
    return {
        "title": title,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "is_milestone_child": True,
        "parent_idx": None,
        "due_offset_days": due,
        "body": _body(("🧾 Summary", [body])),
        "effort_level": effort,
        "_tag_indexes": tags,
    }


def _milestone(title, goal, due=21):
    return {
        "title": title,
        "body": _body(("🎯 Goal", [goal])),
        "status": "WIP",
        "priority": "Normal",
        "due_offset_days": due,
    }


COPY = {
    "en": {
        "departments": ["Product", "Engineering", "Design", "Quality"],
        "group_descriptions": [
            "Everyone in Product",
            "Everyone in Engineering",
            "Everyone in Design",
            "Everyone in Quality",
        ],
        "folder_notes": [
            (
                "How Product works with the other teams",
                _body(
                    (
                        "How we work",
                        [
                            ("bullet", "Priorities for the quarter live in the Roadmap project."),
                            (
                                "bullet",
                                "A decision goes in the note of its task, so it stays on the work.",
                            ),
                            (
                                "bullet",
                                "Questions for the whole team: @product in the chat or a comment.",
                            ),
                        ],
                    ),
                ),
            ),
            (
                "Release checklist",
                _body(
                    (
                        "Before a release",
                        [
                            ("bullet", "Every task in the milestone is Closed or moved out."),
                            ("bullet", "QA has signed off in the Quality chat."),
                            ("bullet", "Say what shipped in the Engineering chat."),
                        ],
                    ),
                ),
            ),
            (
                "Design review: how and when",
                _body(
                    (
                        "How we review",
                        [
                            ("bullet", "Post the frames in the task, not in a private message."),
                            (
                                "bullet",
                                "Reviews are Tuesday and Thursday; @design for a quick one.",
                            ),
                            ("bullet", "The decision is written on the task when the review ends."),
                        ],
                    ),
                ),
            ),
            (
                "Bug triage rules",
                _body(
                    (
                        "How we triage",
                        [
                            ("bullet", "Every bug gets a priority within a day."),
                            (
                                "bullet",
                                "A bug that blocks a release is tagged and raised in @quality.",
                            ),
                            ("bullet", "Flaky tests are reported every Monday, not fixed ad hoc."),
                        ],
                    ),
                ),
            ),
        ],
        "overview_title": "How this workspace is organised",
        "overview": _body(
            (
                "One team, four departments",
                [
                    "This workspace is one Genos team. Each department is four things with "
                    "one name: a project tag, a mention group (@product, @engineering, "
                    "@design, @quality), a folder in Team Notes and an open group chat.",
                ],
            ),
            (
                "Join little, find everything",
                [
                    "You are in three of the six projects and two of the four department "
                    "chats. Press Cmd-K and type # or @ to find the rest, or look under Join "
                    "Project. The leads' chat, folder and the OKR project are private.",
                ],
            ),
        ),
        "chat_suffix": "general",
        "chat_messages": [
            [
                (
                    PRODUCT,
                    "Roadmap review is Thursday. Please put open questions in the Roadmap project first.",
                ),
                (ENGINEERING, "Will do. Engineering has two spikes to add before then."),
            ],
            [
                (
                    ENGINEERING,
                    "The release train leaves Friday. Anything not Closed by Thursday moves out.",
                ),
                (QUALITY, "I will post the sign-off in the Quality chat before the cut."),
            ],
            [
                (
                    DESIGN,
                    "Design review is Tuesday and Thursday. Post your frames on the task, not here.",
                ),
                (PRODUCT, "Thanks. The hero section is first on Tuesday."),
            ],
            [
                (
                    QUALITY,
                    "Flaky test report goes out every Monday. Reply here if one is blocking you.",
                ),
                (ENGINEERING, "Two on the sign-in suite, looking at them today."),
            ],
        ],
        "private_chat": "leads (private)",
        "private_chat_messages": [
            (
                PRODUCT,
                "Headcount plan is in the Leadership OKRs project. Please keep it in there, not in the open chats.",
            ),
            (ENGINEERING, "Understood. I will add the infra hiring ask today."),
        ],
        "private_folder": "Leadership (private)",
        "private_note": (
            "Quarter priorities (leads only)",
            _body(
                (
                    "Why private",
                    [
                        "Only the leads can open this folder. Anything here (budget, hiring, "
                        "changes before they are announced) stays out of the team's search too.",
                    ],
                ),
            ),
        ),
        "projects": [
            {
                "name": "Design system",
                "department": DESIGN,
                "owner": DESIGN,
                "members": (DESIGN, ENGINEERING),
                "visitor": True,
                "private": False,
                "tags": [("Components", "#7c3aed", "#ffffff"), ("Docs", "#0ea5e9", "#ffffff")],
                "milestone": _milestone(
                    "Design system v1",
                    "One set of components and tokens that product and engineering both use.",
                ),
                "tasks": [
                    _task(
                        "Audit the button and form components",
                        "List what exists and what differs.",
                        "bot2",
                        "Closed",
                        "Normal",
                        2,
                        "Moderate",
                        (0,),
                    ),
                    _task(
                        "Publish colour and spacing tokens",
                        "One source for both design and code.",
                        "bot2",
                        "WIP",
                        "High",
                        5,
                        "High",
                        (0,),
                    ),
                    _task(
                        "Write usage guidelines",
                        "When to use each component, with examples.",
                        "bot1",
                        "Open",
                        "Normal",
                        14,
                        "Moderate",
                        (1,),
                    ),
                ],
            },
            {
                "name": "Mobile app",
                "department": ENGINEERING,
                "owner": ENGINEERING,
                "members": (ENGINEERING, DESIGN, QUALITY),
                "visitor": False,
                "private": False,
                "tags": [("iOS", "#0ea5e9", "#ffffff"), ("Android", "#10b981", "#ffffff")],
                "milestone": _milestone("Mobile beta", "A beta that the team can use every day."),
                "tasks": [
                    _task(
                        "Decide the push notification categories",
                        "Which events are worth a push, and which are not.",
                        "bot1",
                        "Open",
                        "High",
                        6,
                        "Moderate",
                        (0, 1),
                    ),
                    _task(
                        "Prototype the offline task list",
                        "Open and edit tasks with no connection.",
                        "bot1",
                        "WIP",
                        "Normal",
                        12,
                        "High",
                        (0,),
                    ),
                ],
            },
            {
                "name": "QA automation",
                "department": QUALITY,
                "owner": QUALITY,
                "members": (QUALITY, ENGINEERING),
                "visitor": False,
                "private": False,
                "tags": [("Suite", "#f59e0b", "#ffffff"), ("Flaky", "#ef4444", "#ffffff")],
                "milestone": _milestone(
                    "Stable regression suite", "Every release is signed off by the suite first."
                ),
                "tasks": [
                    _task(
                        "Automate the sign-in regression suite",
                        "Cover password, OAuth and invite sign-in.",
                        "bot3",
                        "WIP",
                        "High",
                        8,
                        "High",
                        (0,),
                    ),
                    _task(
                        "Report flaky tests every Monday",
                        "One list, one owner, one place.",
                        "bot3",
                        "Open",
                        "Normal",
                        4,
                        "Low",
                        (1,),
                    ),
                ],
            },
            {
                "name": "Leadership OKRs (private)",
                "department": PRODUCT,
                "owner": PRODUCT,
                "members": (PRODUCT, ENGINEERING, DESIGN, QUALITY),
                "visitor": False,
                "private": True,
                "tags": [("OKR", "#7c3aed", "#ffffff"), ("Hiring", "#f59e0b", "#ffffff")],
                "milestone": _milestone(
                    "Next quarter's OKRs",
                    "Agree the company's objectives before they are announced.",
                ),
                "tasks": [
                    _task(
                        "Draft next quarter's company OKRs",
                        "Three objectives, each with measurable results.",
                        "bot0",
                        "WIP",
                        "High",
                        9,
                        "High",
                        (0,),
                    ),
                    _task(
                        "Review the headcount plan",
                        "Which roles, which order, and what it costs.",
                        "bot0",
                        "Open",
                        "Normal",
                        15,
                        "Moderate",
                        (1,),
                    ),
                ],
            },
        ],
    },
    "ja": {
        "departments": ["プロダクト", "エンジニアリング", "デザイン", "品質保証"],
        "group_descriptions": [
            "プロダクト部の全員",
            "エンジニアリング部の全員",
            "デザイン部の全員",
            "品質保証部の全員",
        ],
        "folder_notes": [
            (
                "プロダクト部と他のチームの進め方",
                _body(
                    (
                        "進め方",
                        [
                            (
                                "bullet",
                                "四半期の優先事項は、ロードマップのプロジェクトにあります。",
                            ),
                            ("bullet", "決定は、そのタスクのノートに書き、仕事のそばに残します。"),
                            (
                                "bullet",
                                "チーム全体への質問は、チャットやコメントで @product を使います。",
                            ),
                        ],
                    ),
                ),
            ),
            (
                "リリースのチェックリスト",
                _body(
                    (
                        "リリース前に",
                        [
                            ("bullet", "マイルストーンのタスクはすべて完了にするか、外します。"),
                            ("bullet", "品質保証部のチャットで承認が出ています。"),
                            (
                                "bullet",
                                "リリースした内容を、エンジニアリングのチャットで共有します。",
                            ),
                        ],
                    ),
                ),
            ),
            (
                "デザインレビューの進め方",
                _body(
                    (
                        "レビューのしかた",
                        [
                            ("bullet", "フレームは個別メッセージではなく、タスクに投稿します。"),
                            ("bullet", "レビューは火曜と木曜。急ぎは @design へ。"),
                            ("bullet", "レビューが終わったら、決定をタスクに書きます。"),
                        ],
                    ),
                ),
            ),
            (
                "バグのトリアージ規則",
                _body(
                    (
                        "トリアージのしかた",
                        [
                            ("bullet", "すべてのバグに、一日以内に優先度を付けます。"),
                            ("bullet", "リリースを止めるバグはタグを付け、@quality に知らせます。"),
                            ("bullet", "不安定なテストは、毎週月曜にまとめて報告します。"),
                        ],
                    ),
                ),
            ),
        ],
        "overview_title": "このワークスペースの整え方",
        "overview": _body(
            (
                "ひとつのチーム、4つの部署",
                [
                    "このワークスペースはひとつのGenosチームです。各部署は、同じ名前の4つで"
                    "できています。プロジェクトのタグ、メンショングループ（@product、"
                    "@engineering、@design、@quality）、チームノートのフォルダ、オープンな"
                    "グループチャットです。",
                ],
            ),
            (
                "少しだけ参加して、すべてを見つける",
                [
                    "6つのプロジェクトのうち3つ、4つの部署チャットのうち2つに参加しています。"
                    "残りは Cmd-K で # や @ を入力するか、「プロジェクトに参加」から見つけます。"
                    "リーダーのチャットとフォルダ、OKRのプロジェクトは非公開です。",
                ],
            ),
        ),
        "chat_suffix": "general",
        "chat_messages": [
            [
                (
                    PRODUCT,
                    "ロードマップレビューは木曜です。未解決の質問は、先にロードマップのプロジェクトに入れてください。",
                ),
                (ENGINEERING, "了解です。それまでに、エンジニアリングのスパイクを2つ追加します。"),
            ],
            [
                (
                    ENGINEERING,
                    "リリーストレインは金曜に出ます。木曜までに完了していないものは外します。",
                ),
                (QUALITY, "カットの前に、品質保証のチャットで承認を投稿します。"),
            ],
            [
                (
                    DESIGN,
                    "デザインレビューは火曜と木曜です。フレームはここではなく、タスクに投稿してください。",
                ),
                (PRODUCT, "ありがとうございます。火曜はヒーローセクションが最初です。"),
            ],
            [
                (
                    QUALITY,
                    "不安定なテストのレポートは毎週月曜に出します。困っているものがあれば、ここに返信してください。",
                ),
                (ENGINEERING, "サインインのテストが2件です。今日確認します。"),
            ],
        ],
        "private_chat": "リーダー（非公開）",
        "private_chat_messages": [
            (
                PRODUCT,
                "人員計画は「経営OKR」プロジェクトにあります。オープンなチャットではなく、そちらに入れてください。",
            ),
            (ENGINEERING, "了解です。今日、インフラの採用依頼を追加します。"),
        ],
        "private_folder": "リーダー（非公開）",
        "private_note": (
            "四半期の優先事項（リーダー限定）",
            _body(
                (
                    "非公開にしている理由",
                    [
                        "このフォルダはリーダーだけが開けます。ここにあるもの（予算、採用、発表前の"
                        "変更）は、チームの検索にも出ません。",
                    ],
                ),
            ),
        ),
        "projects": [
            {
                "name": "デザインシステム",
                "department": DESIGN,
                "owner": DESIGN,
                "members": (DESIGN, ENGINEERING),
                "visitor": True,
                "private": False,
                "tags": [
                    ("コンポーネント", "#7c3aed", "#ffffff"),
                    ("ドキュメント", "#0ea5e9", "#ffffff"),
                ],
                "milestone": _milestone(
                    "デザインシステム v1",
                    "プロダクトとエンジニアリングの両方が使う、ひとつのコンポーネントとトークンのセット。",
                ),
                "tasks": [
                    _task(
                        "ボタンとフォームのコンポーネントを棚卸しする",
                        "何があり、何が違うかを一覧にします。",
                        "bot2",
                        "Closed",
                        "Normal",
                        2,
                        "Moderate",
                        (0,),
                    ),
                    _task(
                        "色と余白のトークンを公開する",
                        "デザインとコードで、ひとつの情報源にします。",
                        "bot2",
                        "WIP",
                        "High",
                        5,
                        "High",
                        (0,),
                    ),
                    _task(
                        "使い方のガイドラインを書く",
                        "各コンポーネントをいつ使うか、例と一緒に書きます。",
                        "bot1",
                        "Open",
                        "Normal",
                        14,
                        "Moderate",
                        (1,),
                    ),
                ],
            },
            {
                "name": "モバイルアプリ",
                "department": ENGINEERING,
                "owner": ENGINEERING,
                "members": (ENGINEERING, DESIGN, QUALITY),
                "visitor": False,
                "private": False,
                "tags": [("iOS", "#0ea5e9", "#ffffff"), ("Android", "#10b981", "#ffffff")],
                "milestone": _milestone("モバイルのベータ版", "チームが毎日使えるベータ版。"),
                "tasks": [
                    _task(
                        "プッシュ通知の種類を決める",
                        "どのイベントをプッシュにする価値があるか、ないかを決めます。",
                        "bot1",
                        "Open",
                        "High",
                        6,
                        "Moderate",
                        (0, 1),
                    ),
                    _task(
                        "オフラインのタスク一覧を試作する",
                        "接続なしでタスクを開いて編集できるようにします。",
                        "bot1",
                        "WIP",
                        "Normal",
                        12,
                        "High",
                        (0,),
                    ),
                ],
            },
            {
                "name": "QA自動化",
                "department": QUALITY,
                "owner": QUALITY,
                "members": (QUALITY, ENGINEERING),
                "visitor": False,
                "private": False,
                "tags": [("スイート", "#f59e0b", "#ffffff"), ("不安定", "#ef4444", "#ffffff")],
                "milestone": _milestone(
                    "安定した回帰テストスイート", "すべてのリリースを、まずスイートが承認する。"
                ),
                "tasks": [
                    _task(
                        "サインインの回帰テストを自動化する",
                        "パスワード、OAuth、招待からのサインインを網羅します。",
                        "bot3",
                        "WIP",
                        "High",
                        8,
                        "High",
                        (0,),
                    ),
                    _task(
                        "不安定なテストを毎週月曜に報告する",
                        "ひとつのリスト、ひとりの担当、ひとつの場所。",
                        "bot3",
                        "Open",
                        "Normal",
                        4,
                        "Low",
                        (1,),
                    ),
                ],
            },
            {
                "name": "経営OKR（非公開）",
                "department": PRODUCT,
                "owner": PRODUCT,
                "members": (PRODUCT, ENGINEERING, DESIGN, QUALITY),
                "visitor": False,
                "private": True,
                "tags": [("OKR", "#7c3aed", "#ffffff"), ("採用", "#f59e0b", "#ffffff")],
                "milestone": _milestone("来四半期のOKR", "発表前に、会社の目標を合意する。"),
                "tasks": [
                    _task(
                        "来四半期の全社OKRの案を作る",
                        "3つの目標と、それぞれの測定可能な成果。",
                        "bot0",
                        "WIP",
                        "High",
                        9,
                        "High",
                        (0,),
                    ),
                    _task(
                        "人員計画をレビューする",
                        "どの職種を、どの順で、いくらで採るか。",
                        "bot0",
                        "Open",
                        "Normal",
                        15,
                        "Moderate",
                        (1,),
                    ),
                ],
            },
        ],
    },
}


def _chat(team, title, owner, members, messages, *, is_private):
    """A group chat owned by `owner`, with `members` (users) in it and `messages` ((user, text)
    pairs) in order. Sequence numbers start at 1, like every seeded channel."""
    channel = Channel.objects.create(
        team=team, kind=ChannelKind.GM, title=title, is_private=is_private, owner=owner
    )
    ChannelMember.objects.create(channel=channel, user=owner, role="owner")
    for user in members:
        if user.id != owner.id:
            ChannelMember.objects.create(channel=channel, user=user, role="member")
    for seq, (sender, text) in enumerate(messages, start=1):
        Message.objects.create(
            channel=channel,
            sender=sender,
            seq=seq,
            body=_text_body(text),
            body_text=text,
            metadata={},
        )
    return channel


def _create_extra_project(team, demo_user, bots, spec, label, sprint_name, taken_codes):
    """One extra project: its members, a tag set, one sprint, one milestone and its tasks. The
    department lead owns it (and is the reporter), the visitor is a member only when `visitor`."""
    from origin.services.project_code import derive_project_code

    lead = bots[spec["owner"]]
    project = ProjectMaster.objects.create(
        team=team,
        project_name=spec["name"],
        code=derive_project_code(spec["name"], taken_codes),
        owner=lead,
        project_system_user=lead,
        is_private=spec["private"],
    )
    taken_codes.add(project.code)
    members = [bots[i] for i in spec["members"]] + ([demo_user] if spec["visitor"] else [])
    for user in members:
        # `.create`, not bulk: the project-chat membership signal fires on save.
        ProjectMembers.objects.create(team=team, project=project, attendee=user)
    ProjectTags.objects.bulk_create(
        [
            ProjectTags(
                team=team,
                project=project,
                tag_id=index + 1,
                tag_name=name,
                tag_color=color,
                tag_text_color=text_color,
            )
            for index, (name, color, text_color) in enumerate(spec["tags"])
        ]
    )
    ProjectLabelAssignment.objects.create(label=label, project=project)

    task_specs = []
    for task in spec["tasks"]:
        task = dict(task)
        task["tags_json"] = demo_v2.task_tags(spec["tags"], task.pop("_tag_indexes"))
        task_specs.append(task)
    sprint = _create_sprint(team, project, name_template=sprint_name)
    with _acting_as(lead):
        milestone, backing = _create_milestone_with_backing_task(
            team, project, sprint, lead, bots, spec["milestone"]
        )
        _create_blueprint_tasks(team, project, sprint, milestone, backing, lead, bots, task_specs)
    return project


def create_company_layer(team, demo_user, bots, lang, seeded_projects, sprint_name) -> None:
    """Add the departments, extra projects and private things to a v2 demo workspace.

    `seeded_projects` are the two story projects (`_create_project_from_blueprint` results, in
    blueprint order); they only get department tags. Runs inside `create_demo_environment`'s
    transaction.
    """
    copy = COPY.get(lang, COPY["en"])
    departments = copy["departments"]

    labels = []
    for index, name in enumerate(departments):
        labels.append(
            ProjectLabel.objects.create(
                team=team,
                name=name,
                color=TAG_COLORS[index],
                text_color=TAG_TEXT,
                created_by=bots[index],
            )
        )
        group = MentionGroupMaster.objects.create(
            team=team,
            group_name=GROUP_SLUGS[index],
            description=copy["group_descriptions"][index],
            created_by=bots[index],
        )
        MentionGroupMembers.objects.create(
            team=team, group=group, user=bots[index], added_by=bots[index]
        )
        if index in VISITOR_GROUPS:
            MentionGroupMembers.objects.create(
                team=team, group=group, user=demo_user, added_by=bots[index]
            )

    # The two projects the story already has get the departments that own them.
    for seeded, owners in zip(seeded_projects, STORY_PROJECT_DEPARTMENTS):
        for index in owners:
            ProjectLabelAssignment.objects.create(label=labels[index], project=seeded["project"])

    for index, name in enumerate(departments):
        lead = bots[index]
        folder, _ = create_team_folder(
            team_id=str(team.team_id), user_id=lead.id, name=name, visibility="public"
        )
        title, body = copy["folder_notes"][index]
        _create_team_note(team, lead, folder, title, body)
        if index == PRODUCT:
            _create_team_note(team, lead, folder, copy["overview_title"], copy["overview"])

        members = list(bots) + ([demo_user] if index in VISITOR_CHATS else [])
        _chat(
            team,
            f"{name} · {copy['chat_suffix']}",
            lead,
            members,
            [(bots[who], text) for who, text in copy["chat_messages"][index]],
            is_private=False,
        )

    # What only the leads may see: a folder, a chat and a project. The visitor is in none of them.
    lead = bots[PRODUCT]
    private_folder, _ = create_team_folder(
        team_id=str(team.team_id),
        user_id=lead.id,
        name=copy["private_folder"],
        visibility="private",
    )
    note_title, note_body = copy["private_note"]
    _create_team_note(team, lead, private_folder, note_title, note_body)
    _chat(
        team,
        copy["private_chat"],
        lead,
        list(bots),
        [(bots[who], text) for who, text in copy["private_chat_messages"]],
        is_private=True,
    )

    taken_codes = set(
        ProjectMaster.objects.filter(team=team, code__isnull=False).values_list("code", flat=True)
    )
    for spec in copy["projects"]:
        _create_extra_project(
            team, demo_user, bots, spec, labels[spec["department"]], sprint_name, taken_codes
        )
