# == Intern Bot ==
# Monitors Intern List (SWE, Data Analysis, ML/AI, PM), Simplify internships, and PittCSC.
# Runs on a GitHub Actions cron (every 15 minutes) and sends SMS via Twilio to multiple recipients.
# Dedupe by (company|title|url) persisted in seen.json.

import os, re, json, time, hashlib, requests, sys
from typing import List, Dict
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "intern-bot/1.0 (+https://github.com/)",
    "Accept": "text/html,application/xhtml+xml",
}
SEEN_PATH = os.path.join(os.path.dirname(__file__), "seen.json")  # store next to this file
REQUEST_TIMEOUT = 45
RETRIES = 2
BACKOFF_SECS = 3

# ---------- small logging helper ----------
def log(*args):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts} UTC]", *args, flush=True)

# ---------- state ----------
def load_seen():
    try:
        if os.path.exists(SEEN_PATH):
            return set(json.load(open(SEEN_PATH)))
    except Exception as e:
        log("WARN load_seen:", e)
    return set()

def save_seen(seen):
    try:
        with open(SEEN_PATH, "w") as f:
            json.dump(sorted(list(seen)), f)
    except Exception as e:
        log("WARN save_seen:", e)

def sha(item: Dict) -> str:
    key = f"{item.get('company','').strip()}|{item.get('title','').strip()}|{item.get('url','').strip()}"
    return hashlib.sha1(key.encode()).hexdigest()

# ---------- notification (Twilio) ----------
def twilio_send(body: str):
    sid = os.getenv("TWILIO_SID"); tok = os.getenv("TWILIO_TOKEN")
    frm = os.getenv("TWILIO_FROM"); to_list = os.getenv("SMS_TO_LIST","").split(",")
    to_list = [t.strip() for t in to_list if t.strip()]
    if not all([sid, tok, frm]) or not to_list:
        log("INFO Twilio not configured or no recipients; skipping SMS")
        return
    for to in to_list:
        try:
            r = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, tok),
                data={"From": frm, "To": to, "Body": body[:1500]},
                timeout=REQUEST_TIMEOUT,
            )
            log("INFO Twilio send", to, r.status_code)
        except Exception as e:
            log("ERROR Twilio send", to, e)

# ---------- HTTP fetch with retries ----------
def fetch(url: str) -> str:
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            log(f"WARN fetch attempt {attempt+1}/{RETRIES+1} failed for {url}:", e)
            time.sleep(BACKOFF_SECS)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")

# ---------- helpers to normalize ----------
def normalize_item(source: str, category: str, title: str, url: str, company: str = "", location: str = "") -> Dict:
    return {
        "source": source,
        "category": category,
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
    }

def infer_category(title: str, default: str = "Software Engineering") -> str:
    t = (title or "").lower()
    if any(k in t for k in ["product manager", "apm", "product management", "pm intern", "product intern"]):
        return "Product Management"
    if any(k in t for k in ["data analyst", "analytics", "business analyst", "data analysis"]):
        return "Data Analysis"
    if any(k in t for k in ["machine learning", " ml", "ml ", " ai", "artificial intelligence", "deep learning", "research scientist"]):
        return "Machine Learning & AI"
    if any(k in t for k in ["software engineer", "swe", "backend", "front end", "frontend", "full stack", "mobile", "android", "ios"]):
        return "Software Engineering"
    return default

# ---------- Intern List parsers ----------
IL_BASE = "https://www.intern-list.com"

