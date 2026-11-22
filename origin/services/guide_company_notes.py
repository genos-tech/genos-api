"""Two guide notes for running Genos in a company (MIP-1689, under MIP-1447).

"Organising a large workspace" teaches the model Ken decided on 2026-11-18: one Genos team per
company, and a department is a project tag + a mention group + a team folder + an open group
chat, with the private version of each for what must stay inside a smaller group. "Setting up
Genos for your company" is the owner's checklist for building it.

Only what the app does TODAY is written here, using the names the app shows (a project label is
a "tag", mention groups live in Settings, "Team Notes", "Join Project"). Things that are planned
but not built (hiding a private project's name, a chat directory) are not
promised; the "limits" section says what is missing now. When one of them ships, change that
section.

Seeded with the rest of the guide (`guide_notes.GUIDE_NOTES` / `JAPANESE_GUIDE_NOTES` extend
these lists); `add_company_guide_notes` gives existing members the same two notes.
"""

from __future__ import annotations

from origin.services.demo_blocks import _body

_B = "bullet"

ORGANISING_TITLE = {
    "en": "Organising a large workspace: one team, many departments",
    "ja": "大きな組織でGenosを整える：ひとつのチーム、たくさんの部署",
}
SETUP_TITLE = {
    "en": "Setting up Genos for your company: an owner's checklist",
    "ja": "会社でGenosを使い始める：オーナー向けチェックリスト",
}

_ORGANISING_EN = _body(
    (
        "The idea in one minute",
        [
            "Keep the whole company in one Genos team, and build each department out of four "
            "things that share a name. Search, the AI and @mentions work inside one team, so one "
            "team lets everyone find what they are allowed to see. (Teams are for separate "
            "companies; to work with another company, see “Working with another company: "
            "cross-team sharing”.)",
        ],
    ),
    (
        "A department is four things with one name",
        [
            "Take a department called Payments:",
            (
                _B,
                "A project tag “Payments”. Put it on each of the department's projects (open a "
                "project, then Edit tags). The Tasks sidebar sorts projects by tag and can "
                "filter by it, so a person sees their department's projects together.",
            ),
            (
                _B,
                "A mention group “payments”. Settings → Mention groups → New group, then add "
                "the people. Writing @payments in a chat message, a task comment or a task "
                "body notifies all of them.",
            ),
            (
                _B,
                "A team folder “Payments”. In Notes, under Team Notes, make a folder and choose "
                "who can open it: everyone, or only people you add (you can add a whole "
                "mention group at once). This is where what the department knows lives: "
                "runbooks, policies, meeting notes. Folders nest, so a sub-team gets a "
                "subfolder.",
            ),
            (
                _B,
                "An open group chat “payments · general”. New Group Message, leave it Public. "
                "Anyone in the team can find it by name and join.",
            ),
            "A sub-team repeats the same four things with a longer name: @payments-billing, a "
            "“Billing” subfolder, “payments · billing”, and the tag on its projects.",
        ],
    ),
    (
        "Keeping something inside a smaller group",
        [
            "Use the private version. What each one does today:",
            (
                _B,
                "A private group chat: only people you add, or whose request to join you "
                "approve, are in it.",
            ),
            (
                _B,
                "A private project: only its members open it; anyone else can ask to join.",
            ),
            (
                _B,
                "A private team folder: it opens only for the people you add, and it does not "
                "appear in the folder list of anyone else.",
            ),
            "One thing to know: the NAME of a private project still shows for everyone in the team "
            "(in Join Project, and in Spotlight when someone types #), and the name of a "
            "private group chat shows in the chat search box, marked with a lock. So keep the "
            "secret inside, and give the project or chat a plain name.",
        ],
    ),
    (
        "Where does it go?",
        [
            (_B, "Talking about one piece of work: that project's chat."),
            (_B, "A decision or spec for one task: the task's note, so it stays on the work."),
            (
                _B,
                "What the whole department should know: a note in its team folder.",
            ),
            (_B, "A meeting recap: a note in the department's folder or on the chat."),
            (
                _B,
                "A topic with no project: an open group chat named “department · topic”. "
                "Leave it when it is over.",
            ),
            (_B, "Your own drafts and your day: My Notes and To-Do."),
            "Name chats “department · topic”. Groups can only be found by name for now, so the "
            "same first word makes a search for the department list all of them.",
        ],
    ),
    (
        "Join little, find everything",
        [
            (
                _B,
                "Join your own sub-team's projects and chats, plus two or three others. You do "
                "not need to join to find something.",
            ),
            (
                _B,
                "Press Cmd-K (Ctrl-K on Windows) for Spotlight. Type # to pick a project, "
                "task, note or group chat, and @ for people and mention groups. Open projects you "
                "have not joined show up there too.",
            ),
            (
                _B,
                "In the Tasks sidebar, Join Project lists every project in the team you have "
                "not joined. In the chat search box, typing a group's name finds it, and you can "
                "join it if it is open.",
            ),
        ],
    ),
    (
        "Keeping your own sidebar short",
        [
            (_B, "Pin the few chats you open all day (Pin Chat); the rest sort by latest message."),
            (_B, "Mute a noisy chat with the bell in its header."),
            (
                _B,
                "Tag group chats for yourself (the ⋮ menu on a chat, Tags). The tags are private "
                "to you and filter your list.",
            ),
            (
                _B,
                "Star the milestones, notes and folders you return to (Favorites), and save "
                "the task filters you keep rebuilding.",
            ),
            (
                _B,
                "Hide the services you do not use: Settings → General → Sidebar.",
            ),
        ],
    ),
    (
        "What is not there yet",
        [
            "So that nothing surprises you: today there is no list of all group chats to browse "
            "(search by name), and the task dashboard shows one project at a time. The habits "
            "above work around each of these. A project or a group chat you no longer use can be "
            "archived instead of deleted.",
        ],
    ),
    (
        "A ready-made example",
        [
            "If you kept “Include sample instruction data” ticked when you created the team, you "
            "already have one: "
            "the tag “Sample dept”, @sample-dept, the “Sample dept” folders, two projects and "
            "two group chats. Open the note “How the sample dept keeps its work tidy” in Team "
            "Notes, copy what is useful, and delete the rest. To remove all of the sample data at "
            "once, the owner opens Team Profile and chooses Remove sample data.",
        ],
    ),
)

