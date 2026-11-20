import smtplib

from django.core.mail.backends.base import BaseEmailBackend


class FailingBackend(BaseEmailBackend):
    """Simulates the SMTP server rejecting the login; the server's reply even echoes the password,
    so the tests can prove that secrets are redacted from logs and never stored."""
    def send_messages(self, email_messages):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed for mailbox@example.com using hunter2secret")
