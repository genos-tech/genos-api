"""Tell the account's owner by email when an API key, webhook or AI-app connection is created
(MIP-2240).

The in-app notice for a new key or app (`mcp_notice.notify_credential_event`) sits inside the
account, so whoever holds a stolen session can dismiss it. This email goes to the address the
owner signed up with, where that session cannot reach it. It is a security notice, not a
notification: it is NOT behind the email categories or any switch, as the password-reset mail is
not. It names what was made (the kind and its label), when, and how to undo it. It never holds a
secret, a key, a token or an address the data goes to (a webhook is named by its host).

Sent after the surrounding transaction commits, so a rolled-back creation sends nothing, and it
never raises: a failed email must not undo the creation it reports (the failure is logged by
type only, since a mail error's text quotes the address).

Copy is in the app's seven languages (the member's `language`); anything else reads English.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from django.db import connection, transaction

from apis.request_id import describe_exception
from origin.services.email import send_templated_email

logger = logging.getLogger(__name__)

KIND_API_KEY = "api_key"
KIND_WEBHOOK = "webhook"
KIND_CONNECTION = "connection"

# Languages whose body reads right-to-left.
_RTL = {"ar"}

# One entry per locale. `{name}`, `{what}`, `{label}` and `{when}` are filled in; `kinds` is the
# noun each kind of credential goes by in that language (glossary: "API key" per language).
_COPY: dict[str, dict] = {
    "en": {
        "kinds": {
            KIND_API_KEY: "API key",
            KIND_WEBHOOK: "webhook",
            KIND_CONNECTION: "AI app connection",
        },
        "subject": "A new {what} was added to your Genos account",
        "heading": "A new {what} was added",
        "greeting": "Hi {name},",
        "body": 'A new {what} "{label}" was created on your Genos account on {when} (UTC).',
        "ok": "If this was you, there is nothing to do.",
        "undo": (
            "If it was not you, someone else may have access to your account. Sign in, remove it in "
            "Settings, and reset your password. Resetting your password signs out every session "
            "and ends all connected AI apps and API keys."
        ),
        "sign": "The Genos team",
    },
    "ja": {
        "kinds": {
            KIND_API_KEY: "API キー",
            KIND_WEBHOOK: "Webhook",
            KIND_CONNECTION: "AI アプリの連携",
        },
        "subject": "Genos アカウントに新しい{what}が追加されました",
        "heading": "新しい{what}が追加されました",
        "greeting": "{name} さん",
        "body": "{when} (UTC) に、Genos アカウントで新しい{what}「{label}」が作成されました。",
        "ok": "ご自身で作成した場合は、何もする必要はありません。",
        "undo": (
            "心当たりがない場合は、他の誰かがアカウントにアクセスしている可能性があります。"
            "サインインして設定から削除し、パスワードをリセットしてください。パスワードをリセット"
            "すると、すべてのセッションがサインアウトされ、連携中の AI アプリと API キーがすべて"
            "無効になります。"
        ),
        "sign": "Genos チーム",
    },
    "zh": {
        "kinds": {
            KIND_API_KEY: "API 密钥",
            KIND_WEBHOOK: "Webhook",
            KIND_CONNECTION: "AI 应用连接",
        },
        "subject": "您的 Genos 账户新增了一个{what}",
        "heading": "新增了一个{what}",
        "greeting": "{name}，您好：",
        "body": "{when}（UTC），您的 Genos 账户创建了一个新的{what}“{label}”。",
        "ok": "如果是您本人操作，无需处理。",
        "undo": (
            "如果不是您本人操作，可能有其他人正在访问您的账户。请登录后在设置中删除它，并重置密码。"
            "重置密码会让所有会话退出登录，并结束所有已连接的 AI 应用和 API 密钥。"
        ),
        "sign": "Genos 团队",
    },
    "es": {
        "kinds": {
            KIND_API_KEY: "clave de API",
            KIND_WEBHOOK: "webhook",
            KIND_CONNECTION: "conexión de app de IA",
        },
        "subject": "Se añadió una nueva {what} a tu cuenta de Genos",
        "heading": "Se añadió una nueva {what}",
        "greeting": "Hola, {name}:",
        "body": 'El {when} (UTC) se creó una nueva {what} "{label}" en tu cuenta de Genos.',
        "ok": "Si fuiste tú, no tienes que hacer nada.",
        "undo": (
            "Si no fuiste tú, es posible que otra persona tenga acceso a tu cuenta. Inicia sesión, "
            "elimínala en Ajustes y restablece tu contraseña. Al restablecerla se cierran todas las "
            "sesiones y terminan todas las apps de IA conectadas y las claves de API."
        ),
        "sign": "El equipo de Genos",
    },
    "fr": {
        "kinds": {
            KIND_API_KEY: "clé API",
            KIND_WEBHOOK: "webhook",
            KIND_CONNECTION: "connexion d'app IA",
        },
        "subject": "Un nouveau {what} a été ajouté à votre compte Genos",
        "heading": "Un nouveau {what} a été ajouté",
        "greeting": "Bonjour {name},",
        "body": "Un nouveau {what} « {label} » a été créé sur votre compte Genos le {when} (UTC).",
        "ok": "Si c'était vous, il n'y a rien à faire.",
        "undo": (
            "Si ce n'était pas vous, quelqu'un d'autre a peut-être accès à votre compte. Connectez-vous, "
            "supprimez-le dans les Paramètres et réinitialisez votre mot de passe. La réinitialisation "
            "déconnecte toutes les sessions et met fin à toutes les apps IA connectées et aux clés API."
        ),
        "sign": "L'équipe Genos",
    },
    "hi": {
        "kinds": {
            KIND_API_KEY: "API कुंजी",
            KIND_WEBHOOK: "Webhook",
            KIND_CONNECTION: "AI ऐप कनेक्शन",
        },
        "subject": "आपके Genos खाते में नया {what} जोड़ा गया",
        "heading": "नया {what} जोड़ा गया",
        "greeting": "नमस्ते {name},",
        "body": '{when} (UTC) को आपके Genos खाते में नया {what} "{label}" बनाया गया।',
        "ok": "अगर यह आपने किया है, तो कुछ करने की ज़रूरत नहीं है।",
        "undo": (
            "अगर यह आपने नहीं किया, तो हो सकता है किसी और के पास आपके खाते की पहुँच हो। साइन इन करें, "
            "सेटिंग्स में इसे हटाएँ और अपना पासवर्ड रीसेट करें। पासवर्ड रीसेट करने पर सभी सेशन साइन आउट "
            "हो जाते हैं और जुड़े हुए सभी AI ऐप और API कुंजियाँ बंद हो जाती हैं।"
        ),
        "sign": "Genos टीम",
    },
    "ar": {
        "kinds": {
            KIND_API_KEY: "مفتاح API",
            KIND_WEBHOOK: "Webhook",
            KIND_CONNECTION: "اتصال تطبيق ذكاء اصطناعي",
        },
        "subject": "تمت إضافة {what} جديد إلى حسابك في Genos",
        "heading": "تمت إضافة {what} جديد",
        "greeting": "مرحبًا {name}،",
        "body": 'تم إنشاء {what} جديد "{label}" في حسابك في Genos بتاريخ {when} (UTC).',
        "ok": "إذا كنت أنت من فعل ذلك، فلا حاجة لأي إجراء.",
        "undo": (
            "إذا لم تكن أنت، فقد يكون شخص آخر قادرًا على الوصول إلى حسابك. سجّل الدخول واحذفه من "
            "الإعدادات ثم أعد تعيين كلمة المرور. إعادة تعيين كلمة المرور تُنهي جميع الجلسات وتُنهي "
            "جميع تطبيقات الذكاء الاصطناعي المتصلة ومفاتيح API."
        ),
        "sign": "فريق Genos",
    },
}

LOCALES = tuple(_COPY)


def _locale(user) -> str:
    lang = (getattr(user, "language", None) or "").strip().lower().split("-")[0]
    return lang if lang in _COPY else "en"


def build_message(user, *, kind: str, label: str, when: datetime) -> dict:
    """The subject and the template context, in the member's language. Pure: sends nothing."""
    locale = _locale(user)
    copy = _COPY[locale]
    what = copy["kinds"][kind]
    # One line, bounded: a name is typed by the member (or whoever holds the session) and must not
    # be able to bend the mail (line breaks) or fill it.
    safe_label = " ".join(str(label or "").split())[:80]
    fields = {
        "name": user.username,
        "what": what,
        "label": safe_label,
        "when": when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }
    return {
        "subject": copy["subject"].format(**fields),
        "context": {
            "lang": locale,
            "dir": "rtl" if locale in _RTL else "ltr",
            "heading": copy["heading"].format(**fields),
            "greeting": copy["greeting"].format(**fields),
            "body": copy["body"].format(**fields),
            "ok": copy["ok"],
            "undo": copy["undo"],
            "sign": copy["sign"],
        },
    }


