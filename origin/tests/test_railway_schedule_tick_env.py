"""`schedule-tick` must declare every variable a member's job reads (MIP-2460).

The cron runs the schedules' jobs, and a skill run on it is the web service's whole agent loop and
search. The service only has the variables `.railway/railway.ts` declares and someone set on
Railway, so a setting that was added for the web and never for the tick is silently its default
there: skills off, no search index, another credit rule. A rule needs a mechanism: every environment
variable the app's code reads is either declared on `schedule-tick` or listed here with the reason a
job does not need it. A NEW variable fails this test until someone decides which.

Which variables a job reads is not traced call by call (a job reaches most of the app through the
agent's tools); the whole app is scanned. The file only `preserve()`s names, so this checks names,
never values.

SimpleTestCase (no DB, no network).
"""

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[2]
RAILWAY_TS = REPO / ".railway" / "railway.ts"

# Helpers that read the variable named by their first argument.
ENV_HELPERS = ("_env_int", "_env_on", "_env_switch")
# Files that read `os.environ.get(<not a literal>)`: the helpers above, the retention periods
# (read by the retention cron, never by a schedule) and the log formatter's RAILWAY_* fields, which
# Railway sets itself. A new one would hide a name from this test.
DYNAMIC_READERS = {
    "apis/logging_formatters.py",
    "apis/settings.py",
    "origin/search_engine/agent/tools/field_limits.py",
    "origin/services/briefing_gate.py",
    "origin/services/retention.py",
    "origin/views/public/mcp/result_limits.py",
    "origin/views/public/mcp/result_log.py",
    "origin/views/public/mcp/request_guard.py",
}


def _names(block: str) -> set[str]:
    return set(block.split())


