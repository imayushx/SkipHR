"""
enrich_leads.py - SkipHR Enrichment Engine
==========================================
Takes leads.csv (from find_leads.py) and enriches each row with:

  1. FOUNDER NAME     - Scraped from company website About/Team page
                        + DuckDuckGo search snippet for LinkedIn
  2. REAL EMAIL       - Scraped from website contact/about pages (mailto + regex)
  3. EMAIL PATTERNS   - Generated from name + domain (firstname@, f.lastname@, etc.)
  4. EMAIL VALIDATION - MX record check (confirms domain accepts email)
                        + SMTP RCPT check if port 25 is open on your machine
  5. CONTEXT UPDATE   - Enriches context field with founder title & description

Output: overwrites leads.csv with filled name + email columns

Usage:
  python enrich_leads.py              # Enrich all unsent leads
  python enrich_leads.py --limit 20   # Only enrich first 20 rows
  python enrich_leads.py --recheck    # Re-enrich even already-processed rows
"""

import csv, os, re, time, socket, smtplib, logging, sys, json
from urllib.parse import urljoin, quote_plus
import requests
import dns.resolver
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("enricher.log"), logging.StreamHandler()]
)

# --- CONFIG -------------------------------------------------------------------

LEADS_FILE      = os.getenv("LEADS_FILE", "leads.csv")
REQUEST_TIMEOUT = int(os.getenv("ENRICH_TIMEOUT", "8"))
DELAY_BETWEEN   = float(os.getenv("ENRICH_DELAY", "1.5"))   # seconds between requests
MAX_WORKERS     = int(os.getenv("ENRICH_WORKERS", "3"))       # parallel workers
SMTP_VERIFY     = os.getenv("SMTP_VERIFY", "true").lower() == "true"  # SMTP RCPT check

# User-agent pool to rotate
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Email noise / false positives to filter out
JUNK_DOMAINS  = {"example.com","sentry.io","wixpress.com","squarespace.com",
                  "cloudflare.com","intercom.io","hubspot.com","mailchimp.com",
                  "typeform.com","segment.io","amplitude.com","googletagmanager.com",
                  "facebook.com","twitter.com","instagram.com","linkedin.com"}
JUNK_PATTERNS = re.compile(r'@\d|\.png|\.jpg|\.svg|\.gif|\.webp|noreply|no-reply|donotreply|unsubscribe', re.I)

EMAIL_RE = re.compile(r'(?<![="\'])([a-zA-Z0-9._%+\-]{2,}@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})')

# Pages to crawl on every startup site
CONTACT_PATHS = [
    "", "/contact", "/about", "/about-us", "/team", "/company",
    "/people", "/founders", "/who-we-are", "/contact-us"
]

# Name patterns from About/Team pages
NAME_RE = re.compile(r'\b([A-Z][a-z------\-]+\s+[A-Z][a-z------\-]+(?:\s+[A-Z][a-z------\-]+)?)\b')
FOUNDER_TITLE_RE = re.compile(
    r'(founder|co-founder|ceo|chief executive|cto|chief technology|president|'
    r'md|managing director|director)', re.I
)

# --- HTTP HELPER --------------------------------------------------------------

import random

def get(url: str, timeout: int = REQUEST_TIMEOUT) -> requests.Response | None:
    headers = {"User-Agent": random.choice(UA_POOL),
               "Accept": "text/html,application/xhtml+xml",
               "Accept-Language": "en-US,en;q=0.9"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout,
                         allow_redirects=True, stream=False)
        return r if r.status_code < 400 else None
    except Exception:
        return None


# --- 1. WEBSITE EMAIL SCRAPER -------------------------------------------------

def scrape_website(base_url: str) -> dict:
    """
    Crawl homepage + contact/about/team pages.
    Returns: {emails: set, founder_names: list, raw_text: str}
    """
    result = {"emails": set(), "founder_names": [], "pages_hit": 0}
    seen_text = []

    for path in CONTACT_PATHS:
        url = base_url.rstrip("/") + path
        resp = get(url)
        if not resp:
            continue
        result["pages_hit"] += 1
        soup = BeautifulSoup(resp.text, "html.parser")

        # -- Extract emails ------------------------------------------
        # mailto: links (most reliable)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().startswith("mailto:"):
                em = href[7:].split("?")[0].strip().lower()
                if _is_good_email(em):
                    result["emails"].add(em)

        # regex in full visible text
        page_text = soup.get_text(" ", strip=True)
        for em in EMAIL_RE.findall(page_text):
            em = em.strip().lower().rstrip(".")
            if _is_good_email(em):
                result["emails"].add(em)

        seen_text.append(page_text)

        # -- Extract founder names from About/Team pages -------------
        if path in ("/about", "/about-us", "/team", "/people", "/founders", ""):
            _extract_founder_names(soup, page_text, result)

        time.sleep(0.5)

    result["full_text"] = " ".join(seen_text)[:3000]
    return result