_SETUP_EN = _body(
    (
        "Before you start",
        [
            "This is a short list and some clicking, not a project. Read “Organising a large "
            "workspace” first; this note is the checklist for building it. Inviting people "
            "(step 6) needs the team owner or editor role.",
        ],
    ),
    (
        "1. Write down the departments",
        [
            "A short list, the same spelling everywhere (payments, billing, growth). A name "
            "becomes the tag, the @group, the folder and the chat, so keep it short and "
            "lowercase. You can rename any of them later. If you are creating the team now, type "
            "the list into the “Departments” box on the create-team page: Genos then makes the "
            "project tag, the mention group, the team folder and the open chat of each one, "
            "and you can skip steps 2 to 4 for them.",
        ],
    ),
    (
        "2. Make the mention groups",
        [
            "Settings → Mention groups → New group, one per department (and per sub-team). "
            "Add the people. This is also how you will reach a department in one message.",
        ],
    ),
    (
        "3. Make the team folders",
        [
            "Notes → Team Notes: one folder per department, subfolders for sub-teams. Choose "
            "who can open each one; a private folder for HR, finance or security. Add the "
            "mention group instead of adding people one by one.",
        ],
    ),
    (
        "4. Make the group chats",
        [
            "Every team starts with an open chat for the whole company called “general”. Add one "
            "open chat per department (“payments · general”), unless the “Departments” box "
            "already did. Add private chats only where a small group needs them. Nobody is "
            "added to a chat for you: people find the open ones by name in the chat search.",
        ],
    ),
    (
        "5. Make the projects and tag them",
        [
            "Create the projects (Tasks sidebar → New Project) and put the department's tag on "
            "each (open the project, then Edit tags). Keep a project private only when its "
            "contents must stay inside; remember its name is still visible.",
        ],
    ),
    (
        "6. Invite people",
        [
            "Open the team menu (your avatar, top left) and choose Invite. Then add each "
            "person to their department's mention group, folder, chat and projects. Guests "
            "are invited to a single project.",
        ],
    ),
    (
        "7. Teach three habits",
        [
            (_B, "Where does it go? (the list in “Organising a large workspace”)"),
            (_B, "Join little, find everything: Cmd-K, then # and @."),
            (_B, "Name things “department · topic” so a search finds them together."),
        ],
    ),
    (
        "8. Roles and AI apps",
        [
            "Owner, editor and viewer roles are in “Teams, members, and roles”. To let an AI "
            "app such as Claude work in Genos, see “Connecting Genos to other tools: API, MCP, and realtime”.",
        ],
    ),
)

