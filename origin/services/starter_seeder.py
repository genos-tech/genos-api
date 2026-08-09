"""Opt-in starter workspace for a brand-new REAL team.

The readiness doc's §3.3 finding: a new team lands in a blank app with
no first action. The demo environment already proves seeded content
works — but it cannot be reused directly, because (a) `is_demo=True` is
a self-destruct flag (LogoutView deletes the environment; a daily
sweeper reaps it), (b) its content is written around four bot teammates
who don't exist in a real team (both as assignees and inside ~30 prose
references), and (c) it *creates* its team rather than accepting one.

So this module reuses the demo seeder's MACHINERY — sprint, milestone
+ backing task, blueprint tasks — with small, self-contained blueprints
whose voice is onboarding ("learn Genos by doing") rather than fiction:
every task is assigned to the real owner, nothing sets `is_demo`, and
the team is injected, never created. Deleting the seeded project is
itself one of the tasks, so opting in is fully reversible.

Every user gets separate English and Japanese projects, each with two
milestones: the starter blueprint is the setup week (get the team in,
make something real), and the examples blueprint is one worked example
per trade — engineering, planning, marketing, research, studying,
operations — because signup tells us nothing about what the person
does, and a tour written for engineers reads as "not for me" to
everyone else.

Called (best-effort) from `TeamMasterView.post` when the client sends
`with_starter: true`. A seeding failure must never lose the team —
the caller wraps this in its own try/except and returns the team
regardless.

The eval fixture (`agent/evals/fixture.py`) depends on the DEMO
seeder's byte-identical output; this module deliberately shares only
helpers whose output is fully blueprint-driven, so it cannot drift the
fixture.
"""

from __future__ import annotations

import logging

from django.db import transaction

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.task_models import TaskDependency
from origin.services.demo_seeder import (
    _body,
    _create_blueprint_tasks,
    _create_milestone_with_backing_task,
    _create_sprint,
    _text_body,
    kick_off_demo_reindex,
)
from origin.services.guide_notes import seed_guide_notes
from origin.services.user_time import user_today

log = logging.getLogger(__name__)

# One project that teaches the product by being used. Schema mirrors
# PROJECT_BLUEPRINTS (demo_seeder) — same keys, consumed by the same
# `_create_blueprint_tasks`; every assignee is "demo" (= the real
# owner), which is what makes it safe with `bots=[]`.
STARTER_BLUEPRINT = {
    "name": "Getting started with Genos",
    "tags": [
        ("Setup", "#7c3aed", "#ffffff"),
        ("Team", "#0ea5e9", "#ffffff"),
        ("Try it", "#f59e0b", "#ffffff"),
        ("Example", "#10b981", "#ffffff"),
    ],
    "milestone": {
        "title": "Your first week on Genos",
        "body": _body(
            (
                "🎯 Goal",
                [
                    "By the end of this milestone your team is set up, your first "
                    "real project exists, and everyone knows where work happens.",
                ],
            ),
            (
                "🪜 How to use this",
                [
                    "Each task below is one small step. Open a task to see the "
                    "details, tick it off when done — and when the whole list is "
                    "done, delete this project. It's yours to break.",
                ],
            ),
        ),
        "status": "Open",
        "priority": "High",
        "due_offset_days": 7,
    },
    "tasks": [
        {
            "title": "Invite your teammates",
            "status": "Open",
            "priority": "High",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 1,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "Open the team menu (your avatar, top left) and choose "
                        "Invite. Genos is chat + tasks + notes in one place, so it "
                        "gets useful the moment a second person is here.",
                    ],
                ),
            ),
        },
        {
            "title": "Create your first real project",
            "status": "Open",
            "priority": "High",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 2,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "In Tasks, create a project for something you're actually "
                        "working on. Every project gets its own chat channel "
                        "automatically, so discussion and work stay together.",
                    ],
                ),
            ),
        },
        {
            "title": "Ask Spotlight something about your workspace",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 3,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "Press Cmd-K (Ctrl-K on Windows) and ask something like "
                        '"what\'s due this week?". Spotlight reads your chats, '
                        "tasks and notes — the more you put in, the better the "
                        "answers get.",
                    ],
                ),
            ),
        },
        {
            "title": "Turn on notifications",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 3,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "Settings → Notifications. Allow browser notifications for "
                        "real-time pings; email fills in whenever you're away, in "
                        "one batched message rather than one per event.",
                    ],
                ),
            ),
        },
        {
            "title": "Connect Google Calendar or GitHub",
            "status": "Open",
            "priority": "Low",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 5,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "Settings → Integrations. Calendar events show up beside "
                        "your tasks; GitHub PRs link to the tasks they close.",
                    ],
                ),
            ),
        },
        {
            "title": "Done exploring? Delete this project",
            "status": "Open",
            "priority": "Low",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 7,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "This starter project is scaffolding, not furniture. Once "
                        "your real work lives here, delete it from the project "
                        "menu — everything it created goes with it.",
                    ],
                ),
            ),
        },
    ],
}

