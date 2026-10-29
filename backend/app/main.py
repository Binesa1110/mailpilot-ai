import hashlib
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.engine import make_url
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, ValidationError, field_validator

from .gmail import GMAIL_SEND_SCOPE, GmailSendError, GmailSender, create_mime_message
from .database import database_url
from .frontend import SpaStaticFiles, frontend_build_directory
from .policy import SUPPORTED_CATEGORIES, evaluate_auto_send, normalize_email_address
from .storage import (
    AutoSendKillSwitchActiveError,
    GmailConnectionRequiredError,
    Store,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

SESSION_COOKIE = "mailpilot_session"
OAUTH_COOKIE = "mailpilot_oauth_state"
GMAIL_OAUTH_COOKIE = "mailpilot_gmail_oauth_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 7
OAUTH_STATE_MAX_AGE = 600
IDENTITY_SCOPES = ["openid", "email", "profile"]
GMAIL_CONNECTION_SCOPES = ["openid", "email", GMAIL_SEND_SCOPE]
FRONTEND_URL = os.getenv("MAILPILOT_FRONTEND_URL", "http://localhost:5173")
APP_ENV = os.getenv("MAILPILOT_ENV", "production").lower()
OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/google/callback"
)


def _secure_cookies() -> bool:
    configured = os.getenv("MAILPILOT_COOKIE_SECURE")
    if APP_ENV == "production":
        return True
    return configured is not None and configured.lower() == "true"


def _owner_id_from_identity(identity: dict) -> str:
    subject = identity.get("sub")
    email = identity.get("email")
    if not isinstance(subject, str) or not subject or not isinstance(email, str):
        raise ValueError("Google identity is missing a stable subject or email.")
    if identity.get("email_verified") is not True:
        raise ValueError("Google email address is not verified.")
    return f"google:{subject}"


def _configured_admins() -> set[str]:
    return {
        value.strip()
        for value in os.getenv("MAILPILOT_ADMIN_OWNER_IDS", "").split(",")
        if value.strip()
    }


def _origin_allowed(origin: str | None) -> bool:
    allowed = {FRONTEND_URL.rstrip("/")}
    if APP_ENV != "production":
        allowed.update({"http://localhost:5173", "http://127.0.0.1:5173"})
    return bool(origin and origin.rstrip("/") in allowed)


def _production_configuration_errors() -> list[str]:
    required = (
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
        "MAILPILOT_TOKEN_ENCRYPTION_KEY",
        "MAILPILOT_FRONTEND_URL",
    )
    errors = [f"{name} is required in production." for name in required if not os.getenv(name)]

    frontend = os.getenv("MAILPILOT_FRONTEND_URL", "")
    callback = os.getenv("GOOGLE_REDIRECT_URI", "")
    try:
        frontend_parts = urlsplit(frontend)
        if (
            frontend_parts.scheme != "https"
            or not frontend_parts.hostname
            or frontend_parts.path not in ("", "/")
            or frontend_parts.query
            or frontend_parts.fragment
            or frontend_parts.username
            or frontend_parts.password
        ):
            errors.append("MAILPILOT_FRONTEND_URL must be an HTTPS origin without a path.")
    except ValueError:
        frontend_parts = None
        errors.append("MAILPILOT_FRONTEND_URL must be a valid HTTPS origin.")

    try:
        callback_parts = urlsplit(callback)
        if (
            callback_parts.scheme != "https"
            or not callback_parts.hostname
            or callback_parts.path != "/api/auth/google/callback"
            or callback_parts.query
            or callback_parts.fragment
            or callback_parts.username
            or callback_parts.password
        ):
            errors.append("GOOGLE_REDIRECT_URI must be the HTTPS Google OAuth callback URL.")
        elif frontend_parts and callback_parts.netloc != frontend_parts.netloc:
            errors.append("GOOGLE_REDIRECT_URI must use the MAILPILOT_FRONTEND_URL origin.")
    except ValueError:
        errors.append("GOOGLE_REDIRECT_URI must be a valid HTTPS callback URL.")

    database = os.getenv("DATABASE_URL", "")
    try:
        database_parts = make_url(database)
        if (
            database_parts.drivername not in {"postgresql", "postgresql+psycopg"}
            or not database_parts.host
            or not database_parts.database
        ):
            errors.append("DATABASE_URL must identify the managed PostgreSQL database.")
    except Exception:
        errors.append("DATABASE_URL must be a valid PostgreSQL connection URL.")

    encryption_key = os.getenv("MAILPILOT_TOKEN_ENCRYPTION_KEY", "")
    if encryption_key:
        try:
            from cryptography.fernet import Fernet

            Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            errors.append("MAILPILOT_TOKEN_ENCRYPTION_KEY must be a valid Fernet key.")

    if not frontend_build_directory().is_dir() or not (frontend_build_directory() / "index.html").is_file():
        errors.append("Build the frontend so frontend/dist/index.html exists.")
    return errors


class EmailRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    recipient: str = Field(default="", max_length=200)
    purpose: str = Field(min_length=1, max_length=500)
    context: str = Field(min_length=1, max_length=5000)
    category: str = Field(min_length=1, max_length=60)
    tone: str = Field(min_length=1, max_length=60)
    language: str = Field(min_length=1, max_length=60)
    signature: str = Field(default="", max_length=300)

    @field_validator("purpose", "context", "category", "tone", "language")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value:
            raise ValueError("This field must not be blank.")
        return value


class EmailDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)

    @field_validator("subject", "body")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value:
            raise ValueError("Email content must not be blank.")
        return value


class EmailReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: list[str] = Field(default_factory=list, max_length=10)
    risk_flags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("notes", "risk_flags")
    @classmethod
    def validate_review_items(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in values):
            raise ValueError("Review items must contain 1–500 characters.")
        return [item.strip() for item in values]


class GenerateEmailResponse(BaseModel):
    id: str
    recipient: str
    subject: str
    body: str
    review: EmailReview
    status: str
    created_at: str
    version: int
    send_authorization: str | None = None
    policy_decision: dict | None = None
    message_id: str | None = None


class EnableAutoSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_phrase: Literal["ENABLE AUTO-SEND"]
    consequences_acknowledged: bool
    recipient_allowlist: list[EmailStr] = Field(min_length=1, max_length=20)
    categories: list[str] = Field(min_length=1, max_length=len(SUPPORTED_CATEGORIES))
    daily_limit: int = Field(default=5, ge=1, le=10)

    @field_validator("consequences_acknowledged")
    @classmethod
    def require_consequence_acknowledgement(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Confirm that eligible emails may be sent without per-message approval.")
        return value

    @field_validator("recipient_allowlist")
    @classmethod
    def normalize_recipients(cls, recipients: list[EmailStr]) -> list[str]:
        normalized = list(
            dict.fromkeys(normalize_email_address(str(recipient)) for recipient in recipients)
        )
        if not normalized:
            raise ValueError("At least one allowlisted recipient is required.")
        return normalized

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, categories: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(category.strip().casefold() for category in categories))
        if not normalized or any(category not in SUPPORTED_CATEGORIES for category in normalized):
            raise ValueError("Choose only supported email categories.")
        return normalized


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)

    @field_validator("recipient")
    @classmethod
    def validate_recipient_without_normalizing(cls, value: str) -> str:
        if value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError("Enter a valid recipient email address.")
        try:
            TypeAdapter(EmailStr).validate_python(value)
        except ValidationError as exc:
            raise ValueError("Enter a valid recipient email address.") from exc
        return value

    @field_validator("subject", "body")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Email content must not be blank.")
        return value


class AIServiceError(Exception):
    """Raised when an upstream AI response cannot be used safely."""