_ORGANISING_JA = _body(
    (
        "1分でわかる考え方",
        [
            "会社全体をひとつのGenosチームにまとめ、各部署を同じ名前の4つのもので作ります。"
            "検索、AI、@メンションはひとつのチームの中で働くので、ひとつのチームにしておくと、"
            "誰もが見る権限のあるものを探し出せます。（チームは別の会社のためのものです。"
            "他の会社と作業するときは「他の会社との連携：チーム間共有」を見てください。）",
        ],
    ),
    (
        "部署＝同じ名前の4つ",
        [
            "「Payments」という部署を例にします。",
            (
                _B,
                "プロジェクトのタグ「Payments」。部署のプロジェクトそれぞれに付けます"
                "（プロジェクトを開いて「タグを編集する」）。タスクのサイドバーはタグ順に"
                "並び、タグで絞り込めるので、部署のプロジェクトがまとまって見えます。",
            ),
            (
                _B,
                "メンショングループ「payments」。設定→「メンショングループ」→新しいグループで"
                "作り、メンバーを追加します。チャットのメッセージ、タスクのコメントや本文で "
                "@payments と書くと全員に通知されます。",
            ),
            (
                _B,
                "チームのフォルダ「Payments」。ノートの「チームノート」でフォルダを作り、"
                "開ける人を選びます（全員、または追加した人だけ。メンショングループをまるごと"
                "追加することもできます）。手順書、ルール、議事録など、部署が知っておくことを"
                "置く場所です。フォルダは入れ子にできるので、サブチームにはサブフォルダを作ります。",
            ),
            (
                _B,
                "オープンなグループチャット「payments · general」。新しいグループメッセージで、"
                "公開のままにします。チームの誰でも名前で見つけて参加できます。",
            ),
            "サブチームも同じ4つを、少し長い名前で繰り返します。@payments-billing、"
            "「Billing」サブフォルダ、「payments · billing」、そのプロジェクトへのタグです。",
        ],
    ),
    (
        "もっと小さなグループの中だけにしたいとき",
        [
            "非公開版を使います。今のところ、それぞれ次のようになります。",
            (
                _B,
                "非公開のグループチャット：あなたが追加した人、または参加リクエストを承認した"
                "人だけが入れます。",
            ),
            (
                _B,
                "非公開のプロジェクト：メンバーだけが開けます。他の人は参加をリクエストできます。",
            ),
            (
                _B,
                "非公開のチームフォルダ：追加した人にだけ開かれ、他の人のフォルダ一覧には"
                "表示されません。",
            ),
            "知っておいてほしいこと：非公開のプロジェクトの名前は、チームの全員に見えます"
            "（「プロジェクトに参加」と、Spotlightで # を入力したとき）。非公開のグループ"
            "チャットの名前は、チャットの検索ボックスに錠前マーク付きで表示されます。秘密は"
            "中に置き、プロジェクトやチャットの名前はありふれたものにしましょう。",
        ],
    ),
    (
        "どこに書く？",
        [
            (_B, "ひとつの仕事の話：そのプロジェクトのチャット。"),
            (_B, "ひとつのタスクの決定や仕様：そのタスクのノート（仕事のそばに残ります）。"),
            (_B, "部署の全員が知っておくこと：部署のチームフォルダのノート。"),
            (_B, "会議のまとめ：部署のフォルダ、またはチャットのノート。"),
            (
                _B,
                "プロジェクトのない話題：「部署 · 話題」という名前のオープンなグループ"
                "チャット。終わったら退出します。",
            ),
            (_B, "自分の下書きと一日の予定：マイノートとTo-Do。"),
            "チャットは「部署 · 話題」の形で名付けましょう。グループは今のところ名前でしか"
            "探せないので、最初の単語を揃えておくと、部署名の検索でまとめて表示されます。",
        ],
    ),
    (
        "少しだけ参加して、すべてを見つける",
        [
            (
                _B,
                "参加するのは、自分のサブチームのプロジェクトとチャットに、あと2〜3個。"
                "見つけるために参加する必要はありません。",
            ),
            (
                _B,
                "Cmd-K（WindowsはCtrl-K）でSpotlightを開きます。# でプロジェクト、タスク、"
                "ノート、グループチャットを、@ で人とメンショングループを選べます。参加して"
                "いない公開プロジェクトも表示されます。",
            ),
            (
                _B,
                "タスクのサイドバーの「プロジェクトに参加」には、まだ参加していないチーム内の"
                "プロジェクトがすべて並びます。チャットの検索ボックスにグループ名を入力する"
                "と見つかり、公開のグループなら参加できます。",
            ),
        ],
    ),
    (
        "自分のサイドバーを短く保つ",
        [
            (
                _B,
                "一日中開くチャットだけをピン留め（「チャットをピン留め」）。残りは最新のメッセージ順です。",
            ),
            (_B, "うるさいチャットは、ヘッダーのベルでミュートします。"),
            (
                _B,
                "グループチャットに自分用のタグを付けます（チャットの⋮メニューの「タグ」）。"
                "タグはあなただけのもので、一覧の絞り込みに使えます。",
            ),
            (
                _B,
                "よく戻るマイルストーン、ノート、フォルダにスターを付け（お気に入り）、何度も"
                "作り直すタスクのフィルターは保存します。",
            ),
            (_B, "使わないサービスは非表示にします：設定→一般→サイドバー。"),
        ],
    ),
    (
        "まだできないこと",
        [
            "驚かないように書いておきます。今のところ、"
            "すべてのグループチャットを一覧で見る画面はなく（名前で検索します）、"
            "タスクのダッシュボードは一度にひとつのプロジェクトしか表示しません。"
            "上の習慣は、これらを補うものです。使わなくなったプロジェクトやグループチャットは、"
            "削除せずにアーカイブできます。",
        ],
    ),
    (
        "用意されたサンプル",
        [
            "チーム作成時に「サンプルデータ（使い方の例）を含める」にチェックを入れたままにしていれば、"
            "すでに用意されています。"
            "タグ「サンプル部署」、@sample-dept、「サンプル部署」フォルダ、プロジェクト2つ、"
            "グループチャット2つです。チームノートの「サンプル部署の仕事の整理のしかた」を"
            "開き、役立つものは真似して、残りは削除してください。"
            "まとめて消すには、オーナーがチームプロフィールを開いて「サンプルデータを削除」を選びます。",
        ],
    ),
)

