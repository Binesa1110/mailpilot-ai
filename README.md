# MailPilot AI

MailPilot helps users draft and review emails, then explicitly approve an individual message through their connected Gmail account. **Manual approval is required by default. Automatic sending remains off until the authenticated user separately opts in; the AI model cannot grant or alter that permission.**

## Requirements

- Python 3.11+
- Node.js 18+
- An OpenAI API key
- A Google Cloud OAuth web client with the Gmail API enabled

## Configure

Copy `.env.example` to `backend/.env` and set `OPENAI_API_KEY`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET`.

```powershell
Copy-Item .env.example backend\.env
```

In Google Cloud:

1. Enable the Gmail API for the project.
2. Configure the OAuth consent screen and add your account as a test user if the app is in testing.
3. Create a **Web application** OAuth client.
4. Add `http://localhost:8000/api/auth/google/callback` as an authorized redirect URI.
5. Keep the requested Gmail permission limited to `https://www.googleapis.com/auth/gmail.send`.

Create a Fernet key and paste it into `MAILPILOT_TOKEN_ENCRYPTION_KEY` in `backend/.env`. This key encrypts the stored Google OAuth credentials; keep it private and back it up securely because it cannot be recovered from the database.

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The local SQLite database defaults to `backend/data/mailpilot.sqlite3`. Override its location with `MAILPILOT_DB_PATH` or configure `DATABASE_URL` for PostgreSQL. Schema changes are versioned in `backend/alembic/versions`; apply them explicitly before starting the API. Normal application startup checks connectivity and migration state but does not run schema changes. The database and local environment file are ignored by Git.

The PostgreSQL engine currently allows up to 5 pooled connections plus 5 overflow connections per API process. Set worker count with the managed database's connection limit in mind; pool sizing should be reviewed before production.

## Run locally

In the first terminal, from the repository root. Set `DATABASE_URL` to use PostgreSQL; when it is unset, local development uses the SQLite path in `MAILPILOT_DB_PATH`.