def _send(user_pk, *, kind: str, label: str, when: datetime) -> None:
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415

    try:
        user = CustomUser.objects.filter(pk=user_pk, is_active=True).first()
        if user is None or not user.email:
            return
        message = build_message(user, kind=kind, label=label, when=when)
        send_templated_email(
            to=user.email,
            subject=message["subject"],
            template_base="credential_created",
            context=message["context"],
        )
    except Exception as exc:  # noqa: BLE001 - a notice never undoes what it reports
        logger.error(
            "Credential email failed kind=%s user=%s: %s", kind, user_pk, describe_exception(exc)
        )


def _spawn(job) -> None:
    """Run `job` on a daemon thread, so a slow mail provider never delays the response that carries
    a new secret (MIP-2290). The job never raises; the thread releases its own DB connection. A
    restart loses a mail in flight, which is acceptable for a notice (the in-app notice and the
    security log remain). Tests patch this to run inline."""

    def runner() -> None:
        try:
            job()
        finally:
            connection.close()

    try:
        threading.Thread(target=runner, daemon=True, name="credential-alert-mail").start()
    except Exception as exc:  # noqa: BLE001 - e.g. the process cannot start a thread
        logger.error("Could not start credential mail: %s", describe_exception(exc))


def notify_created(user, *, kind: str, label: str) -> None:
    """Send the owner email after the commit, from a background step. `kind` is a `KIND_*`."""
    when = datetime.now(timezone.utc)
    pk = user.pk
    transaction.on_commit(
        lambda: _spawn(lambda: _send(pk, kind=kind, label=label, when=when)), robust=True
    )


