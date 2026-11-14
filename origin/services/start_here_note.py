"""The "Start here" note: one private note for every NEW account (MIP-1555).

The owner's decision (2026-11-14): a person who signs up gets ONE personal note,
created once, that says what Genos is for and how to connect an AI app (Claude,
ChatGPT) to it. Existing accounts get nothing, and there is no backfill. This is
the single place the first-note idea from MIP-666 / MIP-1447 lives; do not add a
second onboarding mechanism next to it. (The "Genos Guide" folder, which a member
gets when they join a team, is a different thing and unchanged.)

How it fits together
--------------------
* `create_for(user)` is called after the account exists, from the email signup
  view (both the plain and the invite-token branch) and from the first-time
  OAuth sign-in. Nothing else calls it, so an existing account never gets one.
* A personal note belongs to a TEAM (the My Notes list filters by team, and a
  note nobody can list is a note nobody reads), but a new account has no team
  yet: the person makes one, or accepts an invitation, after their first
  sign-in. So the note is written now with no team, and `adopt_for_team` gives
  it the first team the person joins. That hook is `guide_notes.seed_guide_notes`,
  which both `create_starter_workspace` and `TeamMembersView` already call.
  No new column and no migration: an owned note with a title from `TITLES` is
  the marker.
* Idempotent: the user's row is locked, then an existing marker note (in any
  team, or none) means "already done". A retry, a second call or a concurrent
  signup creates nothing. A person who deletes the note is not given another:
  only the signup calls `create_for`.
* Never raises: any failure is logged (the exception type only) and swallowed,
  inside its own savepoint, so it can neither fail the signup nor poison the
  caller's transaction (the OAuth callback runs inside `transaction.atomic`).
* Not for demo visitors, project system users or reviewer accounts. (Reviewer
  accounts and system users are made without going through signup; the guards
  here are belt and braces.)

The "Connect your AI app" section is ALWAYS written (owner decision 2026-11-14): MCP OAuth is part
of the product, so the note never depends on the switch. Only the address can be unknown (no
`API_PUBLIC_BASE_URL`): then the section keeps its steps but shows no address and no command.
"""

from __future__ import annotations

import logging

from django.db import transaction

from apis.request_id import describe_exception
from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.services import mcp_oauth
from origin.views.utils.note_version import snapshot_note_version

logger = logging.getLogger(__name__)

NOTE_TYPE_PERSONAL = 1
ROLE_OWNER = 1

DEFAULT_LOCALE = "en"