```powershell
py -3.11 -m venv backend\.venv
backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
alembic -c backend\alembic.ini upgrade head
uvicorn app.main:app --app-dir backend --reload
```

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL (normally http://localhost:5173) and sign in with Google. Sign-in requests only the `openid`, `email`, and `profile` identity scopes, so users can draft and review messages without connecting Gmail. Connect Gmail separately from the signed-in interface only when you want to send messages. The API is available at http://localhost:8000.

## Hosted HTTPS demo (configuration only)

The repository includes a single-service Docker build and a Render Blueprint in `render.yaml`. The service serves the Vite production build and FastAPI API from one HTTPS origin; `/api/*` remains routed to FastAPI and other paths use the React SPA fallback. The Vite proxy remains for local development.

1. Create a private GitHub repository and push the reviewed code yourself; no remote or deployment is configured by this project yet.
2. In Render, create a managed PostgreSQL database and a Docker web service from `render.yaml`. This may create paid resources; review the provider's current pricing before approving creation.
3. Configure the `sync: false` variables in the host's secret manager/dashboard. Required names are `DATABASE_URL`, `OPENAI_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `MAILPILOT_TOKEN_ENCRYPTION_KEY`, and `MAILPILOT_FRONTEND_URL`. `MAILPILOT_ENV` must be `production`; `MAILPILOT_COOKIE_SECURE` is `true`. `OPENAI_MODEL` and `MAILPILOT_ADMIN_OWNER_IDS` are optional; leave the admin list empty unless an operator is deliberately designated.
4. Set `MAILPILOT_FRONTEND_URL` to the exact public HTTPS origin and `GOOGLE_REDIRECT_URI` to that same origin plus `/api/auth/google/callback`. Register that exact callback in the Google OAuth client. Complete the Google consent-screen setup and required verification before allowing public Gmail connections.
5. Render's pre-deploy command explicitly runs `alembic -c backend/alembic.ini upgrade head` before the web process starts. Application startup does not create schemas or run migrations. Check `/api/health` after deploy; the app only starts if production configuration is valid, PostgreSQL is reachable, and the migration version table exists.
6. Confirm the site and API share HTTPS origin, cookies are Secure/HttpOnly/SameSite=Lax, and cross-origin mutations are rejected. Verify AI drafting and sign-in only after configuring the provider credentials. Keep Gmail sending disabled unless a user connects Gmail and manually approves each message. Auto-send stays disabled by default and requires the separate explicit per-account consent flow.

Do not set a production `DATABASE_URL` to a local SQLite file. This demo configuration does not import the local SQLite database or its users. Before hosting real data, configure managed-database backups and point-in-time recovery, protect the Fernet encryption key separately, and rehearse restoring both database and key. For rollback, application rollback is safe only when the prior code supports the migrated schema. Alembic downgrades are intentionally unsupported for the initial schema; use a tested forward migration or restore a verified database snapshot. Do not revert to an old SQLite snapshot after production writes without a deliberate data-reconciliation plan.

## Review, approve, and send

1. Sign in with Google. To send a message, choose **Connect Gmail** and grant the `gmail.send` permission. This connection is linked to the already authenticated MailPilot account.
2. Generate an email. MailPilot saves it with its review notes and risk flags.
3. Verify the exact recipient email address, subject, and editable final body.
4. Select **Approve this exact email**. The server records approval against a SHA-256 digest of recipient, subject, and body.
5. Select **Send approved email to …** to make a one-message Gmail API call.

Any edit requires a new approval. The API accepts a saved draft ID for sending; message content and tool instructions are not accepted in the send request. The server checks authenticated ownership, current Gmail send scope, recipient validity, size, approval digest, and send state before calling Gmail. Messages are MIME-encoded by the backend.

## Optional automatic sending

Each account starts in **Approval required** mode with automatic sending disabled and no allowed recipients or categories. To opt in, connect Gmail, open **Configure auto-send**, enter an exact email-address allowlist and eligible categories, choose a daily limit (1–10), acknowledge that eligible drafts can send without another approval, and type `ENABLE AUTO-SEND`. This setting is persisted per account. **Disable auto-send now** revokes it; the global emergency stop disables automatic sending for all accounts. The global stop is intentionally one-way in the UI and requires an operator to clear it.

Disconnecting Gmail atomically removes the locally stored connection and clears that account's automatic-send permission, recipient allowlist, and category restrictions. Reconnecting Gmail does not restore them; the user must explicitly opt in again. MailPilot does not currently request revocation of the Google-side OAuth grant. To revoke that grant, use the account's Google security settings.

Automatic sending applies only to newly generated drafts. A deterministic backend policy must pass each draft; AI review is not authorization. The policy requires a new draft, enabled account permission, live Gmail send scope, exact allowlisted recipient, allowed and recognized category, remaining daily allowance, valid message size, and a complete review with no notes or risk flags. It blocks detected placeholders, sensitive data/numbers, high-risk legal/financial/medical language, potential unsupported claims, multiple recipients, and uncertain classifications. Complaint and apology categories always require manual review. Anything blocked remains a saved draft for the normal manual review/approval workflow. The policy is deliberately conservative, but its text heuristics cannot guarantee detection of every risky or unsupported statement; keep recipient/category allowlists narrow and disable auto-send when unsure.

Auto-send decisions are re-evaluated against the current draft version, account setting, global kill switch, daily count, and Gmail scope immediately before the provider call. PostgreSQL uses session-level advisory locks (without an open database transaction) to coordinate sends with account revocation and the global kill switch across workers. A provider call already in progress cannot be recalled; a revocation waits for that in-flight operation and prevents later sends. Local SQLite uses a same-process lock and is for single-worker development only; it is not safe for multi-process hosting. PostgreSQL cross-worker behavior is not yet verified until the gated PostgreSQL integration tests pass against a real test instance. A unique generation `Idempotency-Key` prevents retried generation requests from creating and sending the same request twice. Daily auto-send limits and a separate 50-generation-per-account daily cap are persistent. Each audit event stores the authorization type and policy reason codes, never email bodies or credentials.

Manual and automatic sends have persistent idempotency keys and audit events. A duplicate request with the same key returns the saved result rather than sending again. If Gmail's response is uncertain, MailPilot marks the draft **Outcome uncertain**, blocks retries, and directs the user to check Gmail's Sent folder. A definite provider rejection is recorded as a failure. The interface shows saved drafts, authorization mode, policy outcomes, and any returned Gmail message ID.

## Security notes

- OpenAI and Google credentials remain on the backend. Google OAuth credentials are encrypted in the database with `MAILPILOT_TOKEN_ENCRYPTION_KEY`.
- Google accounts are keyed by Google's verified stable `sub` identifier, not the mutable email address. Email remains a display profile value. Browser authentication uses a hashed server-side session and HTTP-only, SameSite=Lax cookie. Set `MAILPILOT_ENV=production` in production; secure cookies are forced on in production irrespective of the local-development override. Production CORS and mutation Origin checks use only `MAILPILOT_FRONTEND_URL`; loopback origins are enabled only in development. Terminate HTTPS and configure the frontend and OAuth callback URLs for the deployment.
- Identity sign-in requests only `openid`, `email`, and `profile`; it does not require Gmail access. The separately initiated Gmail connection requests `gmail.send` plus identity scopes to identify the connected Google account; it does not read the mailbox.
- Gmail OAuth state is one-time, expires, and is bound server-side to the connection intent, authenticated owner, and initiating live session. Gmail credentials are only stored for that owner after the callback passes those checks.
- Automatic sending is opt-in and policy-limited; manual approval remains the default. The AI model has no permission-setting or send-tool capability.
- Disconnecting Gmail clears MailPilot's local credentials and auto-send consent but does not revoke the Google OAuth grant. Reconnecting requires a new explicit auto-send opt-in.
- The global kill-switch endpoint is restricted to `MAILPILOT_ADMIN_OWNER_IDS`, a comma-separated list of stable Google owner IDs (`google:<sub>`). Leave it unset to disable the endpoint for all users.
- Set `MAILPILOT_AUTO_SEND_KILL_SWITCH=true` before startup to initialize the global kill switch active. Once activated in-app, it remains persistent. To clear it as an operator, stop the service and run:

  ```powershell
  python -c "import sqlite3; c=sqlite3.connect(r'backend\data\mailpilot.sqlite3'); c.execute('UPDATE system_settings SET setting_value=? WHERE setting_key=?', ('0', 'auto_send_kill_switch')); c.commit(); c.close()"
  ```

- Treat the database and encryption key as sensitive production data. Back them up with access controls and encryption at rest.

## Tests and production build

Tests use fake AI and Gmail services; they do not require or contact real accounts.

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
cd frontend
npm run build
```

The API tests cover input validation, AI errors, health, authentication/ownership, draft approval, modified content, Gmail connection and permission checks, successful mocked sends, provider failures, uncertain outcomes, manual and automatic send idempotency, concurrent duplicate requests, auto-send opt-in/revocation/kill switch, policy denials, rate limits, and audit authorization records. Tests do not contact real Google accounts.

PostgreSQL-specific tests are skipped unless pointed at a dedicated disposable test database. Set `MAILPILOT_TEST_POSTGRES_URL` locally (do not commit it), then opt in and run:

```powershell
$env:MAILPILOT_ALLOW_POSTGRES_TESTS = "1"
python -m pytest backend\tests\test_postgres_integration.py -q
```

These tests run Alembic against the configured PostgreSQL database and write uniquely named test rows. Do not point them at production or a database containing data you need to preserve.

## API overview

- `GET /api/health`
- `GET /api/auth/google/sign-in` (legacy alias: `/api/auth/google/start`) and `GET /api/auth/google/callback` — identity-only sign-in
- `GET /api/auth/google/connect-gmail` — authenticated, optional Gmail authorization linked to the current MailPilot session
- `GET /api/auth/session`, `POST /api/auth/logout`, `DELETE /api/auth/gmail`
- `GET /api/settings/auto-send`, `POST /api/settings/auto-send/enable`, `POST /api/settings/auto-send/disable`, `POST /api/settings/auto-send/kill-switch`
- `POST /api/emails/generate` — authenticated; requires an `Idempotency-Key`, generates and reviews a draft, then applies the server-owned auto-send policy
- `GET /api/drafts` and `GET /api/audit` — authenticated, owner-scoped
- `POST /api/drafts/{draft_id}/approve` — saves final editable content and exact-content approval
- `POST /api/drafts/{draft_id}/send` — authenticated; saved draft ID plus `Idempotency-Key` header only
