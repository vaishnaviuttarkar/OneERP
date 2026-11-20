"""SMTP delivery. Credentials come only from settings (environment) and are never logged, stored or shown."""
import logging
import smtplib
import socket
from typing import NamedTuple

from django.conf import settings
from django.core.mail import EmailMessage, get_connection

logger = logging.getLogger("enquiries.mail")


class MailResult(NamedTuple):
    ok: bool
    error: str = ""      # short, generic, safe to show to staff


def _redact(text):
    text = str(text)
    for secret in (settings.EMAIL_HOST_PASSWORD, settings.EMAIL_HOST_USER):
        if secret:
            text = text.replace(secret, "***")
    return text


def _classify(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "The mail server rejected the mailbox login. Check the SMTP settings."
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return "The mail server refused the sender or the recipient address."
    if isinstance(exc, (smtplib.SMTPConnectError, socket.timeout, TimeoutError, ConnectionError, socket.gaierror)):
        return "Could not connect to the mail server."
    if isinstance(exc, smtplib.SMTPException):
        return "The mail server reported an error."
    if isinstance(exc, OSError):
        return "Could not connect to the mail server."
    return "Unexpected error while sending the e-mail."


def _is_smtp():
    return settings.EMAIL_BACKEND.endswith("smtp.EmailBackend")


def business_mailbox():
    return (getattr(settings, "BUSINESS_MAILBOX", "") or "").strip()


def send(*, subject, body, to, reply_to=None, headers=None):
    """Send one plain-text e-mail from the business mailbox. Never raises; returns MailResult."""
    if not to or not business_mailbox():
        logger.error("E-mail not sent: business mailbox is not configured")
        return MailResult(False, "The business mailbox is not configured on the server.")
    if _is_smtp() and not (settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD):
        logger.error("E-mail not sent: SMTP credentials are missing")
        return MailResult(False, "SMTP credentials are not configured on the server.")
    try:
        connection = get_connection(fail_silently=False, timeout=settings.EMAIL_TIMEOUT)
        EmailMessage(subject=subject, body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=list(to),
                     reply_to=list(reply_to) if reply_to else None, headers=headers or {}, connection=connection).send()
    except Exception as exc:       # noqa: BLE001 - delivery must never break the request
        logger.error("E-mail delivery failed (%s): %s", type(exc).__name__, _redact(exc))
        return MailResult(False, _classify(exc))
    return MailResult(True)
