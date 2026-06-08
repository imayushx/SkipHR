"""
SkipHR LeadFinder --- Automated Startup Lead Discovery
----------------------------------------------
Sources (all FREE, no API keys needed):
  1. YC OSS API       --- 5,800+ YC startups, tags, hiring status
  2. Hacker News Jobs --- "Who is Hiring?" threads (monthly)
  3. Email guesser    --- Pattern-based email generation from domain + name

Output: leads.csv --- ready to drop into mailer.py

Usage:
  python find_leads.py              # Interactive mode
  python find_leads.py --auto       # Use defaults from .env / CONFIG
  python find_leads.py --hn-only    # HN jobs only
  python find_leads.py --yc-only    # YC companies only
"""

import csv, json, os, re, time, sys, logging
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("finder.log"), logging.StreamHandler()]
)

# --------- CONFIG ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

OUTPUT_FILE = os.getenv("LEADS_FILE", "leads.csv")

# Tags to match against. Edit to focus on what YOU want.
TARGET_TAGS = {
    # Core targets
    "Fintech", "Payments", "Trading", "Crypto / Web3", "Investing",
    "Banking", "Lending", "Insurance", "Neobank", "Wealth Management",
    # Broad tech (Founder's Office / product eng)
    "B2B", "Developer Tools", "API", "SaaS", "AI", "Machine Learning",
    "Analytics", "Infrastructure", "No-code", "Automation",
    # Role-relevant
    "Marketplace", "Finance", "Quantitative Finance",
}

# Industries to match (if tags don't match, check industry)
TARGET_INDUSTRIES = {
    "Financial Technology and Services",
    "B2B",
    "Artificial Intelligence",
}

# Only show companies hiring OR include all active ones
HIRING_ONLY = os.getenv("HIRING_ONLY", "false").lower() == "true"

# Max leads to output (0 = no limit)
MAX_LEADS = int(os.getenv("MAX_LEADS", "200"))

# YC data source
YC_API_URL = "https://raw.githubusercontent.com/yc-oss/api/gh-pages/companies/all.json"

# --------- HELPERS ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def fetch_json(url: str, timeout: int = 20) -> dict | list | None:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SkipHR LeadFinder/1.0)"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        logging.warning(f"Failed to fetch {url}: {e}")
        return None


def extract_domain(website: str) -> str:
    """Clean domain from a website URL."""
    d = website.lower().replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")
    return d.split("/")[0]


def guess_emails(first: str, last: str, domain: str) -> list[str]:
    """Generate likely email patterns for a founder."""
    f = re.sub(r"[^a-z]", "", first.lower())
    l = re.sub(r"[^a-z]", "", last.lower())
    if not f:
        return [f"founders@{domain}", f"hello@{domain}"]
    patterns = []
    if l:
        patterns = [
            f"{f}@{domain}",
            f"{f}.{l}@{domain}",
            f"{f[0]}{l}@{domain}",
            f"{f[0]}.{l}@{domain}",
        ]
    else:
        patterns = [f"{f}@{domain}"]
    patterns += [f"founders@{domain}", f"hello@{domain}", f"team@{domain}"]
    return patterns


