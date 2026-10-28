# MailPilot AI

MailPilot helps you draft and review emails, then lets you explicitly approve and send an individual message through Gmail. Drafts, account preferences, generation idempotency records, and send history are saved locally in SQLite. **Automatic sending is off by default. It can only be enabled by an explicit authenticated user opt-in; the AI model cannot grant or alter that permission.**

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

The SQLite database defaults to `backend/data/mailpilot.sqlite3`. Override its location with `MAILPILOT_DB_PATH` if needed. The database and local environment file are ignored by Git.

## Run locally

In the first terminal, from the repository root:

```powershell
py -3.11 -m venv backend\.venv
backend\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
uvicorn app.main:app --app-dir backend --reload
```

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL (normally http://localhost:5173), sign in with Google, and connect Gmail. Locally, sign-in also establishes the authenticated account session. The API is available at http://localhost:8000.

## Review, approve, and send

1. Sign in with Google and grant the `gmail.send` permission.
2. Generate an email. MailPilot saves it with its review notes and risk flags.
3. Verify the exact recipient email address, subject, and editable final body.
4. Select **Approve this exact email**. The server records approval against a SHA-256 digest of recipient, subject, and body.
5. Select **Send approved email to …** to make a one-message Gmail API call.

Any edit requires a new approval. The API accepts a saved draft ID for sending; message content and tool instructions are not accepted in the send request. The server checks authenticated ownership, current Gmail send scope, recipient validity, size, approval digest, and send state before calling Gmail. Messages are MIME-encoded by the backend.

## Optional automatic sending

Each account starts in **Approval required** mode with automatic sending disabled and no allowed recipients or categories. To opt in, connect Gmail, open **Configure auto-send**, enter an exact email-address allowlist and eligible categories, choose a daily limit (1–10), acknowledge that eligible drafts can send without another approval, and type `ENABLE AUTO-SEND`. This setting is persisted per account. **Disable auto-send now** revokes it; the global emergency stop disables automatic sending for all accounts. The global stop is intentionally one-way in the UI and requires an operator to clear it.

Automatic sending applies only to newly generated drafts. A deterministic backend policy must pass each draft; AI review is not authorization. The policy requires a new draft, enabled account permission, live Gmail send scope, exact allowlisted recipient, allowed and recognized category, remaining daily allowance, valid message size, and a complete review with no notes or risk flags. It blocks detected placeholders, sensitive data/numbers, high-risk legal/financial/medical language, potential unsupported claims, multiple recipients, and uncertain classifications. Complaint and apology categories always require manual review. Anything blocked remains a saved draft for the normal manual review/approval workflow. The policy is deliberately conservative, but its text heuristics cannot guarantee detection of every risky or unsupported statement; keep recipient/category allowlists narrow and disable auto-send when unsure.

Auto-send decisions are re-evaluated transactionally against the current draft version, account setting, global kill switch, daily count, and Gmail scope immediately before the provider call. The SQLite write lock is held through the provider call so concurrent revocation/send attempts serialize. A provider call already in progress cannot be recalled; a revocation request waits for that in-flight operation and prevents later sends. A unique generation `Idempotency-Key` prevents retried generation requests from creating and sending the same request twice. Daily auto-send limits and a separate 50-generation-per-account daily cap are persistent. Each audit event stores the authorization type and policy reason codes, never email bodies or credentials.

Manual and automatic sends have persistent idempotency keys and audit events. A duplicate request with the same key returns the saved result rather than sending again. If Gmail's response is uncertain, MailPilot marks the draft **Outcome uncertain**, blocks retries, and directs the user to check Gmail's Sent folder. A definite provider rejection is recorded as a failure. The interface shows saved drafts, authorization mode, policy outcomes, and any returned Gmail message ID.

## Security notes

- OpenAI and Google credentials remain on the backend. Google OAuth credentials are encrypted in SQLite with `MAILPILOT_TOKEN_ENCRYPTION_KEY`.
- Google accounts are keyed by Google's verified stable `sub` identifier, not the mutable email address. Email remains a display profile value. Browser authentication uses a hashed server-side session and HTTP-only, SameSite=Lax cookie. Set `MAILPILOT_ENV=production` in production; secure cookies are forced on in production irrespective of the local-development override. Production CORS and mutation Origin checks use only `MAILPILOT_FRONTEND_URL`; loopback origins are enabled only in development. Terminate HTTPS and configure the frontend and OAuth callback URLs for the deployment.
- The app requests only Gmail's send scope; it does not read the user's mailbox.
- Automatic sending is opt-in and policy-limited; manual approval remains the default. The AI model has no permission-setting or send-tool capability.
- The global kill-switch endpoint is restricted to `MAILPILOT_ADMIN_OWNER_IDS`, a comma-separated list of stable Google owner IDs (`google:<sub>`). Leave it unset to disable the endpoint for all users.
- Set `MAILPILOT_AUTO_SEND_KILL_SWITCH=true` before startup to initialize the global kill switch active. Once activated in-app, it remains persistent. To clear it as an operator, stop the service and run:

  ```powershell
  python -c "import sqlite3; c=sqlite3.connect(r'backend\data\mailpilot.sqlite3'); c.execute('UPDATE system_settings SET setting_value=? WHERE setting_key=?', ('0', 'auto_send_kill_switch')); c.commit(); c.close()"
  ```

- Treat the SQLite database and encryption key as sensitive production data. Back them up with access controls and encryption at rest.

## Tests and production build

Tests use fake AI and Gmail services; they do not require or contact real accounts.

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
cd frontend
npm run build
```

The API tests cover input validation, AI errors, health, authentication/ownership, draft approval, modified content, Gmail connection and permission checks, successful mocked sends, provider failures, uncertain outcomes, manual and automatic send idempotency, concurrent duplicate requests, auto-send opt-in/revocation/kill switch, policy denials, rate limits, and audit authorization records. Tests do not contact real Google accounts.

## API overview

- `GET /api/health`
- `GET /api/auth/google/start` and `GET /api/auth/google/callback`
- `GET /api/auth/session`, `POST /api/auth/logout`, `DELETE /api/auth/gmail`
- `GET /api/settings/auto-send`, `POST /api/settings/auto-send/enable`, `POST /api/settings/auto-send/disable`, `POST /api/settings/auto-send/kill-switch`
- `POST /api/emails/generate` — authenticated; requires an `Idempotency-Key`, generates and reviews a draft, then applies the server-owned auto-send policy
- `GET /api/drafts` and `GET /api/audit` — authenticated, owner-scoped
- `POST /api/drafts/{draft_id}/approve` — saves final editable content and exact-content approval
- `POST /api/drafts/{draft_id}/send` — authenticated; saved draft ID plus `Idempotency-Key` header only
