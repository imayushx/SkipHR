"""
SkipHR Mailer - Automated Cold Email for Job Applications
------------------------------------------------------
- Reads leads from leads.csv
- Uses Groq (free) to personalize each email
- Sends via Gmail SMTP
- Updates CSV with sent status + timestamp
- Runs on a schedule (daily at 9am)
- Attaches your resume PDF automatically

SETUP: See README.md
"""

import csv
import os
import smtplib
import time
import schedule
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("mailer.log"),
        logging.StreamHandler()
    ]
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

GMAIL_USER     = os.getenv("GMAIL_USER")       # your Gmail address
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS")   # Gmail App Password (not your real password)
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")     # Free at console.groq.com
RESUME_PATH    = os.getenv("RESUME_PATH", "resume.pdf")  # Path to your resume PDF
LEADS_FILE     = "leads.csv"
DAILY_LIMIT    = int(os.getenv("DAILY_LIMIT", "20"))     # Max emails per day
DELAY_SECONDS  = int(os.getenv("DELAY_SECONDS", "15"))   # Seconds between sends

# Your profile — edit this once, used to personalize every email
YOUR_PROFILE = os.getenv("YOUR_PROFILE", """
Name: Ayush
Background: Final-year B.Tech Computer Science + Finance student at LPU, graduating 2026.
Experience: Built ResumeBoost AI (NVIDIA NIM/llama-3.3-70b), Brisk (stock analysis tool with organic traction), 
Que (movie discovery app), and an algorithmic XAUUSD trading bot (65% win rate, 1.65 profit factor).
Skills: Next.js, Python, FastAPI, Supabase, XGBoost, Pine Script, TradingView webhooks.
Looking for: Founder's Office, Prop Trading, Fintech Product / Founding Engineer roles.
Open to: Relocate anywhere in India or internationally.
""")

# ─── AI EMAIL GENERATOR ───────────────────────────────────────────────────────

def generate_email(name: str, company: str, role: str, context: str) -> dict:
    """Generate a personalized subject + body using Groq (free tier)."""
    client = Groq(api_key=GROQ_API_KEY)

    prompt = f"""
You are writing a cold email from a job seeker to a recruiter or founder.
Write a SHORT, direct, human-sounding cold email. No fluff, no corporate speak.
DO NOT use bullet points. Max 4 sentences in the body.

Sender profile:
{YOUR_PROFILE}

Target:
- Name: {name}
- Company: {company}
- Role they hire for / their role: {role}
- Context / why reaching out: {context}

Return ONLY a JSON object with two keys:
- "subject": the email subject line (concise, not salesy)
- "body": the full email body (plain text, include a greeting and sign-off — the sender's name is Ayush, always sign off as "Ayush")

No markdown, no explanation, just the raw JSON.
"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=400,
    )

    import json, re
    raw = response.choices[0].message.content.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    # Parse — Groq properly escapes \n inside JSON strings
    result = json.loads(raw)
    assert "subject" in result and "body" in result, "AI response missing subject or body"
    return result


# ─── EMAIL SENDER ─────────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, body: str) -> bool:
    """Send email via Gmail SMTP with resume attached."""
    try:
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        # Attach resume if file exists
        if os.path.exists(RESUME_PATH):
            with open(RESUME_PATH, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(RESUME_PATH))
                part["Content-Disposition"] = f'attachment; filename="{os.path.basename(RESUME_PATH)}"'
                msg.attach(part)
        else:
            logging.warning(f"Resume not found at {RESUME_PATH} — sending without attachment")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.sendmail(GMAIL_USER, to_email, msg.as_string())

        return True

    except Exception as e:
        logging.error(f"Failed to send to {to_email}: {e}")
        return False


# ─── CSV HELPERS ──────────────────────────────────────────────────────────────

def load_leads() -> list[dict]:
    """Load leads from CSV. Required columns: name, email, company, role, context"""
    if not os.path.exists(LEADS_FILE):
        logging.error(f"{LEADS_FILE} not found. Create it first (see README).")
        return []
    with open(LEADS_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def mark_sent(leads: list[dict], index: int):
    """Update a lead row as sent with timestamp."""
    leads[index]["sent"] = "YES"
    leads[index]["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    fieldnames = leads[0].keys()
    with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(leads)


# ─── MAIN CAMPAIGN RUNNER ─────────────────────────────────────────────────────

def run_campaign():
    logging.info("=" * 50)
    logging.info("Campaign started")

    leads = load_leads()
    if not leads:
        return

    # Only pick leads not yet sent
    pending = [(i, lead) for i, lead in enumerate(leads) if lead.get("sent", "").upper() != "YES"]

    if not pending:
        logging.info("No pending leads. Add more to leads.csv.")
        return

    batch = pending[:DAILY_LIMIT]
    logging.info(f"Sending to {len(batch)} leads today (limit: {DAILY_LIMIT})")

    sent_count = 0
    for i, (original_index, lead) in enumerate(batch):
        name    = lead.get("name", "there")
        email   = lead.get("email", "").strip()
        company = lead.get("company", "your company")
        role    = lead.get("role", "")
        context = lead.get("context", "")

        if not email:
            logging.warning(f"Row {original_index+1}: No email address, skipping.")
            continue

        logging.info(f"[{i+1}/{len(batch)}] Generating email for {name} at {company}...")
        try:
            result  = generate_email(name, company, role, context)
            subject = result["subject"]
            body    = result["body"]
        except Exception as e:
            logging.error(f"AI generation failed for {name}: {e}")
            continue

        success = send_email(email, subject, body)
        if success:
            mark_sent(leads, original_index)
            sent_count += 1
            logging.info(f"[OK] Sent to {email}")
        else:
            logging.warning(f"[FAIL] Failed for {email}")

        if i < len(batch) - 1:
            time.sleep(DELAY_SECONDS)

    logging.info(f"Campaign done. Sent: {sent_count}/{len(batch)}")
    logging.info("=" * 50)


# ─── SCHEDULER ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--dry-run" in sys.argv:
        # Print what would be sent without actually sending
        logging.info("DRY RUN MODE — no emails will be sent")
        leads = load_leads()
        pending = [(i, lead) for i, lead in enumerate(leads) if lead.get("sent", "").upper() != "YES"]
        batch = pending[:DAILY_LIMIT]
        for i, (_, lead) in enumerate(batch):
            print(f"\n{'='*50}")
            print(f"TO:      {lead['email']}")
            print(f"COMPANY: {lead['company']}")
            print(f"CONTEXT: {lead['context']}")
            try:
                result = generate_email(lead['name'], lead['company'], lead['role'], lead['context'])
                print(f"SUBJECT: {result['subject']}")
                print(f"BODY:\n{result['body']}")
            except Exception as e:
                print(f"AI ERROR: {e}")
        print(f"\n{'='*50}")
        print(f"Would send {len(batch)} emails.")

    elif "--now" in sys.argv:
        # Run immediately
        run_campaign()
    else:
        # Schedule daily at 9:00 AM
        schedule.every().day.at("09:00").do(run_campaign)
        logging.info("Scheduler started. Campaigns will run daily at 09:00 AM.")
        logging.info("To test immediately:  python mailer.py --now")
        logging.info("To preview emails:    python mailer.py --dry-run")
        while True:
            schedule.run_pending()
            time.sleep(30)