# Repeated wrong password confirmations (MIP-2290). `{n}` is the count in the last hour.
_GUESS: dict[str, dict] = {
    "en": {
        "subject": "Someone is guessing your Genos password",
        "heading": "Repeated wrong password confirmations",
        "greeting": "Hi {name},",
        "body": (
            "{n} wrong passwords were typed into the password check of your Genos account within "
            "the last hour (latest: {when} UTC)."
        ),
        "ok": "If this was you, there is nothing to do.",
        "undo": (
            "If it was not you, someone may hold a signed-in session of yours. Reset your "
            "password now: it signs out every session and ends all connected AI apps and API keys."
        ),
        "sign": "The Genos team",
    },
    "ja": {
        "subject": "Genos のパスワードを推測しようとする操作がありました",
        "heading": "パスワード確認の失敗が繰り返されました",
        "greeting": "{name} さん",
        "body": (
            "直近 1 時間に、Genos アカウントのパスワード確認で誤ったパスワードが {n} 回入力されました"
            "（最新: {when} UTC）。"
        ),
        "ok": "ご自身の操作であれば、何もする必要はありません。",
        "undo": (
            "心当たりがない場合は、サインイン済みのセッションを第三者が持っている可能性があります。"
            "すぐにパスワードをリセットしてください。すべてのセッションがサインアウトされ、"
            "連携中の AI アプリと API キーが無効になります。"
        ),
        "sign": "Genos チーム",
    },
    "zh": {
        "subject": "有人正在猜测您的 Genos 密码",
        "heading": "多次密码确认失败",
        "greeting": "{name}，您好：",
        "body": "最近一小时内，您的 Genos 账户的密码确认步骤中输入了 {n} 次错误密码（最近一次：{when} UTC）。",
        "ok": "如果是您本人操作，无需处理。",
        "undo": (
            "如果不是您本人，可能有人持有您已登录的会话。请立即重置密码：这会让所有会话退出登录，"
            "并结束所有已连接的 AI 应用和 API 密钥。"
        ),
        "sign": "Genos 团队",
    },
    "es": {
        "subject": "Alguien está intentando adivinar tu contraseña de Genos",
        "heading": "Confirmaciones de contraseña incorrectas repetidas",
        "greeting": "Hola, {name}:",
        "body": (
            "En la última hora se escribieron {n} contraseñas incorrectas en la comprobación de "
            "contraseña de tu cuenta de Genos (la última: {when} UTC)."
        ),
        "ok": "Si fuiste tú, no tienes que hacer nada.",
        "undo": (
            "Si no fuiste tú, alguien podría tener una sesión iniciada tuya. Restablece tu "
            "contraseña ahora: cierra todas las sesiones y termina todas las apps de IA "
            "conectadas y las claves de API."
        ),
        "sign": "El equipo de Genos",
    },
    "fr": {
        "subject": "Quelqu'un essaie de deviner votre mot de passe Genos",
        "heading": "Confirmations de mot de passe erronées répétées",
        "greeting": "Bonjour {name},",
        "body": (
            "{n} mots de passe erronés ont été saisis dans la vérification du mot de passe de votre "
            "compte Genos au cours de la dernière heure (dernier : {when} UTC)."
        ),
        "ok": "Si c'était vous, il n'y a rien à faire.",
        "undo": (
            "Si ce n'était pas vous, quelqu'un détient peut-être une de vos sessions ouvertes. "
            "Réinitialisez votre mot de passe maintenant : cela déconnecte toutes les sessions et "
            "met fin à toutes les apps IA connectées et aux clés API."
        ),
        "sign": "L'équipe Genos",
    },
    "hi": {
        "subject": "कोई आपका Genos पासवर्ड अंदाज़ा लगाने की कोशिश कर रहा है",
        "heading": "पासवर्ड पुष्टि बार-बार गलत हुई",
        "greeting": "नमस्ते {name},",
        "body": (
            "पिछले एक घंटे में आपके Genos खाते की पासवर्ड जाँच में {n} गलत पासवर्ड डाले गए "
            "(सबसे ताज़ा: {when} UTC)।"
        ),
        "ok": "अगर यह आपने किया है, तो कुछ करने की ज़रूरत नहीं है।",
        "undo": (
            "अगर यह आपने नहीं किया, तो हो सकता है किसी के पास आपका साइन-इन सेशन हो। अभी पासवर्ड रीसेट करें: "
            "इससे सभी सेशन साइन आउट हो जाते हैं और जुड़े हुए सभी AI ऐप और API कुंजियाँ बंद हो जाती हैं।"
        ),
        "sign": "Genos टीम",
    },
    "ar": {
        "subject": "شخص ما يحاول تخمين كلمة مرور Genos الخاصة بك",
        "heading": "تأكيدات كلمة مرور خاطئة متكررة",
        "greeting": "مرحبًا {name}،",
        "body": (
            "تم إدخال {n} كلمات مرور خاطئة في التحقق من كلمة المرور لحسابك في Genos خلال الساعة "
            "الماضية (آخرها: {when} UTC)."
        ),
        "ok": "إذا كنت أنت من فعل ذلك، فلا حاجة لأي إجراء.",
        "undo": (
            "إذا لم تكن أنت، فقد يملك شخص ما جلسة مسجّلة الدخول لحسابك. أعد تعيين كلمة المرور الآن: "
            "هذا يُنهي جميع الجلسات وجميع تطبيقات الذكاء الاصطناعي المتصلة ومفاتيح API."
        ),
        "sign": "فريق Genos",
    },
}