# A second milestone in the same project. The first one is setup ("get
# the team in, make a real project"); this one answers the question that
# follows it — "yes, but what do I actually DO with this?" — for someone
# who is not an engineer.
#
# Signing up does not tell us which kind of work a person does, and a
# tour written for one trade reads as "not for me" to the rest. So each
# task below is one trade's smallest real workflow, named by trade so it
# can be skimmed and mostly ignored: the point is that everyone finds
# their own row, not that anyone does all seven. Every one is a real
# action in the product, not reading, and the deeper version of each
# lives in the "Playbooks" page of the Genos Guide.
#
# Same schema and same helpers as STARTER_BLUEPRINT above; kept a
# separate constant rather than a second key so each dict stays exactly
# the shape `_create_milestone_with_backing_task` / `_create_blueprint_tasks`
# already consume.
EXAMPLES_BLUEPRINT = {
    "milestone": {
        "title": "Try Genos on real work",
        "body": _body(
            (
                "🎯 Goal",
                [
                    "Move from looking at Genos to using it, on something you "
                    "would have had to do anyway.",
                ],
            ),
            (
                "🪜 How to use this",
                [
                    "These are examples, not a checklist — find the one that "
                    "matches your job, do that one, and delete the rest. Each "
                    "takes a few minutes and leaves you with something real "
                    "rather than a tidy list.",
                ],
            ),
        ),
        "status": "Open",
        "priority": "Normal",
        "due_offset_days": 14,
    },
    "tasks": [
        {
            "title": "Engineering: link a pull request to a task",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 10,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Connect GitHub in Settings → Integrations, then paste a "
                        "pull request link onto a task. In Settings → Tasks you "
                        "can also have the task close itself when that PR "
                        "merges.",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "The board stops being a second thing to update by "
                        "hand. Try dependencies too: mark one task blocked by "
                        "another and Genos sets and clears the Blocked status "
                        "for you.",
                    ],
                ),
            ),
        },
        {
            "title": "Planning: turn a date you care about into a milestone",
            "status": "WIP",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 10,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Make a milestone for your next real deadline and attach "
                        "two or three tasks to it. It then tracks its own "
                        'progress, so the question "are we going to make it?" '
                        "has somewhere to be answered.",
                    ],
                ),
            ),
        },
        {
            "title": "…and split one of those tasks into sub-tasks",
            "status": "Open",
            "priority": "Low",
            "assignee": "demo",
            "is_milestone_child": False,
            "parent_idx": 1,
            "due_offset_days": 11,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Open a task and add sub-tasks for the steps inside it. "
                        "This task is itself a sub-task of the one above — that "
                        "indent in the table is what you're making.",
                    ],
                ),
            ),
        },
        {
            "title": "Marketing: draft something as a shared note",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 11,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Write your next post, email or brief as a note and share "
                        "it with a colleague as an editor. You can both type in "
                        "it at the same time and see each other's cursors.",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "Copy stops travelling as attachments. Keep the brief as "
                        "the task's own note so the instructions sit above the "
                        "drafts, and pin the approved version in the project's "
                        "channel so nobody ships last week's wording.",
                    ],
                ),
            ),
        },
        {
            "title": "Research: ask one long document its own questions",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 12,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Paste or import a long document as a note, then use the "
                        "Ask button on that note. The question is scoped to that "
                        "one document instead of the whole workspace, and the "
                        "answer cites the parts it used.",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "Save the answer as a new note and you have a summary "
                        "layer over your raw material — one that is itself "
                        "searchable next time.",
                    ],
                ),
            ),
        },
        {
            "title": "Studying: put a deadline and its reading in one place",
            "status": "Open",
            "priority": "Low",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 12,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Make a project for one course, a milestone for its next "
                        "exam or hand-in, and a notes folder for the lectures. "
                        'Then ask Genos about your own notes — "explain what I '
                        'wrote about elasticity".',
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "Every course's due dates end up on one calendar. The "
                        "to-do pane beside chat is a per-day list, which suits a "
                        "study session better than real coursework tasks do.",
                    ],
                ),
            ),
        },
        {
            "title": "Operations: make a procedure repeatable",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 13,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Write one recurring procedure — onboarding, invoicing, a "
                        "monthly close — as a note with checkboxes, and make one "
                        "task per run of it. A project owner can also set a "
                        "reusable task template and mark fields required.",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "Each run starts from the same steps instead of somebody's "
                        "memory, and every task keeps a trail of who changed what "
                        "and when — usually the thing an audit is actually asking "
                        "for.",
                    ],
                ),
            ),
        },
        {
            "title": "Anyone: turn a conversation into tracked work",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 13,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Next time a chat message contains something you have to "
                        "do, flag it for yourself, or link the thread to a task. "
                        '"We should do this" becomes something with an owner and '
                        "a date, without retyping it.",
                    ],
                ),
            ),
        },
    ],
}

