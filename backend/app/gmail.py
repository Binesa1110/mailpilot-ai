import base64
import json
from email.message import EmailMessage
from email.policy import SMTP

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
MAX_MESSAGE_BYTES = 25 * 1024 * 1024


class GmailSendError(Exception):
    def __init__(self, message: str, outcome_uncertain: bool):
        super().__init__(message)
        self.outcome_uncertain = outcome_uncertain


def create_mime_message(recipient: str, subject: str, body: str) -> str:
    message = EmailMessage(policy=SMTP)
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    serialized = message.as_bytes()
    if len(serialized) > MAX_MESSAGE_BYTES:
        raise ValueError("The email exceeds Gmail's maximum message size.")
    return base64.urlsafe_b64encode(serialized).decode("ascii").rstrip("=")


class GmailSender:
    def send(self, encrypted_credentials: str, encryption_key: str, recipient: str, subject: str, body: str) -> str:
        from cryptography.fernet import Fernet, InvalidToken

        try:
            raw_credentials = Fernet(encryption_key.encode("ascii")).decrypt(
                encrypted_credentials.encode("ascii")
            )
            data = json.loads(raw_credentials)
            credentials = Credentials.from_authorized_user_info(data)
        except (ValueError, InvalidToken, json.JSONDecodeError) as exc:
            raise GmailSendError("The saved Gmail connection could not be read. Reconnect Gmail.", False) from exc

        if not credentials.has_scopes([GMAIL_SEND_SCOPE]):
            raise GmailSendError("The Gmail connection does not have permission to send mail.", False)
        if credentials.expired:
            if not credentials.refresh_token:
                raise GmailSendError("The Gmail connection expired. Reconnect Gmail.", False)
            try:
                credentials.refresh(GoogleRequest())
            except GoogleAuthError as exc:
                raise GmailSendError("The Gmail connection expired or was revoked. Reconnect Gmail.", False) from exc

        try:
            raw_message = create_mime_message(recipient, subject, body)
            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            result = service.users().messages().send(userId="me", body={"raw": raw_message}).execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None) or 500
            raise GmailSendError(
                "Gmail rejected the message. Check the draft and connection before trying again."
                if status < 500
                else "Gmail's response was inconclusive. The message may have been sent; check Gmail before taking action.",
                status >= 500,
            ) from exc
        except Exception as exc:
            raise GmailSendError(
                "Gmail's response was inconclusive. The message may have been sent; check Gmail before taking action.",
                True,
            ) from exc
        message_id = result.get("id") if isinstance(result, dict) else None
        if not message_id:
            raise GmailSendError(
                "Gmail accepted no verifiable message ID. The message may have been sent; check Gmail before taking action.",
                True,
            )
        return message_id