# One entry per frontend locale. Menu names are the ones the app shows in that
# language (Settings > Developer > Connected apps), taken from the frontend
# dictionaries. Anthropic's own screens are described generically on purpose:
# their labels are Anthropic's to change.
_COPY: dict[str, dict] = {
    "en": {
        "title": "Start here",
        "intro": (
            "Genos keeps your team's chat, tasks and notes in one workspace, so the "
            "work and the conversation about it stay connected."
        ),
        "bullets": [
            "Chat: talk with your team, and turn a thread into a task.",
            "Tasks: projects, milestones and boards. Each task has its own notes and discussion.",
            "Notes: private notes for yourself, and folders you share with your team.",
            "Spotlight: the AI assistant. Press Cmd-K (Ctrl-K on Windows) and ask about "
            "anything you can see.",
        ],
        "h_connect": "Connect your AI app",
        "connect_intro": (
            "You can let an AI app such as Claude work in Genos as you. When you approve "
            "the connection you see what it asks for; deleting stays off unless you tick it."
        ),
        "url_label": "Your Genos address for AI apps:",
        "url_hint": "Type it exactly as shown: no slash at the end and nothing after it.",
        "url_missing": "(ask your Genos admin for the API address; it ends in {path})",
        "h_claude": "claude.ai and Claude Desktop",
        "claude_steps": [
            "In Claude, open the connectors settings and add a custom connector.",
            "Paste the address above.",
            "Sign in to Genos when asked, check what the app asks for, then approve.",
        ],
        "h_code": "Claude Code",
        "code_run": "Run this in a terminal:",
        "code_signin": "Then open /mcp inside Claude Code and sign in to genos.",
        "plan": "Connecting an AI app needs Genos Core or above.",
        "revoke": (
            "To end an app's access, open {menu} and revoke it. It stops working on its next request."
        ),
        "menu": "Settings > Developer > Connected apps",
    },
    "ja": {
        "title": "はじめに",
        "intro": (
            "Genos は、チームのチャット・タスク・ノートを 1 つのワークスペースにまとめ、"
            "作業とその会話をつなげたまま管理できるツールです。"
        ),
        "bullets": [
            "チャット：チームと会話し、スレッドをそのままタスクにできます。",
            "タスク：プロジェクト、マイルストーン、ボードで管理。各タスクに専用のノートとやり取りが付きます。",
            "ノート：自分だけのノートと、チームで共有するフォルダ。",
            "Spotlight：AI アシスタント。Cmd-K（Windows は Ctrl-K）で開き、見える範囲のことを質問できます。",
        ],
        "h_connect": "AI アプリをつなぐ",
        "connect_intro": (
            "Claude などの AI アプリに、あなたとして Genos の中で作業させることができます。"
            "接続を承認するときに要求内容が表示され、削除はチェックしない限りオフのままです。"
        ),
        "url_label": "AI アプリ用の Genos のアドレス：",
        "url_hint": "表示のとおりに入力してください。末尾にスラッシュや「?」以降は付けません。",
        "url_missing": "（API のアドレスは管理者に確認してください。末尾は {path} です）",
        "h_claude": "claude.ai と Claude Desktop",
        "claude_steps": [
            "Claude のコネクタ設定を開き、カスタムコネクタを追加します。",
            "上のアドレスを貼り付けます。",
            "求められたら Genos にサインインし、アプリの要求内容を確認して承認します。",
        ],
        "h_code": "Claude Code",
        "code_run": "ターミナルで次を実行します：",
        "code_signin": "続いて Claude Code で /mcp を開き、genos にサインインします。",
        "plan": "AI アプリの接続には Genos の Core 以上のプランが必要です。",
        "revoke": (
            "アプリのアクセスを終了するには、{menu} でそのアプリを取り消します。"
            "次のリクエストから使えなくなります。"
        ),
        "menu": "設定 > Developer > 接続済みアプリ",
    },
    "es": {
        "title": "Empieza aquí",
        "intro": (
            "Genos reúne el chat, las tareas y las notas de tu equipo en un solo espacio, "
            "para que el trabajo y la conversación sobre él sigan conectados."
        ),
        "bullets": [
            "Chat: habla con tu equipo y convierte un hilo en una tarea.",
            "Tareas: proyectos, hitos y tableros. Cada tarea tiene sus propias notas y conversación.",
            "Notas: notas privadas para ti y carpetas que compartes con tu equipo.",
            "Spotlight: el asistente de IA. Pulsa Cmd-K (Ctrl-K en Windows) y pregunta sobre "
            "todo lo que puedes ver.",
        ],
        "h_connect": "Conecta tu app de IA",
        "connect_intro": (
            "Puedes dejar que una app de IA como Claude trabaje en Genos como tú. Al aprobar "
            "la conexión ves lo que pide; borrar sigue desactivado salvo que lo marques."
        ),
        "url_label": "Tu dirección de Genos para apps de IA:",
        "url_hint": "Escríbela tal cual: sin barra al final y sin nada después.",
        "url_missing": "(pide a tu administrador de Genos la dirección de la API; termina en {path})",
        "h_claude": "claude.ai y Claude Desktop",
        "claude_steps": [
            "En Claude, abre los ajustes de conectores y añade un conector personalizado.",
            "Pega la dirección de arriba.",
            "Inicia sesión en Genos cuando te lo pida, revisa lo que solicita la app y aprueba.",
        ],
        "h_code": "Claude Code",
        "code_run": "Ejecuta esto en una terminal:",
        "code_signin": "Después abre /mcp dentro de Claude Code e inicia sesión en genos.",
        "plan": "Conectar una app de IA requiere Genos Core o superior.",
        "revoke": (
            "Para terminar el acceso de una app, abre {menu} y revócala. "
            "Deja de funcionar en su siguiente solicitud."
        ),
        "menu": "Ajustes > Revelador > Apps conectadas",
    },
    "fr": {
        "title": "Commencer ici",
        "intro": (
            "Genos réunit le chat, les tâches et les notes de votre équipe dans un seul espace, "
            "pour que le travail et la conversation qui l'entoure restent liés."
        ),
        "bullets": [
            "Chat : discutez avec votre équipe et transformez un fil en tâche.",
            "Tâches : projets, jalons et tableaux. Chaque tâche a ses propres notes et sa discussion.",
            "Notes : des notes privées pour vous et des dossiers partagés avec votre équipe.",
            "Spotlight : l'assistant IA. Appuyez sur Cmd-K (Ctrl-K sous Windows) et posez vos "
            "questions sur tout ce que vous pouvez voir.",
        ],
        "h_connect": "Connectez votre app d'IA",
        "connect_intro": (
            "Vous pouvez laisser une app d'IA comme Claude travailler dans Genos en votre nom. "
            "En approuvant la connexion, vous voyez ce qu'elle demande ; la suppression reste "
            "désactivée sauf si vous la cochez."
        ),
        "url_label": "Votre adresse Genos pour les apps d'IA :",
        "url_hint": "Saisissez-la exactement ainsi : sans barre oblique à la fin et rien après.",
        "url_missing": (
            "(demandez l'adresse de l'API à votre administrateur Genos ; elle se termine par {path})"
        ),
        "h_claude": "claude.ai et Claude Desktop",
        "claude_steps": [
            "Dans Claude, ouvrez les réglages des connecteurs et ajoutez un connecteur personnalisé.",
            "Collez l'adresse ci-dessus.",
            "Connectez-vous à Genos quand on vous le demande, vérifiez ce que l'app demande, puis "
            "approuvez.",
        ],
        "h_code": "Claude Code",
        "code_run": "Exécutez ceci dans un terminal :",
        "code_signin": "Ouvrez ensuite /mcp dans Claude Code et connectez-vous à genos.",
        "plan": "Connecter une app d'IA nécessite Genos Core ou supérieur.",
        "revoke": (
            "Pour mettre fin à l'accès d'une app, ouvrez {menu} et révoquez-la. "
            "Elle cesse de fonctionner dès sa requête suivante."
        ),
        "menu": "Paramètres > Développeur > Apps connectées",
    },
    "hi": {
        "title": "यहाँ से शुरू करें",
        "intro": (
            "Genos आपकी टीम की चैट, टास्क और नोट्स को एक ही वर्कस्पेस में रखता है, "
            "ताकि काम और उस पर होने वाली बातचीत जुड़ी रहे।"
        ),
        "bullets": [
            "चैट: टीम से बात करें और किसी थ्रेड को टास्क में बदलें।",
            "टास्क: प्रोजेक्ट, माइलस्टोन और बोर्ड। हर टास्क के अपने नोट्स और चर्चा होती है।",
            "नोट्स: आपके निजी नोट्स और टीम के साथ साझा फ़ोल्डर।",
            "Spotlight: AI असिस्टेंट। Cmd-K (Windows पर Ctrl-K) दबाएँ और जो कुछ आप देख सकते हैं, "
            "उसके बारे में पूछें।",
        ],
        "h_connect": "अपना AI ऐप कनेक्ट करें",
        "connect_intro": (
            "आप Claude जैसे AI ऐप को अपनी ओर से Genos में काम करने दे सकते हैं। कनेक्शन मंज़ूर "
            "करते समय आपको दिखता है कि वह क्या माँगता है; डिलीट करना बंद रहता है जब तक आप उसे "
            "चुन न लें।"
        ),
        "url_label": "AI ऐप्स के लिए आपका Genos पता:",
        "url_hint": "इसे ठीक वैसे ही लिखें: अंत में स्लैश नहीं और उसके बाद कुछ नहीं।",
        "url_missing": "(API का पता अपने Genos एडमिन से पूछें; वह {path} पर खत्म होता है)",
        "h_claude": "claude.ai और Claude Desktop",
        "claude_steps": [
            "Claude में कनेक्टर सेटिंग्स खोलें और एक कस्टम कनेक्टर जोड़ें।",
            "ऊपर दिया गया पता पेस्ट करें।",
            "पूछे जाने पर Genos में साइन इन करें, देखें कि ऐप क्या माँग रहा है, फिर मंज़ूर करें।",
        ],
        "h_code": "Claude Code",
        "code_run": "टर्मिनल में यह चलाएँ:",
        "code_signin": "फिर Claude Code में /mcp खोलें और genos में साइन इन करें।",
        "plan": "AI ऐप कनेक्ट करने के लिए Genos Core या उससे ऊपर का प्लान चाहिए।",
        "revoke": (
            "किसी ऐप की पहुँच खत्म करने के लिए {menu} खोलें और उसे रद्द करें। "
            "वह अपने अगले अनुरोध से काम करना बंद कर देता है।"
        ),
        "menu": "सेटिंग्स > डेवलपर > कनेक्टेड ऐप",
    },
    "zh": {
        "title": "从这里开始",
        "intro": "Genos 把团队的聊天、任务和笔记放在同一个工作区里，让工作和围绕它的讨论始终相连。",
        "bullets": [
            "聊天：与团队交流，并可把一个话题串直接转成任务。",
            "任务：项目、里程碑和看板。每个任务都有自己的笔记和讨论。",
            "笔记：只属于你的私人笔记，以及与团队共享的文件夹。",
            "Spotlight：AI 助手。按 Cmd-K（Windows 为 Ctrl-K），即可询问你能看到的任何内容。",
        ],
        "h_connect": "连接你的 AI 应用",
        "connect_intro": (
            "你可以让 Claude 等 AI 应用以你的身份在 Genos 中工作。批准连接时会显示它请求的权限；"
            "除非你勾选，否则删除权限保持关闭。"
        ),
        "url_label": "供 AI 应用使用的 Genos 地址：",
        "url_hint": "请按原样输入：末尾不要加斜杠，后面也不要加任何内容。",
        "url_missing": "（请向 Genos 管理员询问 API 地址，它以 {path} 结尾）",
        "h_claude": "claude.ai 和 Claude Desktop",
        "claude_steps": [
            "在 Claude 中打开连接器设置，添加自定义连接器。",
            "粘贴上面的地址。",
            "按提示登录 Genos，查看应用请求的内容，然后批准。",
        ],
        "h_code": "Claude Code",
        "code_run": "在终端中运行：",
        "code_signin": "然后在 Claude Code 中打开 /mcp，登录 genos。",
        "plan": "连接 AI 应用需要 Genos Core 或更高版本。",
        "revoke": "要结束某个应用的访问权限，请打开 {menu} 并撤销它。它在下一次请求时就会失效。",
        "menu": "设置 > 开发商 > 已连接的应用",
    },
    "ar": {
        "title": "ابدأ من هنا",
        "intro": (
            "يجمع Genos الدردشة والمهام والملاحظات الخاصة بفريقك في مساحة عمل واحدة، "
            "فيبقى العمل والحديث عنه مترابطين."
        ),
        "bullets": [
            "الدردشة: تحدّث مع فريقك وحوّل أي محادثة إلى مهمة.",
            "المهام: مشاريع ومراحل ولوحات. لكل مهمة ملاحظاتها ونقاشها.",
            "الملاحظات: ملاحظات خاصة بك ومجلدات تشاركها مع فريقك.",
            "Spotlight: مساعد الذكاء الاصطناعي. اضغط Cmd-K (أو Ctrl-K على Windows) واسأل عن "
            "أي شيء يمكنك رؤيته.",
        ],
        "h_connect": "اربط تطبيق الذكاء الاصطناعي",
        "connect_intro": (
            "يمكنك السماح لتطبيق ذكاء اصطناعي مثل Claude بالعمل في Genos نيابةً عنك. عند الموافقة "
            "على الربط ترى ما يطلبه التطبيق، ويبقى الحذف معطّلاً ما لم تحدده."
        ),
        "url_label": "عنوان Genos لتطبيقات الذكاء الاصطناعي:",
        "url_hint": "اكتبه تمامًا كما هو: بلا شرطة مائلة في النهاية ولا شيء بعده.",
        "url_missing": "(اطلب عنوان واجهة API من مسؤول Genos؛ وينتهي بـ {path})",
        "h_claude": "claude.ai وClaude Desktop",
        "claude_steps": [
            "في Claude افتح إعدادات الموصّلات وأضف موصّلًا مخصصًا.",
            "الصق العنوان أعلاه.",
            "سجّل الدخول إلى Genos عند الطلب، وراجع ما يطلبه التطبيق، ثم وافق.",
        ],
        "h_code": "Claude Code",
        "code_run": "شغّل هذا الأمر في الطرفية:",
        "code_signin": "ثم افتح /mcp داخل Claude Code وسجّل الدخول إلى genos.",
        "plan": "ربط تطبيق ذكاء اصطناعي يتطلب Genos Core أو أعلى.",
        "revoke": ("لإنهاء وصول تطبيق، افتح {menu} وألغِ وصوله. يتوقف عن العمل عند طلبه التالي."),
        "menu": "الإعدادات > المطور > التطبيقات المتصلة",
    },
}