def _is_good_email(email: str) -> bool:
    if not email or len(email) > 80 or "@" not in email:
        return False
    domain = email.split("@")[1].lower()
    if domain in JUNK_DOMAINS:
        return False
    if JUNK_PATTERNS.search(email):
        return False
    # Must have a real TLD
    parts = domain.split(".")
    if len(parts) < 2 or len(parts[-1]) < 2:
        return False
    return True


def _extract_founder_names(soup: BeautifulSoup, text: str, result: dict):
    """
    Heuristic: Find a person's name near a founder/CEO title on a page.
    Looks at: headings, card divs, meta descriptions, team sections.
    """
    found = []

    # 1. Structured data (JSON-LD) - most reliable when present
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            if isinstance(data, dict):
                # Person or Organization with founder
                for key in ("founder", "founders", "member", "employee"):
                    val = data.get(key)
                    if isinstance(val, dict) and val.get("name"):
                        found.append(val["name"])
                    elif isinstance(val, list):
                        for item in val:
                            if isinstance(item, dict) and item.get("name"):
                                found.append(item["name"])
        except Exception:
            pass

    # 2. OG / meta tags
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        # Sometimes: "Company - John Doe, CEO"
        m = re.search(r'-\s*([A-Z][a-z]+\s+[A-Z][a-z]+)', og_title["content"])
        if m:
            found.append(m.group(1))

    # 3. Headings + nearby title text (Team/About section heuristic)
    for tag in soup.find_all(["h1","h2","h3","h4","p","div","span"]):
        tag_text = tag.get_text(" ", strip=True)
        if FOUNDER_TITLE_RE.search(tag_text) and len(tag_text) < 200:
            # Look for a name before or in the same element
            names = NAME_RE.findall(tag_text)
            found.extend(names[:2])
            # Also look at previous sibling
            prev = tag.find_previous_sibling()
            if prev:
                ptext = prev.get_text(" ", strip=True)
                names2 = NAME_RE.findall(ptext)
                found.extend(names2[:1])

    # 4. Sliding window in text: "John Doe, CEO" or "CEO John Doe"
    windows = re.findall(
        r'([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)'  # Name
        r'[\s,-\-]*'
        r'(?:co-?founder|founder|ceo|chief executive|president|md)',
        text, re.I
    )
    found.extend(windows)

    windows2 = re.findall(
        r'(?:co-?founder|founder|ceo|chief executive|president|md)'
        r'[\s,-\-]*'
        r'([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)',
        text, re.I
    )
    found.extend(windows2)

    # Deduplicate, filter short/common noise words
    NOISE = {"San Francisco","New York","Los Angeles","United States","About Us",
              "Our Team","Read More","Learn More","Sign Up","Get Started"}
    clean = []
    seen = set()
    for name in found:
        name = name.strip()
        if name and name not in NOISE and name not in seen and len(name.split()) >= 2:
            seen.add(name)
            clean.append(name)

    result["founder_names"].extend(clean[:3])


# --- 2. DUCKDUCKGO SEARCH -----------------------------------------------------

def search_founder_name(company_name: str, website: str) -> str | None:
    """
    DuckDuckGo HTML search for '[Company] founder CEO' - extracts name from snippets.
    Falls back to None if blocked or no result.
    """
    query = f'"{company_name}" founder CEO site:linkedin.com'
    url   = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    resp  = get(url, timeout=10)
    if not resp:
        # Try without site: restriction
        query2 = f'"{company_name}" founder OR CEO name'
        url2   = f"https://html.duckduckgo.com/html/?q={quote_plus(query2)}"
        resp   = get(url2, timeout=10)
        if not resp:
            return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Result titles and snippets
    for sel in [".result__title a", ".result__snippet", ".result__title"]:
        for el in soup.select(sel)[:8]:
            text = el.get_text(" ", strip=True)
            # LinkedIn format: "FirstName LastName - Founder at Company | LinkedIn"
            m = re.match(r'^([A-Z][a-z-----\-]+ [A-Z][a-z-----\-]+(?:\s[A-Z][a-z-----\-]+)?)\s*[--|]', text)
            if m:
                return m.group(1)
            # Snippet format: "...CEO John Doe founded..."
            m2 = re.search(
                r'(?:ceo|founder|co-founder|chief executive)\s+([A-Z][a-z]+ [A-Z][a-z]+)',
                text, re.I
            )
            if m2:
                return m2.group(1)

    return None


