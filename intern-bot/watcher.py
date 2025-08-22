# == Intern Bot ==
# Monitors Intern List (SWE, Data Analysis, ML/AI, PM) and the SimplifyJobs Summer 2026 GitHub list.
# Runs on a GitHub Actions cron (every 15 minutes) and sends SMS via Twilio to multiple recipients.
# Dedupe by (company|title|url) persisted in seen.json.

import os, re, json, time, hashlib, requests, sys
from typing import List, Dict, Optional
from bs4 import BeautifulSoup

# ------------ Config & constants ------------
IL_BASE = "https://www.intern-list.com"
GITHUB_RAW_2026 = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/main/README.md"

REQUEST_TIMEOUT = 45
RETRIES = 2
BACKOFF_SECS = 3

PRIMARY_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SECONDARY_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

BASE_HEADERS = {
    "User-Agent": PRIMARY_UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

SEEN_PATH = os.path.join(os.path.dirname(__file__), "seen.json")

# ------------ Logging ------------
def log(*args):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts} UTC]", *args, flush=True)

# ------------ State ------------
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

# ------------ Notify (Twilio) ------------
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

# ------------ HTTP with retries ------------
def fetch(url: str, headers: Optional[Dict] = None) -> str:
    """GET with retries; rotates UA on last try; raises on failure."""
    last_err = None
    h = dict(BASE_HEADERS)
    if headers: h.update(headers)
    for attempt in range(RETRIES + 1):
        try:
            if attempt == RETRIES:
                h["User-Agent"] = SECONDARY_UA
                h["Referer"] = "https://www.bing.com/"
            r = requests.get(url, headers=h, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            log(f"WARN fetch attempt {attempt+1}/{RETRIES+1} failed for {url}:", e)
            time.sleep(BACKOFF_SECS)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")

# ------------ Normalize & categorize ------------
def normalize_item(source: str, category: str, title: str, url: str, company: str = "", location: str = "", meta: Dict = None) -> Dict:
    out = {
        "source": source,
        "category": category,
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
    }
    if meta: out.update(meta)
    return out

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

# ------------ Intern-List (4 categories) ------------
def _extract_links_by_prefix(html: str, path_prefix: str, category: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    # listing/detail links that live under the category path
    for a in soup.select(f"a[href^='/{path_prefix}/']"):
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if not title or not href:
            continue
        url = IL_BASE + href if href.startswith("/") else href
        # attempt nearby company extraction
        company = ""
        parent = a.find_parent()
        if parent:
            txt = parent.get_text(" ", strip=True)
            m = re.search(r"\b(?:at|@)\s+([A-Za-z0-9.&' -]{2,})", txt)
            if m: company = m.group(1)
        items.append(normalize_item("Intern List", category, title, url, company))
    return items

def parse_intern_list_swe():
    html = fetch(f"{IL_BASE}/swe-intern-list")
    return _extract_links_by_prefix(html, "swe-intern-list", "Software Engineering")

def parse_intern_list_da():
    html = fetch(f"{IL_BASE}/da-intern-list")
    return _extract_links_by_prefix(html, "da-intern-list", "Data Analysis")

def _discover_ml_slug() -> Optional[str]:
    """Discover ML/AI category path dynamically from the homepage/nav."""
    try:
        html = fetch(IL_BASE + "/")
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            txt = a.get_text(" ", strip=True).lower()
            href = a["href"]
            if any(k in txt for k in ["machine learning", "ml", "ai"]) and href.startswith("/"):
                return href.strip("/").rstrip("/")
    except Exception as e:
        log("WARN _discover_ml_slug:", e)
    return None

def parse_intern_list_ml():
    slug = _discover_ml_slug()
    candidates = [slug] if slug else []
    # Backward-compatible fallbacks
    candidates += [
        "data-science-internships", "ml-intern-list", "ai-intern-list",
        "machine-learning-internships", "data-science-intern-list"
    ]
    tried = set()
    for s in candidates:
        if not s or s in tried: 
            continue
        tried.add(s)
        try:
            html = fetch(f"{IL_BASE}/{s}")
            return _extract_links_by_prefix(html, s, "Machine Learning & AI")
        except Exception as e:
            log(f"WARN ML/AI slug {s} failed:", e)
    log("ERROR Intern List ML/AI: no working slug found")
    return []

def parse_intern_list_pm():
    html = fetch(f"{IL_BASE}/pm-intern-list")
    return _extract_links_by_prefix(html, "pm-intern-list", "Product Management")

# ------------ SimplifyJobs / Summer 2026 GitHub list ------------
def parse_simplify_2026():
    """
    Parses the Summer2026-Internships README Markdown table.
    Columns are usually: Company | Role | Location | Application/Link | (sometimes Date/Notes)
    We extract company, role (title), location, link, and date if present.
    """
    md = fetch(GITHUB_RAW_2026, headers={"Accept": "text/plain"})
    items = []
    for line in md.splitlines():
        line = line.rstrip()
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 4:
            continue
        # Skip header separator rows
        if cols[0].lower() in ("company", "---", "—"):
            continue

        # Company / Role: strip markdown links
        company = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cols[0])
        title   = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cols[1])
        location = cols[2]

        # Link column may contain one or more markdown links; take the first URL
        url = ""
        m = re.search(r"\((https?://[^\)]+)\)", cols[3])
        if m:
            url = m.group(1)

        # Optional date column (some rows have it as 4th or 5th col)
        posted = None
        if len(cols) >= 5:
            # free-form; keep as-is if it looks like a date or relative text
            maybe = cols[4]
            if maybe and maybe.lower() not in ("notes",):
                posted = maybe

        if url:
            items.append(
                normalize_item(
                    "SimplifyJobs 2026",
                    infer_category(title),  # infer to route SW/DA/ML/PM
                    title,
                    url,
                    company,
                    location,
                    meta={"posted": posted} if posted else None,
                )
            )
    return items

