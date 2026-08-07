import os
import smtplib
from email.mime.text import MIMEText

def send_email(subject: str, body: str) -> None:
    """Send a plain-text email using Gmail SMTP.
    Reads credentials from env vars: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL_TO.
    Silently skips if any env var is missing (so scripts don't crash if email isn't configured
    in a given workflow), and never raises on send failure -- it prints the error instead,
    so an email hiccup never breaks the Telegram-sending flow.
    """
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("NOTIFY_EMAIL_TO")

    if not sender or not password or not recipient:
        print("Email not sent: GMAIL_ADDRESS / GMAIL_APP_PASSWORD / NOTIFY_EMAIL_TO not all set.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        print("Email sent.")
    except Exception as e:
        print(f"Email send failed: {e}")
