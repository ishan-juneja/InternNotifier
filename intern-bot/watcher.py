# == Intern Bot ==
# Monitors Intern List search tabs (SWE, Data Analysis, ML/AI, PM) + SimplifyJobs Summer 2026 list.
# Runs on GitHub Actions (every 15 minutes) and sends SMS via Twilio to multiple recipients.
# Dedupe by (company|title|url) persisted in seen.json.

import os, re, json, time, hashlib, requests, sys
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup

# ------------ Config & constants ------------
IL_BASE = "https://www.intern-list.com"
IL_TABS: List[Tuple[str, str]] = [
    ("Software Engineering", f"{IL_BASE}/?k=swe"),
    ("Data Analysis",        f"{IL_BASE}/?k=da"),
    ("Machine Learning & AI",f"{IL_BASE}/?k=aiml"),
    ("Product Management",   f"{IL_BASE}/?k=pm"),
]

GITHUB_2026_RAW = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"

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

# ------------ Intern-List search tab scraper ------------
def _absolute(url: str) -> str:
    if not url: return ""
    if url.startswith("http://") or url.startswith("https://"): return url
    if url.startswith("/"): return IL_BASE + url
    return IL_BASE + "/" + url

def _extract_cards_from_search(html: str, category: str) -> List[Dict]:
    """
    The search pages render lists of relevant posts. We collect anchor links that look like postings.
    Heuristics:
      - Prefer links under result sections/cards
      - Skip obvious nav/footer/social links
      - Title = anchor text; company inferred from nearby text
    """
    soup = BeautifulSoup(html, "lxml")
    items: List[Dict] = []

    # Strategy A: obvious result anchors with internal detail paths (/...-intern-...)
    for a in soup.select("a[href]"):
        href = a.get("href","")
        text = a.get_text(strip=True)
        if not href or not text: 
            continue
        # Exclude nav/footer/tracking links
        lower = href.lower()
        if any(x in lower for x in ["#","/privacy","/terms","mailto:", "javascript:","/sitemap"]):
            continue
        # Likely posting if internal and not a top-level page
        is_internal = lower.startswith("/") and not lower in ["/", "/?k=swe","/?k=da","/?k=aiml","/?k=pm"]
        looks_like_post = ("intern" in text.lower()) or ("intern" in lower)
        if is_internal and looks_like_post:
            url = _absolute(href)
            # infer company from the parent card's text
            company = ""
            parent = a.find_parent()
            if parent:
                txt = parent.get_text(" ", strip=True)
                m = re.search(r"\b(?:at|@)\s+([A-Za-z0-9.&' -]{2,})", txt)
                if m: company = m.group(1)
            items.append(normalize_item("Intern List", category, text, url, company))

    # Strategy B: if nothing found yet, also capture external application links on the page
    if not items:
        for a in soup.select("a[href^='http']"):
            href = a.get("href","")
            text = a.get_text(strip=True)
            if not href or not text: 
                continue
            if "intern" not in (text.lower() + " " + href.lower()):
                continue
            # Avoid self-links to the search page itself
            if href.startswith(IL_BASE) and href.endswith(("/?k=swe","/?k=da","/?k=aiml","/?k=pm")):
                continue
            items.append(normalize_item("Intern List", category, text, href))

    # De-dupe by URL within this page
    uniq = {}
    for it in items:
        uniq[it["url"]] = it
    return list(uniq.values())

def parse_intern_list_tab(category: str, url: str) -> List[Dict]:
    html = fetch(url)
    items = _extract_cards_from_search(html, category)
    return items

# ------------ SimplifyJobs / Summer 2026 GitHub list ------------
def parse_simplify_2026():
    """
    Parses the Summer 2026 internships list (successor to PittCSC) from GitHub (dev branch).
    Columns: Company | Role | Location | Application/Link | (optional Date/Notes)
    """
    md = fetch(GITHUB_2026_RAW, headers={"Accept": "text/plain"})
    items = []
    for line in md.splitlines():
        line = line.rstrip()
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 4:
            continue
        if cols[0].lower() in ("company","---","—"):
            continue

        company  = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cols[0])
        title    = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cols[1])
        location = cols[2]
        m = re.search(r"\((https?://[^\)]+)\)", cols[3])
        url = m.group(1) if m else ""

        posted = cols[4] if len(cols) >= 5 and cols[4] and cols[4].lower() != "notes" else None

        if url:
            items.append(
                normalize_item(
                    "SimplifyJobs 2026",
                    infer_category(title),
                    title,
                    url,
                    company,
                    location,
                    meta={"posted": posted} if posted else None,
                )
            )
    return items

# ------------ main ------------
def main():
    log("START run")
    seen = load_seen()
    all_items: List[Dict] = []

    # Intern-List tabs (your requested sources)
    for cat, url in IL_TABS:
        try:
            items = parse_intern_list_tab(cat, url)
            log(f"INFO parsed Intern List — {cat}: {len(items)} items")
            all_items += items
        except Exception as e:
            log(f"ERROR parser Intern List — {cat}:", e)

    # SimplifyJobs 2026 GitHub list
    try:
        sj = parse_simplify_2026()
        log(f"INFO parsed SimplifyJobs 2026 GitHub: {len(sj)} items")
        all_items += sj
    except Exception as e:
        log("ERROR parser SimplifyJobs 2026 GitHub:", e)

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