LOCALES = tuple(_COPY)

# The marker: a note the person owns whose title is one of these. (No column or
# migration; the title is what `create_for` and `adopt_for_team` look for.)
TITLES = frozenset(copy["title"] for copy in _COPY.values())


def resolve_locale(user=None, request=None) -> str:
    """The locale to write the note in: the account's own `language` when it is
    set, else the browser's `Accept-Language` (the first tag with a copy here),
    else English. Never raises."""
    try:
        language = (getattr(user, "language", None) or "").strip().lower().split("-")[0]
        if language in _COPY:
            return language
        header = (request.META.get("HTTP_ACCEPT_LANGUAGE") or "") if request is not None else ""
        for chunk in header.split(","):
            primary = chunk.split(";")[0].strip().lower().split("-")[0]
            if primary in _COPY:
                return primary
    except Exception:  # noqa: BLE001 - a malformed header costs the language, nothing else
        pass
    return DEFAULT_LOCALE


def mcp_url() -> str:
    """The address a person pastes into an AI app, or "" when it is not known.

    `mcp_oauth.resource_identifier()` is the one definition (the OAuth metadata and the
    token audience use it): the API origin from `API_PUBLIC_BASE_URL` plus the MCP path, no
    trailing slash, no query string. It is only a usable address with an origin in front.
    """
    if not mcp_oauth.public_base():
        return ""
    return mcp_oauth.resource_identifier()