JAPANESE_STARTER_BLUEPRINT = {
    "name": "Genosをはじめよう",
    "sprint_name": "スプリント1",
    "tags": [
        ("セットアップ", "#7c3aed", "#ffffff"),
        ("チーム", "#0ea5e9", "#ffffff"),
        ("試してみる", "#f59e0b", "#ffffff"),
        ("活用例", "#10b981", "#ffffff"),
    ],
    "milestone": {
        **STARTER_BLUEPRINT["milestone"],
        "title": "Genosを使いはじめる最初の1週間",
        "body": _body(
            (
                "🎯 目標",
                [
                    "このマイルストーンを終えるころには、チームの準備と最初の実案件の"
                    "作成が完了し、全員がどこで仕事を進めるかを把握できています。",
                ],
            ),
            (
                "🪜 使い方",
                [
                    "以下のタスクは、それぞれが小さな一歩です。タスクを開いて詳細を"
                    "確認し、終わったら完了にしてください。すべて終わったら、この"
                    "プロジェクトは削除してかまいません。自由に試してみてください。",
                ],
            ),
        ),
    },
    "tasks": [
        {
            **STARTER_BLUEPRINT["tasks"][0],
            "title": "チームメンバーを招待する",
            "body": _body(
                (
                    "🧾 やること",
                    [
                        "チームメニュー（左上の自分のアバター）を開き、「招待」を選びます。"
                        "Genosではチャット、タスク、ノートを1か所で扱えるため、2人目の"
                        "メンバーが参加した瞬間から便利さを実感できます。",
                    ],
                ),
            ),
        },
        {
            **STARTER_BLUEPRINT["tasks"][1],
            "title": "最初の実案件プロジェクトを作る",
            "body": _body(
                (
                    "🧾 やること",
                    [
                        "「タスク」で、実際に取り組んでいる仕事のプロジェクトを作成します。"
                        "プロジェクトごとに専用のチャットチャンネルが自動作成されるので、"
                        "会話と作業をひとつにまとめられます。",
                    ],
                ),
            ),
        },
        {
            **STARTER_BLUEPRINT["tasks"][2],
            "title": "Spotlightにワークスペースについて質問する",
            "body": _body(
                (
                    "🧾 やること",
                    [
                        "Cmd-K（WindowsではCtrl-K）を押し、「今週が期限のものは？」などと"
                        "質問してみます。Spotlightはチャット、タスク、ノートを参照します。"
                        "情報を追加するほど、より的確な回答が得られます。",
                    ],
                ),
            ),
        },
        {
            **STARTER_BLUEPRINT["tasks"][3],
            "title": "通知を有効にする",
            "body": _body(
                (
                    "🧾 やること",
                    [
                        "「設定」→「通知」を開きます。ブラウザ通知を許可するとリアルタイムで"
                        "知らせを受け取れます。不在時の出来事は、イベントごとではなく"
                        "ひとつにまとめたメールで確認できます。",
                    ],
                ),
            ),
        },
        {
            **STARTER_BLUEPRINT["tasks"][4],
            "title": "GoogleカレンダーまたはGitHubを連携する",
            "body": _body(
                (
                    "🧾 やること",
                    [
                        "「設定」→「連携」を開きます。カレンダーの予定はタスクと並べて"
                        "表示され、GitHubのプルリクエストは完了させるタスクに紐づけられます。",
                    ],
                ),
            ),
        },
        {
            **STARTER_BLUEPRINT["tasks"][5],
            "title": "ひと通り試したら、このプロジェクトを削除する",
            "body": _body(
                (
                    "🧾 やること",
                    [
                        "このスタータープロジェクトは、使いはじめるための足場です。実際の"
                        "仕事をGenosで進められるようになったら、プロジェクトメニューから"
                        "削除してください。ここで作成されたものもすべて一緒に削除されます。",
                    ],
                ),
            ),
        },
    ],
}

