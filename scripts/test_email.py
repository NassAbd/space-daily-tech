import os
import smtplib
import ssl
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

server = os.getenv("MAIL_SERVER", "smtp.gmail.com")
port_str = os.getenv("MAIL_PORT", "465")
username = os.getenv("MAIL_USERNAME")
password = os.getenv("MAIL_PASSWORD")
to_email = os.getenv("MAIL_TO")

# Clean quotes from env if present
if password and password.startswith('"') and password.endswith('"'):
    password = password[1:-1]

if not all([username, password, to_email]):
    print("❌ Error: Missing MAIL_USERNAME, MAIL_PASSWORD, or MAIL_TO in your .env!")
    exit(1)

try:
    port = int(port_str)
except ValueError:
    port = 465

print(f"Connecting to {server}:{port} as {username}...")

msg = EmailMessage()
msg["Subject"] = "🚀 Space Daily Tech — Local SMTP Connection Test"
msg["From"] = f"Space Daily Tech <{username}>"
msg["To"] = to_email
msg.set_content(
    "Hello!\n\n"
    "Your local SMTP credentials are correct! "
    "The test email was successfully sent from your local workspace."
)

context = ssl.create_default_context()

try:
    with smtplib.SMTP_SSL(server, port, context=context) as smtp:
        smtp.login(username, password)
        smtp.send_message(msg)
    print("✅ Success! Test email has been successfully sent to:", to_email)
except Exception as e:
    print("❌ Failed to send email. Error:")
    print(e)