class EmailAIService:
    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=api_key) if api_key else None
        self.model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    def _request_json(self, system_prompt: str, user_prompt: str) -> dict:
        if self.client is None:
            raise AIServiceError("AI generation is not configured. Add OPENAI_API_KEY to backend/.env.")
        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=system_prompt,
                input=user_prompt,
                text={"format": {"type": "json_object"}},
            )
        except OpenAIError as exc:
            raise AIServiceError("The AI service is temporarily unavailable. Please try again.") from exc
        output = getattr(response, "output_text", None)
        if not isinstance(output, str):
            raise AIServiceError("The AI service returned an invalid response.")
        if not output.strip():
            raise AIServiceError("The AI service returned an empty response.")
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            raise AIServiceError("The AI service returned malformed JSON.") from exc
        if not isinstance(parsed, dict):
            raise AIServiceError("The AI service returned an invalid response.")
        return parsed

    def generate_draft(self, request: EmailRequest) -> EmailDraft:
        payload = self._request_json(
            "Write a clear, natural email draft based on the user's request. "
            "Do not invent facts or commitments. Return only a JSON object with string fields "
            '"subject" and "body". Do not add a signature unless one is provided.',
            json.dumps(request.model_dump(), ensure_ascii=False),
        )
        try:
            return EmailDraft.model_validate(payload)
        except ValidationError as exc:
            raise AIServiceError("The AI service returned an invalid or oversized email draft.") from exc

    def review_draft(self, request: EmailRequest, draft: EmailDraft) -> EmailReview:
        payload = self._request_json(
            "Review this email for clarity, tone, unsupported claims, privacy concerns, and risky "
            "or overly committal language. Do not rewrite it. Return only a JSON object with "
            '"notes" (an array of concise helpful strings) and "risk_flags" (an array of material '
            "risks, empty if none). Never claim a risk is absent if uncertain.",
            json.dumps(
                {"request": request.model_dump(), "draft": draft.model_dump()},
                ensure_ascii=False,
            ),
        )
        try:
            return EmailReview.model_validate(payload)
        except ValidationError as exc:
            raise AIServiceError("The AI service returned invalid review notes.") from exc