JAPANESE_EXAMPLES_BLUEPRINT = {
    "milestone": {
        **EXAMPLES_BLUEPRINT["milestone"],
        "title": "実際の仕事でGenosを試す",
        "body": _body(
            (
                "🎯 目標",
                [
                    "Genosを眺めるだけの段階から一歩進み、いずれにしても必要だった実際の"
                    "仕事に使ってみましょう。",
                ],
            ),
            (
                "🪜 使い方",
                [
                    "これはチェックリストではなく活用例です。自分の仕事に合うものを"
                    "ひとつ選んで試し、残りは削除してください。どれも数分で終わり、"
                    "単にリストが片づくだけでなく、実際に使える成果が残ります。",
                ],
            ),
        ),
    },
    "tasks": [
        {
            **EXAMPLES_BLUEPRINT["tasks"][0],
            "title": "エンジニアリング：プルリクエストをタスクに紐づける",
            "body": _body(
                (
                    "🧾 試してみる",
                    [
                        "「設定」→「連携」でGitHubを接続し、プルリクエストのリンクを"
                        "タスクに貼り付けます。「設定」→「タスク」では、そのプルリクエストが"
                        "マージされたときにタスクを自動で完了させる設定もできます。",
                    ],
                ),
                (
                    "💡 便利になること",
                    [
                        "ボードを別途手作業で更新する必要がなくなります。依存関係も試して"
                        "みましょう。あるタスクを別のタスクでブロックすると、Genosが"
                        "「ブロック中」ステータスの設定と解除を自動で行います。",
                    ],
                ),
            ),
        },
        {
            **EXAMPLES_BLUEPRINT["tasks"][1],
            "title": "計画：大切な日付をマイルストーンにする",
            "body": _body(
                (
                    "🧾 試してみる",
                    [
                        "次の実際の締め切りをマイルストーンにし、2〜3件のタスクを紐づけます。"
                        "進捗が自動で集計されるので、「期限に間に合うか？」という問いに"
                        "答えるための場所ができます。",
                    ],
                ),
            ),
        },
        {
            **EXAMPLES_BLUEPRINT["tasks"][2],
            "title": "…そのうちひとつをサブタスクに分ける",
            "body": _body(
                (
                    "🧾 試してみる",
                    [
                        "タスクを開き、作業の各手順をサブタスクとして追加します。このタスク"
                        "自体が、ひとつ上のタスクのサブタスクです。表に表示されている字下げが、"
                        "これから作成する構造です。",
                    ],
                ),
            ),
        },
        {
            **EXAMPLES_BLUEPRINT["tasks"][3],
            "title": "マーケティング：共有ノートで原稿を書く",
            "body": _body(
                (
                    "🧾 試してみる",
                    [
                        "次の投稿、メール、または企画書をノートに書き、同僚を編集者として"
                        "共有します。同時に入力しながら、お互いのカーソルを確認できます。",
                    ],
                ),
                (
                    "💡 便利になること",
                    [
                        "原稿を添付ファイルでやり取りせずに済みます。指示が下書きの上に"
                        "まとまるよう、企画書をタスクのノートとして残しましょう。承認済みの"
                        "版をプロジェクトのチャンネルにピン留めすれば、古い文面を誤って"
                        "公開することもありません。",
                    ],
                ),
            ),
        },
        {
            **EXAMPLES_BLUEPRINT["tasks"][4],
            "title": "リサーチ：長い文書の内容をその文書に質問する",
            "body": _body(
                (
                    "🧾 試してみる",
                    [
                        "長い文書をノートに貼り付けるかインポートし、そのノートの「質問」"
                        "ボタンを使います。ワークスペース全体ではなく、その文書だけを対象に"
                        "質問でき、回答には参照した箇所が示されます。",
                    ],
                ),
                (
                    "💡 便利になること",
                    [
                        "回答を新しいノートとして保存すれば、元資料の上に要約レイヤーを"
                        "作れます。その要約も、次回から検索の対象になります。",
                    ],
                ),
            ),
        },
        {
            **EXAMPLES_BLUEPRINT["tasks"][5],
            "title": "学習：締め切りと教材を1か所にまとめる",
            "body": _body(
                (
                    "🧾 試してみる",
                    [
                        "科目ごとにプロジェクトを作り、次の試験や提出日をマイルストーンに、"
                        "講義資料をノートフォルダーにまとめます。そのうえで「弾力性について"
                        "自分が書いた内容を説明して」など、自分のノートについてGenosに"
                        "質問してみましょう。",
                    ],
                ),
                (
                    "💡 便利になること",
                    [
                        "すべての科目の期限をひとつのカレンダーで確認できます。チャット横の"
                        "ToDo欄は日ごとのリストなので、本格的な課題タスクよりも日々の"
                        "学習セッションに向いています。",
                    ],
                ),
            ),
        },
        {
            **EXAMPLES_BLUEPRINT["tasks"][6],
            "title": "オペレーション：手順を繰り返し使える形にする",
            "body": _body(
                (
                    "🧾 試してみる",
                    [
                        "オンボーディング、請求、月次決算など、繰り返し行う手順をチェック"
                        "ボックス付きのノートにし、実施するたびにタスクをひとつ作ります。"
                        "プロジェクトオーナーは、再利用できるタスクテンプレートを設定し、"
                        "入力必須の項目を指定することもできます。",
                    ],
                ),
                (
                    "💡 便利になること",
                    [
                        "誰かの記憶に頼らず、毎回同じ手順から始められます。また、各タスクには"
                        "誰がいつ何を変更したかが残ります。多くの場合、監査で本当に求められる"
                        "のはこの記録です。",
                    ],
                ),
            ),
        },
        {
            **EXAMPLES_BLUEPRINT["tasks"][7],
            "title": "すべての人：会話を追跡できる仕事に変える",
            "body": _body(
                (
                    "🧾 試してみる",
                    [
                        "次にチャットのメッセージでやるべきことが出てきたら、自分用に"
                        "フラグを付けるか、そのスレッドをタスクに紐づけます。「これを"
                        "やろう」という会話が、入力し直すことなく担当者と期限のある仕事に"
                        "変わります。",
                    ],
                ),
            ),
        },
    ],
}