def load_existing_leads() -> set:
    """Return set of (company, email) already in output CSV to avoid duplicates."""
    seen = set()
    if not os.path.exists(OUTPUT_FILE):
        return seen
    with open(OUTPUT_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            seen.add((row.get("company", "").lower(), row.get("email", "").lower()))
    return seen


def append_leads(leads: list[dict]):
    """Append new leads to the CSV. Creates file + header if needed."""
    fieldnames = ["name", "email", "company", "role", "website", "context", "source", "tags", "sent", "sent_at"]
    file_exists = os.path.exists(OUTPUT_FILE)
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for lead in leads:
            writer.writerow({k: lead.get(k, "") for k in fieldnames})


# --------- SOURCE 1: YC Companies ---------------------------------------------------------------------------------------------------------------------------------------------------------

def fetch_yc_leads() -> list[dict]:
    logging.info("Fetching YC company database...")
    companies = fetch_json(YC_API_URL, timeout=30)
    if not companies:
        logging.error("Could not load YC data.")
        return []

    logging.info(f"Loaded {len(companies)} YC companies. Filtering...")

    existing = load_existing_leads()
    leads = []

    for c in companies:
        # Status filter
        if c.get("status") != "Active":
            continue

        # Hiring filter (optional)
        if HIRING_ONLY and not c.get("isHiring"):
            continue

        # Website required
        website = c.get("website", "")
        if not website or "missing" in website:
            continue

        # Tag / industry match
        tags = set(c.get("tags", []))
        industry = c.get("industry", "")
        matched = bool(tags & TARGET_TAGS) or any(t in industry for t in TARGET_INDUSTRIES)
        if not matched:
            continue

        domain  = extract_domain(website)
        name    = c.get("name", "")
        batch   = c.get("batch", "")
        one_liner = c.get("one_liner", "")
        tag_str = ", ".join(list(tags)[:4])

        # Generate a plausible founder email (best guess --- primary email to try)
        guessed_email = f"founder@{domain}"

        # Deduplicate
        if (name.lower(), guessed_email.lower()) in existing:
            continue

        leads.append({
            "name":    "Founder",          # Real name added via Hunter/LinkedIn manually
            "email":   guessed_email,
            "company": name,
            "role":    "Founder / CEO",
            "website": website,
            "context": f"YC {batch} --- {one_liner[:120]}",
            "source":  "YC",
            "tags":    tag_str,
            "sent":    "",
            "sent_at": "",
        })

        if MAX_LEADS and len(leads) >= MAX_LEADS:
            break

    logging.info(f"YC: {len(leads)} leads found.")
    return leads


# --------- SOURCE 2: Hacker News "Who is Hiring?" ---------------------------------------------------------------------------------------------------------

def fetch_hn_jobs() -> list[dict]:
    """
    Parse HN 'Who is Hiring?' job posts.
    Uses the Firebase HN API to get the latest thread's top-level comments.
    Each comment = one job posting from a company.
    """
    logging.info("Fetching Hacker News 'Who is Hiring?' thread...")

    # Get the HN monthly job thread (search for it)
    # We use the HN Algolia search API (publicly accessible)
    search_url = (
        "https://hn.algolia.com/api/v1/search?"
        "query=%22Ask+HN%3A+Who+is+hiring%22&tags=story,ask_hn&hitsPerPage=3"
    )
    result = fetch_json(search_url, timeout=15)
    if not result or not result.get("hits"):
        logging.warning("HN search failed.")
        return []

    # Get the most recent hiring thread
    thread = result["hits"][0]
    thread_id = thread.get("objectID")
    thread_title = thread.get("title", "")
    logging.info(f"Found HN thread: {thread_title} (id={thread_id})")

    # Fetch comments from the thread
    comments_url = (
        f"https://hn.algolia.com/api/v1/search?"
        f"tags=comment,story_{thread_id}&hitsPerPage=100"
    )
    comments = fetch_json(comments_url, timeout=15)
    if not comments:
        return []

    existing = load_existing_leads()
    leads = []
    keywords = [kw.lower() for kw in [
        "fintech", "trading", "finance", "payments", "crypto",
        "founder", "founding engineer", "product", "full stack",
        "python", "typescript", "b2b", "saas", "api", "ai", "ml",
        "remote", "india", "series a", "series b", "seed"
    ]]

    for hit in comments.get("hits", []):
        text = hit.get("comment_text", "") or ""
        if not text:
            continue

        # Score relevance by keyword hits
        text_lower = text.lower()
        score = sum(1 for kw in keywords if kw in text_lower)
        if score < 2:
            continue

        # Extract company name (usually first line, may have | separator)
        lines = [l.strip() for l in re.split(r"[\n|<br>]+", text) if l.strip()]
        company_line = lines[0][:80] if lines else "Unknown"

        # Extract website from text
        url_match = re.search(r'https?://[^\s\'"<>]+', text)
        website = url_match.group(0)[:100] if url_match else ""
        domain = extract_domain(website) if website else ""

        # Extract email from text
        email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w{2,}', text)
        email = email_match.group(0) if email_match else (f"hiring@{domain}" if domain else "")

        if not email:
            continue

        if (company_line.lower(), email.lower()) in existing:
            continue

        leads.append({
            "name":    "Hiring Manager",
            "email":   email,
            "company": company_line,
            "role":    "Hiring",
            "website": website,
            "context": f"HN Who's Hiring --- score {score}/10 --- {text[:150].strip()}...",
            "source":  "HN",
            "tags":    "",
            "sent":    "",
            "sent_at": "",
        })

    logging.info(f"HN: {len(leads)} relevant job posts found.")
    return leads


# --------- SOURCE 3: Email enrichment from domain (pattern guessing) ------------------------------------------------

def enrich_emails_for_leads(leads: list[dict]) -> list[dict]:
    """
    For leads that only have a domain-guessed email (founder@domain.com),
    expand into multiple guesses that you can manually verify with Hunter.io.
    """
    enriched = []
    for lead in leads:
        email = lead.get("email", "")
        if email.startswith("founder@") or email.startswith("hiring@"):
            domain = extract_domain(lead.get("website", ""))
            if domain:
                guesses = guess_emails("", "", domain)
                # Keep primary guess, add all guesses to context
                lead["email"] = guesses[0]
                lead["context"] = (lead.get("context", "") + f" | Email guesses: {', '.join(guesses[:3])}")
        enriched.append(lead)
    return enriched


# --------- REPORT PRINTER ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def print_summary(leads: list[dict]):
    print("\n" + "="*65)
    print(f"  LEADFINDER RESULTS --- {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*65)

    sources = {}
    for l in leads:
        s = l.get("source", "?")
        sources[s] = sources.get(s, 0) + 1

    for src, count in sources.items():
        print(f"  {src:15} --- {count} leads")
    print(f"  {'TOTAL':15} --- {len(leads)} leads")
    print(f"\n  Saved to: {OUTPUT_FILE}")
    print("="*65)
    print()
    print("  Next steps:")
    print("  1. Open leads.csv --- review and fill in real founder names")
    print("  2. Use Hunter.io (free) to verify emails for top targets")
    print("  3. Run: python mailer.py --dry-run  --- preview emails")
    print("  4. Run: python mailer.py --now       --- send campaign")
    print()


# --------- MAIN ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    all_leads = []

    yc_only = "--yc-only" in args
    hn_only = "--hn-only" in args
    run_all = not yc_only and not hn_only

    if run_all or yc_only:
        yc_leads = fetch_yc_leads()
        all_leads.extend(yc_leads)

    if run_all or hn_only:
        hn_leads = fetch_hn_jobs()
        all_leads.extend(hn_leads)

    if not all_leads:
        logging.info("No new leads found.")
        return

    # Enrich with email guesses
    all_leads = enrich_emails_for_leads(all_leads)

    # Save
    append_leads(all_leads)
    print_summary(all_leads)


if __name__ == "__main__":
    main()