def _session_hash(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()


def _content_hash(recipient: str, subject: str, body: str, version: int) -> str:
    content = json.dumps(
        {"recipient": recipient, "subject": subject, "body": body, "version": version},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _oauth_flow(state: str | None = None, intent: str = "sign_in") -> Flow:
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )
    return Flow.from_client_config(
        {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=IDENTITY_SCOPES if intent == "sign_in" else GMAIL_CONNECTION_SCOPES,
        state=state,
        redirect_uri=OAUTH_REDIRECT_URI,
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    store = None
    try:
        if APP_ENV == "production":
            configuration_errors = _production_configuration_errors()
            if configuration_errors:
                raise RuntimeError("; ".join(configuration_errors))
        store = Store(database_url())
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception:
        if store is not None:
            store.dispose()
        raise RuntimeError(
            "The database is unavailable or migrations are missing. Check DATABASE_URL and run "
            "`alembic -c backend/alembic.ini upgrade head` before starting MailPilot."
        ) from None
    application.state.store = store
    try:
        yield
    finally:
        store.dispose()


app = FastAPI(title="MailPilot AI API", version="2.0.0", lifespan=lifespan)
allowed_origins = {FRONTEND_URL}
if APP_ENV != "production":
    allowed_origins.update({"http://localhost:5173", "http://127.0.0.1:5173"})
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)
app.state.ai_service = EmailAIService()
app.state.gmail_sender = GmailSender()


@app.middleware("http")
async def require_same_origin_for_mutations(request: Request, call_next):
    if (
        request.url.path.startswith("/api/")
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and not _origin_allowed(request.headers.get("origin"))
    ):
        return Response("Cross-origin request rejected.", status_code=403)
    return await call_next(request)


def authenticated_owner(
    request: Request,
    mailpilot_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    if not mailpilot_session:
        raise HTTPException(status_code=401, detail="Sign in with Google to continue.")
    owner_id = request.app.state.store.session_owner(_session_hash(mailpilot_session))
    if not owner_id:
        raise HTTPException(status_code=401, detail="Your session expired. Sign in again.")
    return owner_id


def _make_email_response(draft: dict) -> GenerateEmailResponse:
    return GenerateEmailResponse(
        id=draft["id"],
        recipient=draft["recipient"],
        subject=draft["subject"],
        body=draft["body"],
        review=EmailReview.model_validate(draft["review"]),
        status=draft["status"],
        created_at=draft["created_at"],
        version=draft["version"],
        send_authorization=draft.get("send_authorization"),
        policy_decision=draft.get("policy_decision"),
        message_id=draft.get("provider_message_id"),
    )


def _attempt_auto_send(request: Request, owner_id: str, draft: dict) -> dict:
    store = request.app.state.store
    settings = store.get_account_settings(owner_id)
    connection = store.get_connection(owner_id)
    connected = bool(connection and GMAIL_SEND_SCOPE in connection["scopes"])
    decision = evaluate_auto_send(
        draft,
        settings,
        store.auto_send_count_today(owner_id),
        connected,
        store.kill_switch_enabled(),
    ).as_dict()
    decision["draft_hash"] = store._content_hash(
        draft["recipient"], draft["subject"], draft["body"], draft["version"]
    )
    store.record_auto_send_policy(owner_id, draft["id"], draft["version"], decision)
    if not decision["allowed"]:
        return {"status": "draft", "send_authorization": "manual_approval_required", "policy_decision": decision}

    idempotency_key = str(uuid.uuid4())
    state, attempt = store.begin_auto_send(
        owner_id,
        draft["id"],
        idempotency_key,
        draft["version"],
        decision,
    )
    if state != "started":
        policy = attempt.get("policy_decision") if attempt else None
        if state == "blocked":
            return {"status": "draft", "send_authorization": "manual_approval_required", "policy_decision": policy}
        return {
            "status": attempt["status"] if attempt else "draft",
            "send_authorization": "manual_approval_required",
            "policy_decision": policy or {"allowed": False, "reasons": ["send_not_started"]},
        }

    try:
        with store.send_lock(owner_id, auto_send=True):
            verified = store.verify_auto_send_before_provider(owner_id, draft["id"], draft["version"])
            if not verified:
                store.finish_send(
                    owner_id,
                    draft["id"],
                    "failed",
                    error="Auto-send permission or policy changed before provider submission.",
                    policy_decision={
                        "allowed": False,
                        "reasons": ["permissions_changed_before_send"],
                    },
                )
                return {
                    "status": "failed",
                    "send_authorization": "auto_policy",
                    "policy_decision": {
                        "allowed": False,
                        "reasons": ["permissions_changed_before_send"],
                    },
                }
            message_id = request.app.state.gmail_sender.send(
                verified["credentials_encrypted"],
                os.getenv("MAILPILOT_TOKEN_ENCRYPTION_KEY", ""),
                verified["draft"]["recipient"],
                verified["draft"]["subject"],
                verified["draft"]["body"],
            )
            store.finish_send(owner_id, draft["id"], "sent", message_id=message_id)
    except GmailSendError as exc:
        final_status = "uncertain" if exc.outcome_uncertain else "failed"
        store.finish_send(owner_id, draft["id"], final_status, error=str(exc))
        return {
            "status": final_status,
            "send_authorization": "auto_policy",
            "policy_decision": decision,
        }
    except Exception:
        store.finish_send(
            owner_id,
            draft["id"],
            "uncertain",
            error="The provider outcome could not be verified. Check Gmail before taking action.",
        )
        return {
            "status": "uncertain",
            "send_authorization": "auto_policy",
            "policy_decision": decision,
        }
    return {
        "status": "sent",
        "send_authorization": "auto_policy",
        "policy_decision": decision,
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _start_google_oauth(request: Request, intent: str, owner_id: str | None = None):
    flow = _oauth_flow(intent=intent)
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )
    session = request.cookies.get(SESSION_COOKIE)
    if intent == "gmail" and (not owner_id or not session):
        raise HTTPException(status_code=401, detail="Sign in before connecting Gmail.")
    cookie_name = GMAIL_OAUTH_COOKIE if intent == "gmail" else OAUTH_COOKIE
    request.app.state.store.create_oauth_state(
        _session_hash(state),
        intent,
        owner_id,
        _session_hash(session) if intent == "gmail" and session else None,
        OAUTH_STATE_MAX_AGE,
    )
    redirect = RedirectResponse(authorization_url, status_code=302)
    redirect.set_cookie(
        cookie_name,
        state,
        max_age=OAUTH_STATE_MAX_AGE,
        httponly=True,
        secure=_secure_cookies(),
        samesite="lax",
    )
    return redirect


@app.get("/api/auth/google/start")
@app.get("/api/auth/google/sign-in")
def google_auth_start(request: Request):
    return _start_google_oauth(request, "sign_in")


@app.get("/api/auth/google/connect-gmail")
def google_gmail_connect_start(
    request: Request, owner_id: str = Depends(authenticated_owner)
):
    return _start_google_oauth(request, "gmail", owner_id)


@app.get("/api/auth/google/callback")
def google_auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if not state:
        raise HTTPException(status_code=400, detail="Google sign-in was not completed; try again.")
    store = request.app.state.store
    intent = store.oauth_state_intent(_session_hash(state))
    if intent not in {"sign_in", "gmail"}:
        raise HTTPException(status_code=400, detail="Google sign-in was not completed; try again.")
    cookie_name = GMAIL_OAUTH_COOKIE if intent == "gmail" else OAUTH_COOKIE
    oauth_cookie = request.cookies.get(cookie_name)
    if not oauth_cookie or not secrets.compare_digest(state, oauth_cookie):
        raise HTTPException(status_code=400, detail="Google sign-in was not completed; try again.")
    current_session = request.cookies.get(SESSION_COOKIE)
    consumed = store.consume_oauth_state(
        _session_hash(state),
        intent,
        _session_hash(current_session) if current_session else None,
    )
    if not consumed:
        raise HTTPException(status_code=400, detail="Google sign-in was not completed; try again.")
    if error or not code:
        raise HTTPException(status_code=400, detail="Google sign-in was not completed; try again.")

    try:
        flow = _oauth_flow(state=state, intent=intent)
        flow.fetch_token(code=code)
        credentials = flow.credentials
        if intent == "sign_in" and not credentials.has_scopes(IDENTITY_SCOPES):
            raise HTTPException(status_code=403, detail="Google identity permissions were not granted.")
        if intent == "gmail" and not credentials.has_scopes([GMAIL_SEND_SCOPE]):
            raise HTTPException(status_code=403, detail="Gmail send permission was not granted.")
        identity = id_token.verify_oauth2_token(
            credentials.id_token, GoogleRequest(), os.getenv("GOOGLE_CLIENT_ID")
        )
        try:
            identity_owner = _owner_id_from_identity(identity)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Google did not provide a verified stable identity.") from exc
        account_email = identity["email"].strip().lower()
        if intent == "gmail":
            encryption_key = os.getenv("MAILPILOT_TOKEN_ENCRYPTION_KEY")
            if not encryption_key:
                raise HTTPException(
                    status_code=503,
                    detail="Set MAILPILOT_TOKEN_ENCRYPTION_KEY before connecting Gmail.",
                )
            from cryptography.fernet import Fernet

            encrypted = Fernet(encryption_key.encode("ascii")).encrypt(
                credentials.to_json().encode("utf-8")
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Google sign-in failed. Please try again.") from exc

    if intent == "sign_in":
        store.save_user(identity_owner, account_email)
        raw_session = secrets.token_urlsafe(32)
        store.create_session(_session_hash(raw_session), identity_owner, SESSION_MAX_AGE)
        redirect = RedirectResponse(FRONTEND_URL, status_code=303)
        redirect.set_cookie(
            SESSION_COOKIE,
            raw_session,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=_secure_cookies(),
            samesite="lax",
        )
    else:
        connected = store.save_connection(
            consumed["owner_id"],
            encrypted.decode("ascii"),
            credentials.scopes or [],
            account_email,
            session_hash=consumed["session_hash"],
        )
        if not connected:
            raise HTTPException(
                status_code=400,
                detail="Your sign-in session changed or expired. Sign in and connect Gmail again.",
            )
        redirect = RedirectResponse(FRONTEND_URL, status_code=303)
    redirect.delete_cookie(cookie_name)
    return redirect


@app.get("/api/auth/session")
def auth_session(request: Request, owner_id: str = Depends(authenticated_owner)):
    connection = request.app.state.store.get_connection(owner_id)
    user = request.app.state.store.get_user(owner_id)
    return {
        "owner_id": owner_id,
        "account_email": connection["account_email"] if connection else "",
        "user_email": user["email"] if user else "",
        "gmail_connected": bool(connection and GMAIL_SEND_SCOPE in connection["scopes"]),
    }


@app.post("/api/auth/logout")
def logout(
    request: Request,
    response: Response,
    mailpilot_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    if mailpilot_session:
        request.app.state.store.delete_session(_session_hash(mailpilot_session))
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "signed_out"}


@app.delete("/api/auth/gmail")
def disconnect_gmail(request: Request, owner_id: str = Depends(authenticated_owner)):
    request.app.state.store.disconnect_gmail(owner_id)
    return {
        "status": "disconnected",
        "auto_send_consent_cleared": True,
        "remote_revocation_attempted": False,
    }


@app.get("/api/settings/auto-send")
def get_auto_send_settings(request: Request, owner_id: str = Depends(authenticated_owner)):
    store = request.app.state.store
    settings = store.get_account_settings(owner_id)
    return {
        **settings,
        "kill_switch_enabled": store.kill_switch_enabled(),
        "can_engage_kill_switch": owner_id in _configured_admins(),
        "sent_today": store.auto_send_count_today(owner_id),
        "gmail_connected": bool(
            (connection := store.get_connection(owner_id))
            and GMAIL_SEND_SCOPE in connection["scopes"]
        ),
    }


@app.post("/api/settings/auto-send/enable")
def enable_auto_send(
    payload: EnableAutoSendRequest,
    request: Request,
    owner_id: str = Depends(authenticated_owner),
):
    store = request.app.state.store
    try:
        settings = store.save_auto_send_settings(
            owner_id,
            payload.recipient_allowlist,
            payload.categories,
            payload.daily_limit,
        )
    except GmailConnectionRequiredError as exc:
        raise HTTPException(
            status_code=409,
            detail="Connect Gmail with send permission before enabling auto-send.",
        ) from exc
    except AutoSendKillSwitchActiveError as exc:
        raise HTTPException(
            status_code=409,
            detail="The global auto-send kill switch is active.",
        ) from exc
    return {
        **settings,
        "kill_switch_enabled": store.kill_switch_enabled(),
        "sent_today": store.auto_send_count_today(owner_id),
    }


@app.post("/api/settings/auto-send/disable")
def disable_auto_send(request: Request, owner_id: str = Depends(authenticated_owner)):
    settings = request.app.state.store.disable_auto_send(owner_id)
    return {**settings, "kill_switch_enabled": request.app.state.store.kill_switch_enabled()}


@app.post("/api/settings/auto-send/kill-switch")
def engage_auto_send_kill_switch(request: Request, owner_id: str = Depends(authenticated_owner)):
    if owner_id not in _configured_admins():
        raise HTTPException(status_code=403, detail="Only a configured operator can engage the global kill switch.")
    store = request.app.state.store
    store.engage_kill_switch(owner_id)
    return {"kill_switch_enabled": True}


@app.post("/api/emails/generate", response_model=GenerateEmailResponse)
def generate_email(
    email_request: EmailRequest,
    request: Request,
    owner_id: str = Depends(authenticated_owner),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
) -> GenerateEmailResponse:
    store = request.app.state.store
    request_hash = hashlib.sha256(
        json.dumps(
            email_request.model_dump(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    claim, existing = store.claim_generation(owner_id, idempotency_key, request_hash)
    if claim == "conflict":
        raise HTTPException(status_code=409, detail="This idempotency key was already used for a different request.")
    if claim == "rate_limited":
        raise HTTPException(status_code=429, detail="Daily generation limit reached. Try again tomorrow.")
    if claim == "in_progress":
        raise HTTPException(status_code=409, detail="A matching generation request is already in progress or failed. Use a new key to retry.")
    if claim == "duplicate":
        if not existing:
            raise HTTPException(status_code=409, detail="The saved generation result is unavailable.")
        return _make_email_response(existing)

    service: EmailAIService = request.app.state.ai_service
    try:
        draft = service.generate_draft(email_request)
        review = service.review_draft(email_request, draft)
    except AIServiceError as exc:
        store.fail_generation(owner_id, idempotency_key)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    saved = store.create_draft(
        owner_id,
        email_request.recipient,
        draft.subject,
        draft.body,
        review.model_dump(),
        email_request.category,
        idempotency_key,
    )
    auto_result = _attempt_auto_send(request, owner_id, saved)
    refreshed = request.app.state.store.get_draft(owner_id, saved["id"])
    return _make_email_response({**refreshed, **auto_result})


@app.get("/api/drafts")
def list_drafts(request: Request, owner_id: str = Depends(authenticated_owner)):
    return request.app.state.store.list_drafts(owner_id)


@app.get("/api/audit")
def audit_history(request: Request, owner_id: str = Depends(authenticated_owner)):
    return request.app.state.store.audit_history(owner_id)


@app.post("/api/drafts/{draft_id}/approve")
def approve_draft(
    draft_id: str,
    approval: ApprovalRequest,
    request: Request,
    owner_id: str = Depends(authenticated_owner),
):
    current = request.app.state.store.get_draft(owner_id, draft_id)
    if not current:
        raise HTTPException(status_code=404, detail="Draft not found.")
    try:
        create_mime_message(approval.recipient, approval.subject, approval.body)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail="The recipient or email content is not valid for sending.") from exc
    result = request.app.state.store.approve_draft(
        owner_id,
        draft_id,
        str(approval.recipient),
        approval.subject,
        approval.body,
        _content_hash(str(approval.recipient), approval.subject, approval.body, current["version"] + 1),
    )
    if result == "not_found":
        raise HTTPException(status_code=404, detail="Draft not found.")
    if result == "locked":
        raise HTTPException(status_code=409, detail="This draft can no longer be edited or approved.")
    return {"id": result["id"], "status": result["status"], "approved_at": result["approved_at"]}


@app.post("/api/drafts/{draft_id}/send")
def send_draft(
    draft_id: str,
    request: Request,
    owner_id: str = Depends(authenticated_owner),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
):
    store = request.app.state.store
    draft = store.get_draft(owner_id, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    if draft["status"] == "sent":
        if draft["send_attempt_key"] == idempotency_key:
            return {
                "id": draft_id,
                "status": "sent",
                "message_id": draft["provider_message_id"],
                "sent_at": draft["sent_at"],
                "send_authorization": draft["send_authorization"],
                "policy_decision": draft["policy_decision"],
            }
        raise HTTPException(status_code=409, detail="This draft was already sent with a different idempotency key.")
    if draft["status"] == "sending":
        raise HTTPException(status_code=409, detail="A send attempt is already in progress.")
    if draft["status"] == "uncertain":
        raise HTTPException(
            status_code=409,
            detail="The prior send outcome is uncertain. Check Gmail; automatic retry is disabled.",
        )
    if draft["status"] != "approved" or not draft["approved_hash"]:
        raise HTTPException(status_code=409, detail="Approve this exact draft before sending.")
    if _content_hash(draft["recipient"], draft["subject"], draft["body"], draft["version"]) != draft["approved_hash"]:
        raise HTTPException(status_code=409, detail="Draft content changed after approval. Review and approve it again.")
    try:
        create_mime_message(draft["recipient"], draft["subject"], draft["body"])
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail="The recipient or email content is not valid for sending.") from exc

    connection = store.get_connection(owner_id)
    if not connection:
        raise HTTPException(status_code=409, detail="Connect Gmail before sending.")
    if GMAIL_SEND_SCOPE not in connection["scopes"]:
        raise HTTPException(status_code=403, detail="The current Gmail connection lacks send permission.")

    state, attempt = store.begin_send(
        owner_id,
        draft_id,
        idempotency_key,
        draft["approved_hash"],
        draft["version"],
    )
    if state == "not_found":
        raise HTTPException(status_code=404, detail="Draft not found.")
    if state == "approval_changed":
        raise HTTPException(status_code=409, detail="Draft content changed after approval. Review and approve it again.")
    if state == "already_sent":
        return {
            "id": draft_id,
            "status": "sent",
            "message_id": attempt["provider_message_id"],
            "sent_at": attempt["sent_at"],
            "send_authorization": attempt["send_authorization"],
            "policy_decision": attempt["policy_decision"],
        }
    if state == "duplicate":
        raise HTTPException(status_code=409, detail="This draft was already sent with a different idempotency key.")
    if state == "in_progress":
        raise HTTPException(status_code=409, detail="A send attempt is already in progress.")
    if state == "uncertain":
        raise HTTPException(status_code=409, detail="The prior send outcome is uncertain. Check Gmail; automatic retry is disabled.")
    try:
        with store.send_lock(owner_id):
            verified = store.verify_manual_send_before_provider(
                owner_id,
                draft_id,
                attempt["version"],
                attempt["approved_hash"],
            )
            if not verified:
                store.finish_send(
                    owner_id,
                    draft_id,
                    "failed",
                    error="Gmail permission or approved draft changed before provider submission.",
                )
                raise HTTPException(status_code=409, detail="Send permission or approval changed. Review and approve again.")
            message_id = request.app.state.gmail_sender.send(
                verified["connection"]["credentials_encrypted"],
                os.getenv("MAILPILOT_TOKEN_ENCRYPTION_KEY", ""),
                verified["draft"]["recipient"],
                verified["draft"]["subject"],
                verified["draft"]["body"],
            )
            store.finish_send(owner_id, draft_id, "sent", message_id=message_id)
    except HTTPException:
        raise
    except GmailSendError as exc:
        final_status = "uncertain" if exc.outcome_uncertain else "failed"
        store.finish_send(owner_id, draft_id, final_status, error=str(exc))
        raise HTTPException(
            status_code=503 if exc.outcome_uncertain else 502,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        store.finish_send(
            owner_id,
            draft_id,
            "uncertain",
            error="The provider outcome could not be verified. Check Gmail before taking action.",
        )
        raise HTTPException(
            status_code=503,
            detail="The provider outcome is uncertain. Check Gmail before retrying.",
        ) from exc
    finished = store.get_draft(owner_id, draft_id)
    return {
        "id": draft_id,
        "status": "sent",
        "message_id": message_id,
        "sent_at": finished["sent_at"],
        "send_authorization": "manual_approval",
        "policy_decision": None,
    }


if APP_ENV == "production":
    app.mount(
        "/",
        SpaStaticFiles(
            directory=str(frontend_build_directory()),
            html=True,
            check_dir=False,
        ),
        name="frontend",
    )