# Read by the code, never by a job. Setting one of these on the tick would change nothing.
NOT_READ_BY_A_JOB = {
    "the HTTP server: sign-in, tokens, hosts, CORS, request limits and logs": _names("""
        ACCESS_TOKEN_LIFETIME_MINUTES CORS_ALLOWED_ORIGINS CSRF_TRUSTED_ORIGINS
        EMAIL_VERIFICATION_TOKEN_EXPIRY_MINUTES JWT_ACCEPT_HS256 JWT_KEY_ID JWT_PRIVATE_KEY
        JWT_PUBLIC_KEY JWT_SIGNING_ALGORITHM PASSWORD_BREACH_CHECK
        PASSWORD_RESET_TOKEN_EXPIRY_MINUTES PUBLIC_API_DISABLED RAILWAY_PRIVATE_DOMAIN
        REQUEST_PERF_CPU_STAT_PATH REQUEST_PERF_LOG SIGNIN_BACKOFF_FAILURES SIGNIN_SPRAY_ACCOUNTS
        TEAM_INVITE_TOKEN_EXPIRY_MINUTES THROTTLE_AGENT THROTTLE_AGENT_SUMMARY THROTTLE_AGENT_UNDO
        THROTTLE_BATCH_READ THROTTLE_CHANGE_FEED THROTTLE_CLIENT_PERF THROTTLE_EXPORT
        THROTTLE_INVITE THROTTLE_MCP_CONNECTIONS THROTTLE_MCP_OAUTH_AUTHORIZE
        THROTTLE_MCP_OAUTH_CONSENT THROTTLE_MCP_OAUTH_REGISTER THROTTLE_MCP_OAUTH_TOKEN
        THROTTLE_MCP_ANONYMOUS THROTTLE_SEARCH THROTTLE_USER_BURST THROTTLE_USER_SUSTAINED TRUST_CLOUDFLARE_IP
    """),
    "the MCP server, public API and AI app connections": _names("""
        AGENT_CALL_LOG AGENT_MAX_BODY_BYTES MCP_BODY_PAGE_CHARS
        MCP_BRIEFING_EVENT_DESCRIPTION_MAX_CHARS MCP_CORE_RULES MCP_DISABLED
        MCP_EVENT_DESCRIPTION_MAX_CHARS MCP_EVENT_MAX_ATTENDEES MCP_ITEM_MAX_CHARS
        MCP_LIST_DEFAULT_LIMIT MCP_LIST_MAX_LIMIT MCP_MAX_REQUEST_BYTES MCP_OAUTH_CIMD_ENABLED
        MCP_OAUTH_CLIENT_PRUNE_DAYS MCP_OAUTH_ENABLED MCP_OAUTH_MAX_REGISTRATIONS_PER_DAY
        MCP_RESULT_MAX_CHARS MCP_RESULT_SHADOW_CAP_CHARS MCP_SEARCH_QUERY_MAX_CHARS
        MCP_SESSION_ID_PROBE MCP_TIER_LIMITS_MODE OAUTH_REVOKE_ON_DISCONNECT OPENAI_APPS_CHALLENGE
        PUBLIC_API_MCP_RATE PUBLIC_API_RATE BRIEFING_LOCK_SECONDS BRIEFING_ONE_IN_FLIGHT
        BRIEFING_RETRY_AFTER_SECONDS
    """),
    "Spotlight's own request: sessions, approvals, undo, resume, ceilings of an ask": _names("""
        AGENT_AUTO_EFFORT AGENT_RESUME_KEEPS_BLOCKS AGENT_RESUME_KEEPS_TURNS AGENT_UNDO_NOTE_IN_MEMORY
        AI_CEILING_PAUSE AI_CEILING_ROUTE_CHEAPEST RAG_CONVERSATION_INDEX_ON_COMPLETE
        RAG_SESSION_ROLLING_SUMMARY SESSION_MAX_PRIOR_TURNS SESSION_TTL_MINUTES
    """),
    "billing pages, signup and the Stripe webhook (a job reads the plan from the database)": _names("""
        FIRST_MONTH_CORE_TRIAL STRIPE_ALLOW_PROMOTION_CODES STRIPE_AUTOMATIC_TAX
        STRIPE_CREDIT_PACK_PRICES STRIPE_DEFAULT_CURRENCY STRIPE_PRICES_BY_CURRENCY
        STRIPE_PRICE_CORE STRIPE_PRICE_MAX STRIPE_PRICE_MAX_LEGACY STRIPE_PRICE_PRO
        STRIPE_PRICE_PRO_LEGACY STRIPE_SECRET_KEY STRIPE_TOS_CONSENT STRIPE_WEBHOOK_SECRET
    """),
    "email: a report is delivered to the Inbox and by web push, never by mail": _names("""
        ANYMAIL_RESEND_SIGNING_SECRET API_PUBLIC_BASE_URL DEFAULT_FROM_EMAIL EMAIL_BACKEND
        EMAIL_NOTIFICATIONS_ENABLED EMAIL_NOTIFY_AWAY_MINUTES EMAIL_NOTIFY_COOLDOWN_MINUTES
        RESEND_API_KEY
    """),
    "connecting GitHub and its webhooks (a job only uses a connection that exists)": _names("""
        BACKEND_BASE_URL GITHUB_CONNECT_REPO_SCOPE_REMOVED GITHUB_OAUTH_CLIENT_ID
        GITHUB_OAUTH_CLIENT_SECRET GITHUB_WEBHOOK_SECRET
    """),
    "the GitHub App, off on the tick (2026-11-27): a scheduled skill's pull request tools use the "
    "member's GitHub OAuth connection": _names("""
        GITHUB_APP_CLIENT_ID GITHUB_APP_CLIENT_SECRET GITHUB_APP_ENABLED GITHUB_APP_ID
        GITHUB_APP_PRIVATE_KEY GITHUB_APP_SLUG GITHUB_APP_WEBHOOK_SECRET
    """),
    "indexing, team reindex and demo provisioning, done by other processes": _names("""
        DEMO_PROVISION_IN_BACKGROUND OPENSEARCH_REFRESH_INTERVAL OPENSEARCH_REPLICAS
        OPENSEARCH_SHARDS RAG_BULK_REFRESH SEARCH_BULK_BATCH_SIZE SEARCH_EMBEDDING_BATCH_SIZE
        SEARCH_TEAM_REINDEX_MAX_PROCS SEARCH_TEAM_REINDEX_MODE SEARCH_TEAM_REINDEX_TIMEOUT_S
    """),
    "logs: Railway sets its own, and log shipping is off until set (add it to read a cron's logs)": _names("""
        BETTERSTACK_INGEST_URL BETTERSTACK_SOURCE_TOKEN RAILWAY_ENVIRONMENT
    """),
    "ops alerts, security events and other crons": _names("""
        CREDENTIAL_AUTOSUSPEND_MODE EXTERNAL_CRON_HEARTBEAT_TOKEN OPS_5XX_BURST OPS_ALERT_EMAIL
        OPS_CERT_WARN_DAYS OPS_HEALTH_TOKEN OPS_OPENSEARCH_DISK_PERCENT OPS_OPENSEARCH_DOC_LIMIT
        OPS_OPENSEARCH_HEAP_PERCENT OPS_OPENSEARCH_HEAP_TICKS OPS_RUNBOOK_URL PR_NUDGES_ENABLED
        PR_READY_NUDGE_DAYS PR_STALE_NUDGE_DAYS SECURITY_EVENT_RETENTION_DAYS
        SECURITY_FORBIDDEN_BURST SECURITY_READ_CALLS_PER_MINUTE SECURITY_READ_CHARS_PER_MINUTE
        SECURITY_READ_USER_MULTIPLIER SECURITY_WRITE_ROWS_PER_WINDOW SECURITY_WRITE_WINDOW_SECONDS
        RAG_JUDGE_DRIFT_THRESHOLD RAG_JUDGE_SAMPLE_RATE RAG_TOOL_SELECTION_NORTH_STAR
    """),
    "tests, other tools and files a cron container does not have": _names("""
        DJANGO_MEDIA_ROOT DJANGO_SETTINGS_MODULE GIPHY_API_KEY RECORD_GOLDEN TAVILY_API_KEY
        UNIFIED_MESSAGING_DUAL_WRITE
    """),
}