# --- 3. EMAIL PATTERN GENERATOR -----------------------------------------------

def generate_email_patterns(first: str, last: str, domain: str) -> list[tuple[str, int]]:
    """
    Returns list of (email, priority_score).
    Score 10 = most likely, 1 = generic fallback.
    """
    f = re.sub(r"[^a-z]", "", first.lower()) if first else ""
    l = re.sub(r"[^a-z]", "", last.lower())  if last  else ""
    patterns = []

    if f and l:
        patterns = [
            (f"{f}@{domain}",          10),
            (f"{f}.{l}@{domain}",       9),
            (f"{f[0]}{l}@{domain}",     8),
            (f"{f[0]}.{l}@{domain}",    7),
            (f"{f}_{l}@{domain}",       6),
            (f"{l}@{domain}",           4),
            (f"{f}{l}@{domain}",        3),
        ]
    elif f:
        patterns = [(f"{f}@{domain}", 10)]

    # Generic fallbacks
    patterns += [
        (f"founders@{domain}",  2),
        (f"hello@{domain}",     2),
        (f"hi@{domain}",        1),
        (f"team@{domain}",      1),
        (f"info@{domain}",      1),
    ]
    return sorted(patterns, key=lambda x: x[1], reverse=True)


# --- 4. EMAIL VALIDATOR -------------------------------------------------------

_mx_cache = {}

def get_mx(domain: str) -> str | None:
    """Resolve MX record for domain. Cached."""
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        mx_records = dns.resolver.resolve(domain, "MX")
        mx = str(sorted(mx_records, key=lambda r: r.preference)[0].exchange).rstrip(".")
        _mx_cache[domain] = mx
        return mx
    except Exception:
        _mx_cache[domain] = None
        return None


def validate_email_smtp(email: str, from_email: str = "verify@gmail.com") -> str:
    """
    Returns: 'valid' | 'invalid' | 'unknown' | 'no_mx'
    Tries SMTP RCPT TO. Falls back to 'unknown' if port blocked (common on ISPs).
    """
    domain = email.split("@")[1]
    mx     = get_mx(domain)
    if not mx:
        return "no_mx"

    try:
        # Resolve to IPv4
        ip = socket.getaddrinfo(mx, 25, socket.AF_INET)[0][4][0]
        smtp = smtplib.SMTP(timeout=8)
        smtp.connect(ip, 25)
        smtp.helo("gmail.com")
        smtp.mail(from_email)
        code, _ = smtp.rcpt(email)
        smtp.quit()
        if code == 250:
            return "valid"
        elif code in (550, 551, 553):
            return "invalid"
        else:
            return "unknown"
    except (smtplib.SMTPConnectError, ConnectionRefusedError, OSError):
        # Port 25 blocked (most ISPs / VPS do this)
        # Fall back to just MX existence as "domain_ok"
        return "domain_ok"
    except Exception:
        return "unknown"


def validate_email_quick(email: str) -> str:
    """Fast validation: just check MX exists."""
    domain = email.split("@")[1]
    mx = get_mx(domain)
    return "domain_ok" if mx else "no_mx"


# --- 5. MAIN ENRICHER ---------------------------------------------------------

