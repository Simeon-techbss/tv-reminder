from tv_reminder import send_email

send_email(
    "TV reminder - test",
    "If you got this, Gmail SMTP + App Password is working from your Raspberry Pi."
)

print("Sent test email.")