# A job would read these, but production sets them on no service: the code's default applies to the
# web service and the tick alike. Setting one on backend-django means moving it to the tick's `env`
# in `.railway/railway.ts` too.
DEFAULT_ON_BOTH = _names("""
    AGENT_BODY_ON_USER_MODEL AGENT_CHEATSHEET_FROM_REGISTRY AGENT_CLOSED_ANCESTOR_RULE
    AGENT_CLOSING_LINE_IN_ORIGIN_THREAD AGENT_CONTENT_LENGTH AGENT_DISABLED_TOOLS
    AGENT_DYNAMIC_STEPS AGENT_FETCH_TASK_ANCHORS AGENT_INAPP_HELP_TOOL AGENT_INAPP_NOTE_COMMENT_TOOL
    AGENT_INAPP_TIMELINE_TOOL AGENT_INBOX_REPORT_LABEL AGENT_INJECT_CURRENT_DATE
    AGENT_LOOP_REPEAT_LIMIT AGENT_MAX_STEPS AGENT_MAX_STEPS_HARD_CAP AGENT_NOTE_THREADS_IN_GET_NOTE
    AGENT_PARITY_FOLLOWUPS AGENT_PARITY_TOOLS AGENT_REJECT_REASON AGENT_STEP_CAP_WRAPUP
    AGENT_TASK_MOVE_PARITY AGENT_TEAM_IN_CURRENT_USER AGENT_TELL_MODEL_UNDO AGENT_THINKING_BUDGETS
    AI_BENCHMARK_REGRESSION_PCT AI_FREE_DAILY_BREAKER AI_REQUEST_MAX_JPY_MILLI
    AI_REQUEST_MAX_USD_MICRO AI_TEAM_MONTHLY_CEILING_JPY AI_USER_DAILY_ALERT_JPY AI_USER_DAILY_ALERT_USD CLAUDE_PROMPT_CACHE
    COHERE_API_KEY COHERE_RERANK_BASE_URL COHERE_RERANK_MODEL COHERE_RERANK_TIMEOUT_S
    CREDIT_POLICY_YAML GEMINI_API_KEY GEMINI_EXPLICIT_CACHE_MAX_HISTORY_RATIO
    GEMINI_EXPLICIT_CACHE_TTL_S GEMINI_LLM_LOCATION GEMINI_RETRY_ATTEMPTS
    GOOGLE_APPLICATION_CREDENTIALS LLM_MODELS_YAML LOG_LEVEL MODEL_QUOTA_FALLBACK
    OAUTH_TOKEN_ENCRYPTION_KEY OPENAI_MAX_TOKENS OPENAI_MODEL OPENSEARCH_ALIAS RAG_ABSTENTION_GATE
    RAG_AGENT_SELF_CRITIQUE RAG_CHAT_CONTEXT_WINDOW RAG_COLLAPSE_MIRROR_RESULTS
    RAG_CRITIQUE_MAX_STEPS RAG_CRITIQUE_RETRIEVAL RAG_DEDUP_BY_HASH RAG_EMBEDDING_CACHE_TTL_S
    RAG_FRESHNESS_HALF_LIFE_DAYS RAG_GRAPH_EXPANSION RAG_GRAPH_MAX_HOPS RAG_GRAPH_MAX_NEIGHBORS
    RAG_GRAPH_MAX_SOURCES RAG_GRAPH_REVERSE_WEIGHT RAG_GRAPH_VECTOR_FLOOR RAG_GRAPH_WEIGHT
    RAG_MENTION_BOOST RAG_MENTION_BOOST_WEIGHT RAG_PARALLEL_TOOLS RAG_PARALLEL_TOOLS_MAX_WORKERS
    RAG_PLANNING_MODEL RAG_RANK_SOURCES_BY_CITATION RAG_RERANKER_MODEL RAG_RERANKER_PROVIDER
    RAG_RERANK_FUSION RAG_RERANK_FUSION_WEIGHT RAG_RERANK_FUSION_WEIGHT_EXACT
    RAG_RERANK_FUSION_WEIGHT_PARAPHRASE RAG_RERANK_INPUT_K RAG_RERANK_KEEP_DROPPED
    RAG_RERANK_OUTPUT_K RAG_REWRITE_MODEL RAG_REWRITE_NUM_VARIANTS RAG_SESSION_TOOL_CACHE
    RAG_SESSION_TOOL_CACHE_TTL_S RAG_SUBPROCESS_THINKING_BUDGET RAG_USE_QUERY_REWRITE
    RAG_USE_RERANKER TIER_QUOTAS_JSON
""")