def enrich_lead(lead: dict) -> dict:
    """
    Enrich a single lead row. Returns updated dict.
    """
    company = lead.get("company", "").strip()
    website = lead.get("website", "").strip()
    current_name  = lead.get("name", "").strip()
    current_email = lead.get("email", "").strip()

    if not website:
        lead["enrich_status"] = "no_website"
        return lead

    # Normalize website
    if not website.startswith("http"):
        website = "https://" + website
    
    domain = re.sub(r"^www\.", "", website.replace("https://","").replace("http://","").split("/")[0])

    logging.info(f"Enriching: {company} ({domain})")

    # -- Step 1: Scrape website --------------------------------------
    scraped = scrape_website(website)
    found_emails  = scraped["emails"]
    found_names   = scraped["founder_names"]

    # -- Step 2: Find founder name if still unknown ------------------
    best_name = current_name
    if not best_name or best_name in ("Founder", "Hiring Manager"):
        if found_names:
            best_name = found_names[0]
        else:
            # Try DuckDuckGo
            time.sleep(DELAY_BETWEEN)
            ddg_name = search_founder_name(company, website)
            if ddg_name:
                best_name = ddg_name

    lead["name"] = best_name or current_name

    # -- Step 3: Choose best email -----------------------------------
    name_parts = best_name.split() if best_name and best_name not in ("Founder","Hiring Manager") else []
    first = name_parts[0] if name_parts else ""
    last  = name_parts[1] if len(name_parts) > 1 else ""

    # Prefer scraped real emails from website
    best_email = None
    if found_emails:
        # Filter to domain-matching emails (founder's own domain)
        domain_emails = [e for e in found_emails if domain in e or e.endswith(f"@{domain}")]
        if domain_emails:
            # Prefer personal emails over generic
            personal = [e for e in domain_emails if not re.search(r'(info|hello|support|help|contact|press|media|legal|privacy|team)@', e)]
            best_email = personal[0] if personal else domain_emails[0]
        else:
            # Keep any email found (may be a work email on different domain)
            best_email = list(found_emails)[0]

    if not best_email:
        # Generate patterns and pick top one
        patterns = generate_email_patterns(first, last, domain)
        best_email = patterns[0][0] if patterns else f"founders@{domain}"

    # -- Step 4: Validate email --------------------------------------
    if SMTP_VERIFY:
        valid_status = validate_email_smtp(best_email)
    else:
        valid_status = validate_email_quick(best_email)

    lead["email"]          = best_email
    lead["email_status"]   = valid_status
    lead["email_patterns"] = " | ".join(e for e, _ in generate_email_patterns(first, last, domain)[:4])
    lead["enrich_status"]  = "done"

    # Update context with what we found
    extra = []
    if found_names:
        extra.append(f"Team: {', '.join(found_names[:2])}")
    if scraped["pages_hit"]:
        extra.append(f"Pages scraped: {scraped['pages_hit']}")
    if extra:
        lead["context"] = lead.get("context","") + " | " + " | ".join(extra)

    logging.info(f"  - Name: {lead['name']} | Email: {best_email} | Status: {valid_status}")
    return lead


# --- 6. BATCH RUNNER ----------------------------------------------------------

def load_leads() -> list[dict]:
    if not os.path.exists(LEADS_FILE):
        logging.error(f"{LEADS_FILE} not found. Run find_leads.py first.")
        return []
    with open(LEADS_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_leads(leads: list[dict]):
    if not leads:
        return
    # Merge in new columns
    all_keys = list(leads[0].keys())
    for new_col in ["email_status", "email_patterns", "enrich_status"]:
        if new_col not in all_keys:
            all_keys.append(new_col)
    with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)


def main():
    args = sys.argv[1:]
    recheck = "--recheck" in args
    limit   = 0
    if "--limit" in args:
        idx = args.index("--limit")
        try:
            limit = int(args[idx + 1])
        except (IndexError, ValueError):
            limit = 20

    leads = load_leads()
    if not leads:
        return

    # Select which leads to enrich
    to_enrich = []
    for i, lead in enumerate(leads):
        if lead.get("sent","").upper() == "YES":
            continue
        already_done = lead.get("enrich_status") == "done"
        if already_done and not recheck:
            continue
        to_enrich.append(i)

    if limit:
        to_enrich = to_enrich[:limit]

    if not to_enrich:
        logging.info("All leads already enriched. Use --recheck to re-process.")
        return

    logging.info(f"Enriching {len(to_enrich)} leads...")
    print(f"\n{'='*60}")
    print(f"  ENRICHER - processing {len(to_enrich)} leads")
    print(f"{'='*60}\n")

    done = 0
    errors = 0

    for idx in to_enrich:
        try:
            leads[idx] = enrich_lead(leads[idx])
            done += 1
        except Exception as e:
            logging.error(f"Error on {leads[idx].get('company','?')}: {e}")
            leads[idx]["enrich_status"] = f"error: {str(e)[:60]}"
            errors += 1

        # Save after every 5 to avoid losing progress
        if done % 5 == 0:
            save_leads(leads)

        time.sleep(DELAY_BETWEEN)

    save_leads(leads)

    print(f"\n{'='*60}")
    print(f"  Done: {done} enriched, {errors} errors")
    print(f"  Saved to: {LEADS_FILE}")
    print(f"\n  Ready to send? Run:")
    print(f"    python mailer.py --dry-run   - preview")
    print(f"    python mailer.py --now       - send")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