TODO_BLUEPRINTS = (
    {
        "category": "Getting started",
        "items": [
            (
                "Look around the Getting started project",
                "The tasks there double as a product tour — each one is a real action, not reading.",
            ),
            ("Press Cmd-K and ask Spotlight anything", None),
            (
                "Do the one example that matches your job",
                "The 'Try Genos on real work' milestone has one for engineering, planning, "
                "marketing, research, studying and operations. Yours is in there; the rest "
                "are meant to be deleted.",
            ),
        ],
    },
    {
        "category": "はじめに",
        "items": [
            (
                "「Genosをはじめよう」プロジェクトを見て回る",
                "各タスクは製品ツアーにもなっています。読むだけでなく、どれも実際に操作します。",
            ),
            ("Cmd-Kを押してSpotlightに何か質問する", None),
            (
                "自分の仕事に合う活用例をひとつ試す",
                "「実際の仕事でGenosを試す」マイルストーンには、エンジニアリング、"
                "計画、マーケティング、リサーチ、学習、オペレーションの活用例があります。"
                "自分に合うものを試し、残りは削除してかまいません。",
            ),
        ],
    },
)


def _create_starter_project(
    user,
    team,
    starter_blueprint,
    examples_blueprint,
    taken_codes,
) -> None:
    """Create one localized starter project using the shared seeder schema."""
    from origin.services.project_code import derive_project_code

    project_name = starter_blueprint["name"]
    project = ProjectMaster.objects.create(
        team=team,
        project_name=project_name,
        code=derive_project_code(project_name, taken_codes),
        owner=user,
        project_system_user=user,
        is_private=False,
    )
    taken_codes.add(project.code)

    # Plain .save() so the PM-channel membership signal fires; bulk_create
    # would skip it.
    ProjectMembers.objects.create(team=team, project=project, attendee=user)
    ProjectTags.objects.bulk_create(
        [
            ProjectTags(
                team=team,
                project=project,
                tag_id=tag_idx + 1,
                tag_name=name,
                tag_color=color,
                tag_text_color=text_color,
            )
            for tag_idx, (name, color, text_color) in enumerate(starter_blueprint["tags"])
        ]
    )

    sprint = _create_sprint(team, project)
    if sprint_name := starter_blueprint.get("sprint_name"):
        sprint.name = sprint_name
        sprint.save(update_fields=["name"])
    milestone, backing_task = _create_milestone_with_backing_task(
        team, project, sprint, user, [], starter_blueprint["milestone"]
    )
    tasks = _create_blueprint_tasks(
        team,
        project,
        sprint,
        milestone,
        backing_task,
        user,
        [],
        starter_blueprint["tasks"],
    )

    examples_milestone, examples_backing_task = _create_milestone_with_backing_task(
        team, project, sprint, user, [], examples_blueprint["milestone"]
    )
    _create_blueprint_tasks(
        team,
        project,
        sprint,
        examples_milestone,
        examples_backing_task,
        user,
        [],
        examples_blueprint["tasks"],
    )

    # Inviting teammates blocks the first-project walkthrough in each
    # language, giving the dependency UI one real relationship to show.
    TaskDependency.objects.create(
        blocker_task=tasks[0],
        blocked_task=tasks[1],
        team=team,
        created_by=user,
    )