_SETUP_JA = _body(
    (
        "始める前に",
        [
            "必要なのは短いリストといくつかの操作で、プロジェクトではありません。先に"
            "「大きな組織でGenosを整える」を読んでください。このノートは、それを作る"
            "ためのチェックリストです。人の招待（手順6）にはチームのオーナーまたは"
            "エディターのロールが必要です。",
        ],
    ),
    (
        "1. 部署を書き出す",
        [
            "短いリストにし、表記はどこでも同じにします（payments、billing、growth）。名前は"
            "タグ、@グループ、フォルダ、チャットになるので、短く、小文字にします。あとから"
            "どれも変更できます。いまチームを作るところなら、このリストをチーム作成ページの"
            "「部署」欄に入力してください。それぞれの部署について、プロジェクトのタグ、メンション"
            "グループ、チームフォルダ、オープンなチャットを自動で作ります（手順2〜4は"
            "省略できます）。",
        ],
    ),
    (
        "2. メンショングループを作る",
        [
            "設定→「メンショングループ」→新しいグループ。部署ごと（サブチームごと）に作り、"
            "メンバーを追加します。部署に1通で届くようになります。",
        ],
    ),
    (
        "3. チームのフォルダを作る",
        [
            "ノート→チームノート：部署ごとにフォルダ、サブチームにはサブフォルダ。開ける人を"
            "それぞれ選びます。人事、経理、セキュリティなどは非公開のフォルダに。人をひとり"
            "ずつではなく、メンショングループを追加します。",
        ],
    ),
    (
        "4. グループチャットを作る",
        [
            "どのチームにも、会社全体用のオープンなチャット「general」が最初からあります。"
            "部署ごとにオープンなチャットをひとつ（「payments · general」）作ります（「部署」欄で"
            "作った場合は不要です）。非公開のチャットは、小さなグループが必要とするところだけに"
            "作ります。誰かが自動で追加されることはなく、公開のチャットは名前でチャット検索して"
            "見つけます。",
        ],
    ),
    (
        "5. プロジェクトを作ってタグを付ける",
        [
            "プロジェクトを作り（タスクのサイドバー→新規プロジェクト）、部署のタグを付けます"
            "（プロジェクトを開いて「タグを編集する」）。中身を内側に留めたいときだけ非公開に"
            "します。名前は見えることを忘れないでください。",
        ],
    ),
    (
        "6. 人を招待する",
        [
            "チームのメニュー（左上のアバター）を開き、「招待」を選びます。そのあと、各自を"
            "部署のメンショングループ、フォルダ、チャット、プロジェクトに追加します。ゲストは"
            "プロジェクト単位で招待します。",
        ],
    ),
    (
        "7. 3つの習慣を伝える",
        [
            (_B, "どこに書く？（「大きな組織でGenosを整える」の一覧）"),
            (_B, "少しだけ参加して、すべてを見つける：Cmd-K、そして # と @。"),
            (_B, "名前は「部署 · 話題」にして、検索でまとめて見つかるようにする。"),
        ],
    ),
    (
        "8. ロールとAIアプリ",
        [
            "オーナー、エディター、ビューアーのロールは「チーム、メンバー、役割」にあります。"
            "ClaudeのようなAIアプリにGenosで作業させるには、「Genosと他のツールを接続する：API、MCP、リアルタイム」を"
            "見てください。",
        ],
    ),
)

COMPANY_NOTES: dict[str, list[tuple[str, list]]] = {
    "en": [(ORGANISING_TITLE["en"], _ORGANISING_EN), (SETUP_TITLE["en"], _SETUP_EN)],
    "ja": [(ORGANISING_TITLE["ja"], _ORGANISING_JA), (SETUP_TITLE["ja"], _SETUP_JA)],
}