# ------------ Optional: Simplify site (best-effort; may block) ------------
def parse_simplify_site():
    """Scrape simplify.jobs/internships best-effort; safe to disable if noisy."""
    try:
        html = fetch("https://simplify.jobs/internships", headers={
            "Referer": "https://simplify.jobs/",
            "User-Agent": PRIMARY_UA,
            "Accept-Language": "en-US,en;q=0.9",
        })
    except Exception as e:
        log("ERROR Simplify site fetch:", e)
        return []
    soup = BeautifulSoup(html, "lxml")
    items = []
    for a in soup.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        href = a.get("href","")
        if not title or not href:
            continue
        url = "https://simplify.jobs" + href if href.startswith("/") else href
        company = ""
        parent = a.find_parent()
        if parent:
            txt = parent.get_text(" ", strip=True)
            m = re.search(r"^([A-Za-z0-9.&' -]{2,})\s+[•–-]\s+", txt)
            if m: company = m.group(1)
        items.append(normalize_item("Simplify", infer_category(title), title, url, company))
    return items

# ------------ main ------------
def main():
    log("START run")
    seen = load_seen()
    all_items: List[Dict] = []

    # Each parser isolated with error logging; one failing won’t stop others
    parsers = [
        ("Intern List — SWE", parse_intern_list_swe),
        ("Intern List — Data Analysis", parse_intern_list_da),
        ("Intern List — ML/AI", parse_intern_list_ml),
        ("Intern List — Product Management", parse_intern_list_pm),
        ("SimplifyJobs 2026 GitHub", parse_simplify_2026),
        # Optionally include the Simplify site (can 403 sometimes)
        # ("Simplify site", parse_simplify_site),
    ]

    for name, fn in parsers:
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

    # Compose compact SMS (cap to 6 lines)
    batch = new[:6]
    lines = [
        "• [" + (i.get("category") or "?") + "] [" + (i.get("source") or "?") + "] "
        + (i.get("company", "")[:40] or "Unknown Company") + " — " + (i.get("title", "")[:70] or "Role")
        + ((" — " + i.get("location", "")) if i.get("location") else "")
        + ((" — " + str(i.get("posted"))) if i.get("posted") else "")
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