def build_guessing_message(user, *, count: int, when: datetime) -> dict:
    locale = _locale(user)
    copy = _GUESS[locale]
    fields = {
        "name": user.username,
        "n": count,
        "when": when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }
    return {
        "subject": copy["subject"].format(**fields),
        "context": {
            "lang": locale,
            "dir": "rtl" if locale in _RTL else "ltr",
            "heading": copy["heading"].format(**fields),
            "greeting": copy["greeting"].format(**fields),
            "body": copy["body"].format(**fields),
            "ok": copy["ok"],
            "undo": copy["undo"],
            "sign": copy["sign"],
        },
    }


def _send_guessing(user_pk, *, count: int, when: datetime) -> None:
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415

    try:
        user = CustomUser.objects.filter(pk=user_pk, is_active=True).first()
        if user is None or not user.email:
            return
        message = build_guessing_message(user, count=count, when=when)
        send_templated_email(
            to=user.email,
            subject=message["subject"],
            template_base="credential_created",
            context=message["context"],
        )
    except Exception as exc:  # noqa: BLE001 - a notice never breaks the 403 it follows
        logger.error("Guessing email failed user=%s: %s", user_pk, describe_exception(exc))


def notify_guessing(user, *, count: int = 0) -> None:
    """Email the owner that wrong password confirmations are piling up (MIP-2290). Called once per
    window by `recent_auth.note_failure`; sent after commit from a background step."""
    from origin.services import recent_auth  # noqa: PLC0415

    when = datetime.now(timezone.utc)
    pk = user.pk
    n = count or recent_auth.ALERT_AFTER_FAILURES
    transaction.on_commit(
        lambda: _spawn(lambda: _send_guessing(pk, count=n, when=when)), robust=True
    )