def _source_files():
    """The app's code: settings and `origin`, without tests, migrations, evals and the other
    crons' management commands."""
    yield REPO / "apis" / "settings.py"
    for path in sorted((REPO / "apis").glob("*.py")):
        if path.name not in {
            "settings.py",
            "settings_dev.py",
            "settings_test.py",
            "test_runner.py",
        }:
            yield path
    for path in sorted((REPO / "origin").rglob("*.py")):
        parts = path.relative_to(REPO).parts
        if {"tests", "migrations", "evals"} & set(parts) or path.name.startswith("test"):
            continue
        if "commands" in parts and path.name != "schedule_tick.py":
            continue
        yield path


def _is_environ(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _read_argument(node):
    """The expression naming the variable, when `node` reads the environment."""
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        name = getattr(func, "attr", getattr(func, "id", ""))
        if name == "getenv" or name in ENV_HELPERS:
            return node.args[0]
        if name in ("get", "pop", "setdefault") and _is_environ(getattr(func, "value", None)):
            return node.args[0]
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return node.slice if _is_environ(node.value) else None
    if isinstance(node, ast.Compare) and isinstance(node.ops[0], (ast.In, ast.NotIn)):
        return node.left if _is_environ(node.comparators[0]) else None
    return None


def _string_constants(tree: ast.Module) -> dict[str, str]:
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _scan() -> tuple[set[str], set[str]]:
    """(variable names read, files that read a name this scan cannot see)."""
    names: set[str] = set()
    dynamic: set[str] = set()
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {path.stem: _string_constants(tree) for path, tree in trees.items()}
    for path, tree in trees.items():
        constants = modules[path.stem] = _string_constants(tree)
        for node in ast.walk(tree):
            argument = _read_argument(node)
            if argument is None:
                continue
            if isinstance(argument, ast.Constant):
                names.add(argument.value)
            elif isinstance(argument, ast.Name) and argument.id in constants:
                names.add(constants[argument.id])
            elif (  # a constant of another module: `core_rules.FLAG_ENV`
                isinstance(argument, ast.Attribute)
                and isinstance(argument.value, ast.Name)
                and argument.attr in modules.get(argument.value.id, {})
            ):
                names.add(modules[argument.value.id][argument.attr])
            else:
                dynamic.add(path.relative_to(REPO).as_posix())
    return names, dynamic


def _env_names(source: str, service_name: str) -> set[str]:
    """Names in the `env: { ... }` block of `service("<service_name>", {...})`."""
    # Comments go first: they quote `${{...}}` references, whose braces would end the block early.
    source = re.sub(r"^\s*//[^\n]*$", "", source, flags=re.M)
    start = source.index(f'service("{service_name}"')
    nxt = source.find("service(", start + 1)
    block = source[start : nxt if nxt != -1 else len(source)]
    env = re.search(r"env:\s*\{(.*?)\}", block, re.S)
    assert env, f"{service_name} has no env block"
    return set(re.findall(r"\b([A-Z][A-Z0-9_]*):\s*preserve\(\)", env.group(1)))


class ScheduleTickDeclaresWhatItsJobsReadTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.read, cls.dynamic = _scan()
        cls.declared = _env_names(RAILWAY_TS.read_text(), "schedule-tick")
        cls.not_read = set().union(*NOT_READ_BY_A_JOB.values())

    def test_every_variable_the_code_reads_is_declared_or_explained(self):
        missing = sorted(self.read - self.declared - self.not_read - DEFAULT_ON_BOTH)
        self.assertEqual(
            missing,
            [],
            f"{missing}: if a schedule's job can read it, preserve() it on schedule-tick in "
            ".railway/railway.ts (and set it on Railway); otherwise add it to a list in this test",
        )

    def test_the_lists_hold_no_stale_or_declared_name(self):
        listed = self.not_read | DEFAULT_ON_BOTH
        self.assertEqual(sorted(listed - self.read), [], "no code reads these any more")
        self.assertEqual(sorted(listed & self.declared), [], "declared on the tick: unlist them")
        self.assertEqual(sorted(self.not_read & DEFAULT_ON_BOTH), [])

    def test_no_new_reader_hides_a_name(self):
        self.assertEqual(sorted(self.dynamic - DYNAMIC_READERS), [])

    def test_the_scan_sees_each_way_of_reading(self):
        # Guards the scan: an empty set would pass the first test.
        for name in (
            "GENOS_SKILLS",  # os.environ.get
            "REPORT_AI_DASHBOARDS",  # in a job, not in settings
            "MCP_OAUTH_ENABLED",  # a helper
            "FIRST_MONTH_CORE_TRIAL",  # a constant naming it
        ):
            self.assertIn(name, self.read)

    def test_what_a_job_cannot_work_without_is_declared(self):
        # Pinned by name so that moving one into a list above is not a quiet way to pass.
        needed = _names("""
            AI_AGENT_KILL_SWITCH AI_MONTHLY_BUDGET_JPY AI_TEAM_MONTHLY_CEILING_USD AI_COST_METER AI_CREDITS_AUTHORITATIVE AI_CREDITS_SHADOW EMBEDDING_PROVIDER
            FRONTEND_BASE_URL GENOS_SKILLS GENOS_SKILLS_PUBLIC HTML_NOTES OPENSEARCH_CA_CERT
            OPENSEARCH_HOST OPENSEARCH_INDEX OPENSEARCH_PASSWORD OPENSEARCH_PORT OPENSEARCH_USERNAME
            OPENSEARCH_USE_SSL REDIS_URL REPORT_AI_DASHBOARDS REPORT_AI_SENTENCES
            SOCKETS_INTERNAL_SECRET SOCKETS_INTERNAL_URL VERTEX_EMBEDDING_DIMENSIONS
            VERTEX_EMBEDDING_MODEL
        """)
        self.assertEqual(sorted(needed - self.declared), [])

    def test_the_github_app_is_not_declared(self):
        # Decided, not forgotten: declaring it puts the App's private key on one more service.
        self.assertEqual(sorted(n for n in self.declared if n.startswith("GITHUB_APP_")), [])

    def test_matches_backend_django_when_the_platform_checkout_is_next_to_this_one(self):
        platform = REPO.parent / "genos-platform" / ".railway" / "railway.ts"
        if not platform.exists():
            self.skipTest("genos-platform checkout not present (CI has only this repo)")
        django = _env_names(platform.read_text(), "backend-django")
        self.assertEqual(
            sorted(django - self.declared - self.not_read),
            [],
            "backend-django sets these: the tick must declare them, or no job may read them",
        )
