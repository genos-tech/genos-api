import { defineRailway, github, preserve, project, service } from "railway/iac";

// The cron services (MIP-857). Railway's Config as Code is deprecated: existing
// config files stop working on 2026-12-01 and a service created after
// 2026-08-28 cannot opt in. Each cron's schedule, start command and restart
// policy used to live in its own `railway-<name>.toml`; they live here now.
//
// WHY A NAMED PARTIAL. This file declares only the crons. A partial owns only
// what it declares, so applying it can never delete the databases, the web
// services or anything else. An unnamed whole-project file would delete every
// resource it omits. Keep `export const partial` while this file lists only
// part of the project.
//
// VARIABLES ARE NOT MANAGED HERE. Every variable is `preserve()`: its existing
// value is kept and no secret is written to source. Rotating a secret is still
// done in Railway (RAILWAY_DEPLOY.md, MIP-791) and this file does not change.
// A variable ADDED to a service must also be listed here, or plan refuses.
//
// To change anything: edit this file, `railway config plan`, review, then
// `railway config apply`. See .railway/README.md in this repo's docs.
export const partial = "crons";

export default defineRailway(() => {
  const agentJudgeSampler = service("agent-judge-sampler", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false, rootDirectory: "/" }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      AI_COST_METER: preserve(),
      AI_CREDITS_AUTHORITATIVE: preserve(),
      AI_CREDITS_SHADOW: preserve(),
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      GEMINI_API_KEY: preserve(),
      GEMINI_LOCATION: preserve(),
      GEMINI_MODEL: preserve(),
      GEMINI_PROJECT: preserve(),
      GEMINI_SA_BASE64: preserve(),
      GEMINI_SERVICE_ACCOUNT_FILE: preserve(),
      GEMINI_USE_VERTEX: preserve(),
      JWT_SECRET_KEY: preserve(),
      LLM_PROVIDER: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "0 * * * *",
      startCommand: "sh /app/railway-judge-sampler-entrypoint.sh --heartbeat agent-judge-sampler",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const aiCostDashboardDaily = service("ai-cost-dashboard-daily", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      AI_COST_ARCHIVE_URI: preserve(),
      AI_COST_METER: preserve(),
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      GEMINI_SA_BASE64: preserve(),
      GEMINI_SERVICE_ACCOUNT_FILE: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "15 6 * * *",
      startCommand: "sh /app/railway-ai-cost-dashboard-entrypoint.sh --days 1 --heartbeat ai-cost-dashboard-daily",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const aiCostDashboardMonthly = service("ai-cost-dashboard-monthly", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      AI_COST_ARCHIVE_URI: preserve(),
      AI_COST_METER: preserve(),
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      GEMINI_SA_BASE64: preserve(),
      GEMINI_SERVICE_ACCOUNT_FILE: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "45 6 1 * *",
      startCommand: "sh /app/railway-ai-cost-dashboard-entrypoint.sh --last-month --heartbeat ai-cost-dashboard-monthly",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const aiCostDashboardWeekly = service("ai-cost-dashboard-weekly", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      AI_COST_ARCHIVE_URI: preserve(),
      AI_COST_METER: preserve(),
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      GEMINI_SA_BASE64: preserve(),
      GEMINI_SERVICE_ACCOUNT_FILE: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "30 6 * * 1",
      startCommand: "sh /app/railway-ai-cost-dashboard-entrypoint.sh --days 7 --heartbeat ai-cost-dashboard-weekly",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const demoUserCleanup = service("demo-user-cleanup", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      OPENSEARCH_CA_CERT: preserve(),
      OPENSEARCH_HOST: preserve(),
      OPENSEARCH_INDEX: preserve(),
      OPENSEARCH_PASSWORD: preserve(),
      OPENSEARCH_PORT: preserve(),
      OPENSEARCH_USERNAME: preserve(),
      OPENSEARCH_USE_SSL: preserve(),
      REDIS_URL: preserve(),
    },
    deploy: {
      cronSchedule: "0 3 * * *",
      startCommand: "python /app/manage.py cleanup_demo_users --heartbeat demo-user-cleanup",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const emailNotify = service("email-notify", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      API_PUBLIC_BASE_URL: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DEFAULT_FROM_EMAIL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      EMAIL_BACKEND: preserve(),
      EMAIL_NOTIFICATIONS_ENABLED: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      FRONTEND_BASE_URL: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      REDIS_URL: preserve(),
      RESEND_API_KEY: preserve(),
    },
    deploy: {
      cronSchedule: "*/5 * * * *",
      startCommand: "sh /app/railway-email-notify-entrypoint.sh --heartbeat email-notify",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const opensearchMaintenance = service("opensearch-maintenance", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      OPENSEARCH_CA_CERT: preserve(),
      OPENSEARCH_HOST: preserve(),
      OPENSEARCH_PASSWORD: preserve(),
      OPENSEARCH_PORT: preserve(),
      OPENSEARCH_USERNAME: preserve(),
      OPENSEARCH_USE_SSL: preserve(),
    },
    deploy: {
      cronSchedule: "0 5 * * *",
      startCommand: "sh /app/railway-opensearch-maintenance-entrypoint.sh --heartbeat opensearch-maintenance",
      restartPolicyType: "NEVER",
    },
  });
  const opensearchReindexer = service("opensearch-reindexer", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      AI_COST_METER: preserve(),
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      EMBEDDING_PROVIDER: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      GEMINI_LOCATION: preserve(),
      GEMINI_PROJECT: preserve(),
      GEMINI_SA_BASE64: preserve(),
      GEMINI_SERVICE_ACCOUNT_FILE: preserve(),
      GEMINI_USE_VERTEX: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      OPENAI_API_KEY: preserve(),
      OPENSEARCH_CA_CERT: preserve(),
      OPENSEARCH_HOST: preserve(),
      OPENSEARCH_INDEX: preserve(),
      OPENSEARCH_PASSWORD: preserve(),
      OPENSEARCH_PORT: preserve(),
      OPENSEARCH_USERNAME: preserve(),
      OPENSEARCH_USE_SSL: preserve(),
      SEARCH_EMBEDDING_BATCH_SIZE: preserve(),
      VERTEX_EMBEDDING_DIMENSIONS: preserve(),
      VERTEX_EMBEDDING_MODEL: preserve(),
    },
    deploy: {
      cronSchedule: "*/10 * * * *",
      startCommand: "sh /app/railway-reindexer-entrypoint.sh --since-minutes 11 --catch-up --heartbeat opensearch-reindexer",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const opsAlertTick = service("ops-alert-tick", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DEFAULT_FROM_EMAIL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      EMAIL_BACKEND: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      OPENSEARCH_CA_CERT: preserve(),
      OPENSEARCH_HOST: preserve(),
      // The capacity alert (MIP-2385) reads node stats and counts the alias: it logs in, and
      // needs the index name the other services use.
      OPENSEARCH_INDEX: preserve(),
      OPENSEARCH_PASSWORD: preserve(),
      OPENSEARCH_PORT: preserve(),
      OPENSEARCH_USERNAME: preserve(),
      OPENSEARCH_USE_SSL: preserve(),
      OPS_ALERT_EMAIL: preserve(),
      RESEND_API_KEY: preserve(),
    },
    deploy: {
      cronSchedule: "*/5 * * * *",
      startCommand: "sh /app/railway-ops-alert-tick-entrypoint.sh --heartbeat ops-alert-tick",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const reapStaleAgentRuns = service("reap-stale-agent-runs", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "0 * * * *",
      startCommand: "python /app/manage.py reap_stale_agent_runs --heartbeat reap-stale-agent-runs",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const reconcilePrDeliveries = service("reconcile-pr-deliveries", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      // The GitHub App (MIP-2088): the cron redelivers the App webhook's failures. All off, and
      // all empty, until the switch is turned on for this service too.
      GITHUB_APP_CLIENT_ID: preserve(),
      GITHUB_APP_CLIENT_SECRET: preserve(),
      GITHUB_APP_ENABLED: preserve(),
      GITHUB_APP_ID: preserve(),
      GITHUB_APP_PRIVATE_KEY: preserve(),
      GITHUB_APP_SLUG: preserve(),
      GITHUB_APP_WEBHOOK_SECRET: preserve(),
      GITHUB_OAUTH_CLIENT_ID: preserve(),
      GITHUB_OAUTH_CLIENT_SECRET: preserve(),
      GOOGLE_OAUTH_CLIENT_ID: preserve(),
      GOOGLE_OAUTH_CLIENT_SECRET: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      REDIS_URL: preserve(),
    },
    deploy: {
      cronSchedule: "*/15 * * * *",
      startCommand: "python /app/manage.py reconcile_pr_deliveries --heartbeat reconcile-pr-deliveries",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const reminderTick = service("reminder-tick", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      WEBPUSH_MEDIA_BASE_URL: preserve(),
      WEBPUSH_VAPID_ADMIN_EMAIL: preserve(),
      WEBPUSH_VAPID_PRIVATE_KEY: preserve(),
      WEBPUSH_VAPID_PUBLIC_KEY: preserve(),
    },
    deploy: {
      cronSchedule: "*/5 * * * *",
      startCommand: "sh /app/railway-reminder-tick-entrypoint.sh --drain-seconds 240 --heartbeat reminder-tick",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const scheduleTick = service("schedule-tick", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    // MIP-2460: this service runs the members' jobs, a skill run and a skill dashboard included, which
    // use the same agent loop and search as the web service. So it declares every variable a job reads;
    // each value is set on Railway as a reference to backend-django's (`${{backend-django.NAME}}`).
    // `origin/tests/test_railway_schedule_tick_env.py` fails when the code reads one that is not here.
    env: {
      // Every Django cron.
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      // The model: AI sentences (MIP-1293), AI dashboards and skill runs. The Vertex key file is written from
      // GEMINI_SA_BASE64 by railway-schedule-tick-entrypoint.sh. The CLAUDE_* and OPENAI_API_KEY values matter
      // only when LLM_PROVIDER says so.
      LLM_PROVIDER: preserve(),
      GEMINI_LOCATION: preserve(),
      GEMINI_MODEL: preserve(),
      GEMINI_PROJECT: preserve(),
      GEMINI_SA_BASE64: preserve(),
      GEMINI_SERVICE_ACCOUNT_FILE: preserve(),
      GEMINI_USE_VERTEX: preserve(),
      GEMINI_EXPLICIT_CACHE: preserve(),
      CLAUDE_API_KEY: preserve(),
      CLAUDE_MAX_TOKENS: preserve(),
      CLAUDE_MODEL: preserve(),
      OPENAI_API_KEY: preserve(),
      // The cost meter and the credit rule: the same as the web service, or a scheduled run is not charged
      // and not stopped at zero credits.
      AI_COST_METER: preserve(),
      AI_CREDITS_AUTHORITATIVE: preserve(),
      AI_CREDITS_SHADOW: preserve(),
      // The operator switches. Skills: off ("not set") fails every skill schedule (MIP-2045). AI sentences
      // (MIP-1210) and the AI summary of a dashboard page (MIP-2355) are off unless "true".
      GENOS_SKILLS: preserve(),
      GENOS_SKILLS_PUBLIC: preserve(),
      HTML_NOTES: preserve(),
      REPORT_AI_DASHBOARDS: preserve(),
      REPORT_AI_SENTENCES: preserve(),
      // The agent loop of a skill run: the same step budget, models and result sizes as in Spotlight.
      AGENT_EFFORT_LEVELS: preserve(),
      AGENT_TWO_TIER_LOOP: preserve(),
      LLM_LOG_USAGE_METADATA: preserve(),
      MCP_RESULT_LIMITS: preserve(),
      // A skill run's search: the index, and the embedder that turns the query into a vector.
      EMBEDDING_PROVIDER: preserve(),
      OPENAI_EMBEDDING_DIMENSIONS: preserve(),
      OPENAI_EMBEDDING_MODEL: preserve(),
      OPENSEARCH_CA_CERT: preserve(),
      OPENSEARCH_HOST: preserve(),
      OPENSEARCH_INDEX: preserve(),
      OPENSEARCH_PASSWORD: preserve(),
      OPENSEARCH_PORT: preserve(),
      OPENSEARCH_USERNAME: preserve(),
      OPENSEARCH_USE_SSL: preserve(),
      VERTEX_EMBEDDING_DIMENSIONS: preserve(),
      VERTEX_EMBEDDING_MODEL: preserve(),
      // The shared cache (presence for web push, demo AI limits, spend limits).
      REDIS_URL: preserve(),
      // The live update of a chat report and of the Inbox. Set both or neither: with the URL and no secret
      // the service does not boot (`_validate_transport`).
      SOCKETS_INTERNAL_SECRET: preserve(),
      SOCKETS_INTERNAL_URL: preserve(),
      // MIP-2355: a dashboard schedule saves its note from this service (`html_notes.save_page` pushes the page to
      // genos-collab). Without these two the note is saved to the database but an open note does not refresh.
      // The secret is the same value as on genos-collab and backend-django, and differs from JWT_SECRET_KEY.
      COLLAB_INTERNAL_SECRET: preserve(),
      COLLAB_INTERNAL_URL: preserve(),
      // The links a report posts into a chat.
      FRONTEND_BASE_URL: preserve(),
      // A skill run's pull request tools use the owner's GitHub App connection. Set all or none: with
      // GITHUB_APP_ENABLED on and any of the other six missing the service does not boot.
      GITHUB_APP_CLIENT_ID: preserve(),
      GITHUB_APP_CLIENT_SECRET: preserve(),
      GITHUB_APP_ENABLED: preserve(),
      GITHUB_APP_ID: preserve(),
      GITHUB_APP_PRIVATE_KEY: preserve(),
      GITHUB_APP_SLUG: preserve(),
      GITHUB_APP_WEBHOOK_SECRET: preserve(),
      // MIP-1333: "On your calendar" in a scheduled report refreshes the member's Google token with the OAuth client.
      GOOGLE_OAUTH_CLIENT_ID: preserve(),
      GOOGLE_OAUTH_CLIENT_SECRET: preserve(),
      // MIP-1374: a scheduled report's web push is sent from this service, and it is silently skipped without the VAPID keys
      // (`vapid_configured()` is false): the 17:00 recap of 2026-11-14 ran "ok" and no push arrived.
      WEBPUSH_MEDIA_BASE_URL: preserve(),
      WEBPUSH_VAPID_ADMIN_EMAIL: preserve(),
      WEBPUSH_VAPID_PRIVATE_KEY: preserve(),
      WEBPUSH_VAPID_PUBLIC_KEY: preserve(),
    },
    deploy: {
      cronSchedule: "*/5 * * * *",
      startCommand: "sh /app/railway-schedule-tick-entrypoint.sh --heartbeat schedule-tick",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const securityEventPrune = service("security-event-prune", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "30 3 * * *",
      startCommand: "python /app/manage.py prune_security_events --heartbeat security-event-prune",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const oauthClientPrune = service("oauth-client-prune", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "45 3 * * *",
      startCommand: "python /app/manage.py prune_oauth_clients --heartbeat oauth-client-prune",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const closedTeamPurge = service("closed-team-purge", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "30 4 * * *",
      startCommand: "python /app/manage.py purge_closed_teams --apply --yes --heartbeat closed-team-purge",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const scheduleReportPrune = service("schedule-report-prune", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "15 4 * * *",
      startCommand: "python /app/manage.py prune_schedule_reports --heartbeat schedule-report-prune",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const aiAppNoticePrune = service("ai-app-notice-prune", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      // MIP-1541: 04:30 UTC, between schedule-report-prune (04:15) and opensearch-maintenance (05:00).
      cronSchedule: "30 4 * * *",
      startCommand: "python /app/manage.py prune_ai_app_notices --apply --heartbeat ai-app-notice-prune",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const agentCallPrune = service("agent-call-prune", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      // MIP-2006: 04:40 UTC, after ai-app-notice-prune (04:30) and before opensearch-maintenance (05:00).
      cronSchedule: "40 4 * * *",
      startCommand: "python /app/manage.py prune_agent_calls --apply --heartbeat agent-call-prune",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });
  const webhookDeliver = service("webhook-deliver", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      cronSchedule: "*/5 * * * *",
      startCommand: "sh /app/railway-webhook-deliver-entrypoint.sh --drain-seconds 240 --heartbeat webhook-deliver",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });

  const retentionPurge = service("retention-purge", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
      // The approved periods (MIP-728, MIP-1786): agent runs 180, email outbox 90, high-volume security rows 90.
      // Ken sets the values in Railway; a period left unset keeps the rows for ever.
      RETENTION_AGENT_RUNS_DAYS: preserve(),
      RETENTION_EMAIL_NOTIFICATION_EVENTS_DAYS: preserve(),
      RETENTION_SECURITY_EVENTS_HIGH_VOLUME_DAYS: preserve(),
      // MIP-2252, approved by Ken 2026-11-24 (same rule: unset = keep for ever):
      // agent step output 30, agent sessions 180, webhook deliveries 30, note versions 90,
      // task history bodies 30, task history 365, security event address and browser 90.
      RETENTION_AGENT_STEP_RESULTS_DAYS: preserve(),
      RETENTION_AGENT_SESSIONS_DAYS: preserve(),
      RETENTION_WEBHOOK_DELIVERIES_DAYS: preserve(),
      RETENTION_NOTE_VERSIONS_DAYS: preserve(),
      RETENTION_TASK_ACTIVITY_BODIES_DAYS: preserve(),
      RETENTION_TASK_ACTIVITIES_DAYS: preserve(),
      RETENTION_SECURITY_EVENT_NETWORK_DAYS: preserve(),
    },
    deploy: {
      // MIP-2251: 04:00 UTC. Moved here from railway-retention-purge.toml (a service created after 2026-08-28 cannot use a toml).
      cronSchedule: "0 4 * * *",
      startCommand: "python /app/manage.py retention_purge --apply --heartbeat retention-purge",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });

  const housekeepingPrune = service("housekeeping-prune", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      // MIP-2251 (MIP-1787): 03:10 UTC. Dead tokens, revoked keys and connections, done invites, idle push subscriptions, resolved alerts.
      cronSchedule: "10 3 * * *",
      startCommand: "python /app/manage.py prune_housekeeping --apply --heartbeat housekeeping-prune",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });

  const tombstonePrune = service("tombstone-prune", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      // MIP-2251: 03:20 UTC. Empty tombstone rows of deleted messages.
      cronSchedule: "20 3 * * *",
      startCommand: "python /app/manage.py prune_old_tombstones --apply --heartbeat tombstone-prune",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });

  const memberMemorySweep = service("member-memory-sweep", {
    source: github("genos-tech/genos-api", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      ALLOWED_HOSTS: preserve(),
      BLIND_INDEX_KEYS: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      FIELD_ENCRYPTION_KEYS: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_FORMAT: preserve(),
    },
    deploy: {
      // MIP-2251: 03:25 UTC. MemberEvent (180 d) and MemberItemDay (365 d) of every member.
      cronSchedule: "25 3 * * *",
      startCommand: "python /app/manage.py prune_member_memory --apply --heartbeat member-memory-sweep",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });

  // genos-collab's orphan sweep (MIP-1823), a cron like the others here. Built from genos-collab,
  // not genos-api. DRY RUN unless the service variable COLLAB_SWEEP_DELETE is 1 or true (set by
  // hand in Railway, never in this file). Its PG_* reference backend-collab's own PG_* variables
  // (one database: Postgres), so no secret is written here. Runbook: genos-docs operations/COLLAB_ORPHAN_SWEEP.md.
  const collabOrphanSweep = service("collab-orphan-sweep", {
    source: github("genos-tech/genos-collab", { branch: "main", checkSuites: false, rootDirectory: "/" }),
    build: { buildEnvironment: "V3", builder: "NIXPACKS", buildCommand: "npm ci --omit=dev" },
    replicas: { "asia-southeast1-eqsg3a": 1 },
    env: {
      COLLAB_SWEEP_DELETE: preserve(),
      DJANGO_BASE_URL: preserve(),
      EXTERNAL_CRON_HEARTBEAT_TOKEN: preserve(),
      LOG_FORMAT: preserve(),
      PG_DATABASE: preserve(),
      PG_HOST: preserve(),
      PG_PASSWORD: preserve(),
      PG_PORT: preserve(),
      PG_USER: preserve(),
    },
    deploy: {
      cronSchedule: "20 4 * * *",
      startCommand: "node cron-sweep-orphans.js",
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.5, memoryBytes: 500000000 } },
    },
  });

  return project("genos", { resources: [agentCallPrune, agentJudgeSampler, aiAppNoticePrune, aiCostDashboardDaily, aiCostDashboardMonthly, aiCostDashboardWeekly, closedTeamPurge, collabOrphanSweep, demoUserCleanup, emailNotify, housekeepingPrune, memberMemorySweep, oauthClientPrune, opensearchMaintenance, opensearchReindexer, opsAlertTick, reapStaleAgentRuns, reconcilePrDeliveries, reminderTick, retentionPurge, scheduleReportPrune, scheduleTick, securityEventPrune, tombstonePrune, webhookDeliver] });
});