def _markdown(locale: str) -> str:
    copy = _COPY[locale]
    lines = [copy["intro"], ""]
    lines += [f"- {bullet}" for bullet in copy["bullets"]]

    url = mcp_url()
    lines += ["", f"## {copy['h_connect']}", "", copy["connect_intro"], ""]
    if url:
        lines += [copy["url_label"], "", f"`{url}`", "", copy["url_hint"], ""]
    lines += [f"### {copy['h_claude']}", ""]
    lines += [f"{i}. {step}" for i, step in enumerate(copy["claude_steps"], start=1)]
    if url:
        lines += ["", f"### {copy['h_code']}", "", copy["code_run"], ""]
        lines += ["```", f"claude mcp add --transport http genos {url}", "```", ""]
        lines += [copy["code_signin"]]
    lines += ["", copy["plan"], "", copy["revoke"].format(menu=copy["menu"])]
    return "\n".join(lines)


def build(locale: str) -> tuple[str, list]:
    """`(title, BlockNote blocks)` in that locale (English when it is unknown)."""
    if locale not in _COPY:
        locale = DEFAULT_LOCALE
    return _COPY[locale]["title"], markdown_to_blocks(_markdown(locale))


def _eligible(user) -> bool:
    if user is None or getattr(user, "pk", None) is None:
        return False
    if getattr(user, "is_demo", False) or getattr(user, "is_system_user", False):
        return False
    # A reviewer account owns a seeded demo team and builds its own sample data.
    from origin.management.commands.create_reviewer_account import reviewer_team

    return reviewer_team(user) is None