def create_starter_workspace(user, team) -> None:
    """Seed English and Japanese starter content for an existing real team.

    One atomic block; raises on failure (the caller decides that a
    failed seed must not fail team creation). Never touches `is_demo`.
    """
    with transaction.atomic():
        # Membership first — the view creates only the TeamMaster row and
        # the client normally joins via a separate call, which is
        # idempotent against this row (TeamMembersView short-circuits on
        # an existing membership).
        TeamMembers.objects.get_or_create(team=team, attendee=user)

        taken_codes = set(
            ProjectMaster.objects.filter(team=team, code__isnull=False).values_list(
                "code", flat=True
            )
        )
        for starter_blueprint, examples_blueprint in (
            (STARTER_BLUEPRINT, EXAMPLES_BLUEPRINT),
            (JAPANESE_STARTER_BLUEPRINT, JAPANESE_EXAMPLES_BLUEPRINT),
        ):
            _create_starter_project(
                user,
                team,
                starter_blueprint,
                examples_blueprint,
                taken_codes,
            )

        # Seed the guide once, after both localized projects are complete.
        seed_guide_notes(user, team)

        today = user_today(user)
        group = ToDoGroup.objects.create(
            team=team,
            user=user,
            local_date=today,
            is_completed=False,
        )
        for category_order, todo_blueprint in enumerate(TODO_BLUEPRINTS):
            category = ToDoCategory.objects.create(
                team=team,
                user=user,
                name=todo_blueprint["category"],
                sort_order=category_order,
            )
            for item_order, (title, notes) in enumerate(todo_blueprint["items"]):
                item_fields = {
                    "group": group,
                    "category": category,
                    "title": title,
                    "sort_order": item_order,
                }
                if notes is not None:
                    item_fields["notes"] = _text_body(notes)
                ToDoItem.objects.create(**item_fields)

    # After commit, exactly like the demo path: content only becomes
    # searchable once OpenSearch has it, and the reindex must not run
    # inside the transaction.
    kick_off_demo_reindex()
