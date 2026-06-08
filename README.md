<h1 align="center">SkipHR 🚀</h1>
<p align="center"><b>Skip the ATS. Go straight to the founder.</b></p>
<p align="center">Find startups → enrich founder emails → send AI-personalized cold emails.<br>
Runs on your machine. Zero cost. No subscriptions. No middleman.</p>

---

> **The whole premise:** Most job tools help you apply faster to job boards. SkipHR skips the board entirely — it finds the founder's email and puts you directly in their inbox.

---

## What it does

Most job automation tools target job boards and ATS form-filling. SkipHR does something different — it goes **directly to founders**, bypassing ATS entirely.

```
find_leads.py  →  enrich_leads.py  →  mailer.py
  discover          enrich              send
```

**Step 1 — Discover** (`find_leads.py`)
- Pulls 5,870+ YC-backed startups from the public YC OSS API
- Scrapes Hacker News "Who is Hiring?" threads monthly
- Filters by tags you care about (Fintech, B2B, AI, Trading, etc.)
- Outputs `leads.csv` with company name, website, tags, context

**Step 2 — Enrich** (`enrich_leads.py`)
- Crawls each startup's homepage + `/about`, `/team`, `/contact`, `/people` pages
- Extracts real emails from `mailto:` links and page text
- Finds founder names via JSON-LD structured data, heading heuristics, DuckDuckGo search snippets
- Generates scored email patterns: `firstname@`, `firstname.lastname@`, `flastname@`, etc.
- Validates emails via MX record check + SMTP RCPT TO (no email sent)

**Step 3 — Send** (`mailer.py`)
- Uses Groq (free) to write a personalized cold email for each lead
- Sends via Gmail SMTP with your resume attached
- Marks each lead as sent — never emails the same person twice
- Runs on a schedule (daily at 9am) or on-demand

---

## Stack

| Tool | Purpose | Cost |
|------|---------|------|
| Python 3.10+ | Core runtime | Free |
| YC OSS API | 5,870 startup profiles | Free |
| HN Algolia API | Monthly hiring threads | Free |
| requests + BeautifulSoup | Web scraping | Free |
| dnspython | MX / email validation | Free |
| Groq (`llama3-8b-8192`) | AI email personalization | Free tier |
| Gmail SMTP | Email sending | Free |

**Total cost: $0/month**

---

## Quickstart

### 1. Clone and install

```bash
git clone https://github.com/imayushx/skiphr.git
cd skiphr
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Where to get it |
|----------|----------------|
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASS` | [Gmail App Password](https://myaccount.google.com/apppasswords) — not your real password |
| `GROQ_API_KEY` | Free at [console.groq.com](https://console.groq.com) |

### 3. Add your resume

Place your resume PDF in the project folder, named `resume.pdf`.

### 4. Run the pipeline

```bash
# Step 1: Find startups
python find_leads.py

# Step 2: Enrich with founder names + emails
python enrich_leads.py --limit 30

# Step 3: Preview emails (no sending)
python mailer.py --dry-run

# Step 4: Send
python mailer.py --now
```

---

## Targeting

Edit `TARGET_TAGS` in `find_leads.py` to match your search:

```python
TARGET_TAGS = {
    "Fintech", "Payments", "Trading", "B2B", "AI",
    "Developer Tools", "SaaS", "Analytics", ...
}
```

All YC tags include: `Fintech`, `B2B`, `SaaS`, `AI`, `Machine Learning`, `Crypto / Web3`, `Developer Tools`, `Payments`, `Trading`, `Neobank`, `Infrastructure`, and 80+ more.

Set `HIRING_ONLY=true` in `.env` to only target companies actively hiring on YC.

---

## Scheduling (run daily automatically)

**Mac / Linux (cron):**
```bash
crontab -e
# Add:
0 8 * * * cd /path/to/skiphr && python find_leads.py
0 9 * * * cd /path/to/skiphr && python mailer.py --now
```

**Windows (Task Scheduler):**
1. Open Task Scheduler → Create Basic Task
2. Set trigger: Daily at 9:00 AM
3. Action: `python C:\path\to\skiphr\mailer.py --now`

---

## How the email enrichment works

For each company, the enricher runs 5 layers:

1. **Website crawl** — checks up to 10 pages per site for `mailto:` links and email-shaped text
2. **JSON-LD parsing** — reads structured data if the site uses it (most modern sites do)
3. **Heading heuristics** — finds "John Doe, CEO" patterns in headings and team sections
4. **DuckDuckGo search** — queries `"Company" founder site:linkedin.com`, parses name from snippet
5. **Pattern scoring** — generates all email formats ranked by founder likelihood, picks highest score

Email validation uses:
- **MX record check** — confirms the domain has a mail server (filters dead domains)
- **SMTP RCPT TO** — pings the mail server to verify the mailbox exists, without sending anything

---

## Email limits & safety

- Default: 20 emails/day (`DAILY_LIMIT` in `.env`)
- 15-second delay between sends (avoids spam flags)
- Never sends to the same address twice
- Gmail's actual limit is ~500/day; staying under 50 keeps your domain clean

---

## Project structure

```
skiphr/
├── find_leads.py       ← Discovers startups (YC + HN)
├── enrich_leads.py     ← Finds founder names + emails
├── mailer.py           ← AI-personalized email sender
├── leads.csv           ← Your lead database (gitignored)
├── resume.pdf          ← Your resume (gitignored)
├── .env                ← Your secrets (gitignored)
├── .env.example        ← Config template
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Contributing

PRs welcome. Some ideas for v2:

- [ ] Wellfound / AngelList as an additional startup source
- [ ] LinkedIn company page scraper (session cookie based)
- [ ] Auto follow-up: if no reply in N days, send a bump email
- [ ] Notion / Airtable / Google Sheets CRM export
- [ ] Web dashboard for leads.csv with status tracking
- [ ] Indian startup sources: YourStory, Tracxn, Inc42
- [ ] Slack/Telegram notification when someone replies

---

## Disclaimer

This tool scrapes publicly available data and sends emails to professional addresses. Use responsibly — target relevant people, keep daily volume sane, and make your emails genuinely worth reading.

---

## License

MIT — do whatever you want with it.

---

Built by [Ayush](https://github.com/imayushx) · Star ⭐ if it saves you time