def _create(user, locale: str) -> PersonalNoteMaster | None:
    with transaction.atomic():
        # Serialise on the account: a concurrent call waits here, then sees the note.
        locked = CustomUser.objects.select_for_update().filter(pk=user.pk).first()
        if locked is None:
            return None
        if PersonalNoteMaster.objects.filter(owner=locked, title__in=TITLES).exists():
            return None
        title, body = build(locale)
        note = PersonalNoteMaster.objects.create(team=None, owner=locked, title=title, body=body)
        NotePermissionMaster.objects.create(
            team=None,
            user=locked,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_OWNER,
        )
        snapshot_note_version(
            team=None,
            editor=locked,
            note_type=NOTE_TYPE_PERSONAL,
            note_id=note.note_id,
            title=note.title,
            body=note.body,
        )
        return note


def create_for(user, *, request=None, language: str | None = None):
    """Give a NEW account its one "Start here" note. Returns the note, or None when
    nothing was created (already has one, not eligible, or a failure).

    Never raises: the caller is a signup, and the account is created regardless."""
    try:
        if not _eligible(user):
            return None
        locale = language if language in _COPY else resolve_locale(user, request)
        return _create(user, locale)
    except Exception as exc:  # noqa: BLE001 - the signup must not depend on this note
        # The type only: an exception's text can quote the address (MIP-622).
        logger.error(
            "start-here note failed user=%s: %s", getattr(user, "pk", None), describe_exception(exc)
        )
        return None