def _extract_links_by_prefix(html: str, path_prefix: str, category: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    # Any anchor that links into the given prefix is considered a listing
    for a in soup.select(f"a[href^='/{path_prefix}/']"):
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if not title or not href:
            continue
        url = IL_BASE + href if href.startswith("/") else href
        # Try to infer a company from nearby text
        company = ""
        parent = a.find_parent()
        if parent:
            txt = parent.get_text(" ", strip=True)
            m = re.search(r"\b(?:at|@)\s+([A-Za-z0-9.&' -]{2,})", txt)
            if m:
                company = m.group(1)
        items.append(normalize_item("Intern List", category, title, url, company))
    return items

def parse_intern_list_swe():
    html = fetch(f"{IL_BASE}/swe-intern-list")
    return _extract_links_by_prefix(html, "swe-intern-list", "Software Engineering")

def parse_intern_list_da():
    html = fetch(f"{IL_BASE}/da-intern-list")
    return _extract_links_by_prefix(html, "da-intern-list", "Data Analysis")

def parse_intern_list_ml():
    # ML & AI lives under data-science-internships detail pages
    html = fetch(f"{IL_BASE}/data-science-internships")
    return _extract_links_by_prefix(html, "data-science-internships", "Machine Learning & AI")

def parse_intern_list_pm():
    html = fetch(f"{IL_BASE}/pm-intern-list")
    return _extract_links_by_prefix(html, "pm-intern-list", "Product Management")

# ---------- PittCSC (raw Markdown table) ----------
def parse_pittcsc():
    raw = fetch("https://raw.githubusercontent.com/pittcsc/Summer2024-Internships/dev/README.md")
    items = []
    for line in raw.splitlines():
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 4 or cols[0] in ("Company", "—", "-"):
            continue
        company = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cols[0])
        title = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cols[1])
        loc = cols[2]
        m = re.search(r"\((https?://[^\)]+)\)", cols[3])
        url = m.group(1) if m else ""
        if url:
            items.append(normalize_item("PittCSC", infer_category(title), title, url, company, loc))
    return items

# ---------- Simplify ----------
def parse_simplify():
    """
    Scrape simplify.jobs/internships for new internships.
    Very light-weight parser: collect anchor tags pointing to job pages (/jobs/...).
    """
    html = fetch("https://simplify.jobs/internships")
    soup = BeautifulSoup(html, "lxml")
    items = []
    for a in soup.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if not title or not href:
            continue
        url = "https://simplify.jobs" + href if href.startswith("/") else href
        # best-effort company extraction from surrounding card text
        company = ""
        parent = a.find_parent()
        if parent:
            txt = parent.get_text(" ", strip=True)
            # Try “Company • Role” or “Company – …”
            m = re.search(r"^([A-Za-z0-9.&' -]{2,})\s+[•–-]\s+", txt)
            if m:
                company = m.group(1)
        items.append(normalize_item("Simplify", infer_category(title), title, url, company))
    return items

# ---------- main ----------
def main():
    log("START run")
    seen = load_seen()
    all_items: List[Dict] = []

    # Each parser isolated with error logging; failure in one won’t stop others
    for name, fn in [
        ("Intern List SWE", parse_intern_list_swe),
        ("Intern List Data Analysis", parse_intern_list_da),
        ("Intern List ML/AI", parse_intern_list_ml),
        ("Intern List PM", parse_intern_list_pm),
        ("PittCSC", parse_pittcsc),
        ("Simplify", parse_simplify),
    ]:
        try:
            items = fn()
            log(f"INFO parsed {name}: {len(items)} items")
            all_items += items
        except Exception as e:
            log(f"ERROR parser {name}:", e)

    # De-dupe and find new
    new = []
    for it in all_items:
        key = sha(it)
        if key not in seen:
            seen.add(key)
            new.append(it)

    if not new:
        log("DONE no new items")
        return

    # Compose SMS (cap to 6 lines to avoid long messages)
    batch = new[:6]
    lines = [
        "• [" + (i.get("category") or "?") + "] [" + (i.get("source") or "?") + "] "
        + (i.get("company", "")[:40] or "Unknown Company") + " — " + (i.get("title", "")[:70] or "Role")
        + ((" — " + i.get("location", "")) if i.get("location") else "")
        + "\n" + i["url"]
        for i in batch
    ]
    tail = "" if len(new) <= 6 else f"\n(+{len(new)-6} more new roles)"
    body = "New internships:\n" + "\n".join(lines) + tail + "\nReply STOP to opt out."

    twilio_send(body)
    save_seen(seen)
    log(f"DONE notified {len(batch)} of {len(new)} new items")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL run crashed:", e)
        sys.exit(1)