def adopt_for_team(user, team) -> bool:
    """Give the person's team-less "Start here" note to the first team they join.

    Called from `guide_notes.seed_guide_notes`. Notes are listed per team, so until
    this runs the note is not shown anywhere. A no-op when there is nothing to
    adopt (existing accounts, or already adopted). Never raises. Returns whether a
    note was moved.
    """
    try:
        with transaction.atomic():
            notes = list(
                PersonalNoteMaster.objects.select_for_update().filter(
                    owner=user, team__isnull=True, title__in=TITLES
                )
            )
            for note in notes:
                # Instances, not `QuerySet.update`: saving is what tells the browser's
                # change feed that the team's My Notes list moved.
                note.team = team
                note.save(update_fields=["team", "ts_updated_at"])
                for grant in NotePermissionMaster.objects.filter(
                    user=user, note_type=NOTE_TYPE_PERSONAL, note_id=note.note_id
                ):
                    grant.team = team
                    grant.save(update_fields=["team", "ts_updated_at"])
                NoteVersionMaster.objects.filter(
                    note_type=NOTE_TYPE_PERSONAL, note_id=note.note_id
                ).update(team=team)
            return bool(notes)
    except Exception as exc:  # noqa: BLE001 - joining a team matters more than this note
        logger.error(
            "start-here note adoption failed user=%s: %s",
            getattr(user, "pk", None),
            describe_exception(exc),
        )
        return False
