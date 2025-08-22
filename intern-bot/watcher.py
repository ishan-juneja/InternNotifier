# == Intern Bot ==
# Monitors Intern-List search tabs (SWE, Data Analysis, ML/AI, PM) + SimplifyJobs Summer 2026 README (Age=0d).
# Runs on GitHub Actions (every 15 minutes). Sends SMS (Twilio) + Email (SMTP).
# Dedupe by (company|title|url). Also: one-time "no new internships" ping per dry spell.

import os, re, json, time, hashlib, requests, sys, smtplib
from typing import List, Dict, Optional, Tuple, Union
from email.mime.text import MIMEText
from bs4 import BeautifulSoup

# ------------ Config & constants ------------
IL_BASE = "https://www.intern-list.com"
IL_TABS: List[Tuple[str, str]] = [
    ("Software Engineering", f"{IL_BASE}/?k=swe"),
    ("Data Analysis",        f"{IL_BASE}/?k=da"),
    ("Machine Learning & AI",f"{IL_BASE}/?k=aiml"),
    ("Product Management",   f"{IL_BASE}/?k=pm"),
]

# SimplifyJobs (PittCSC successor) — we’ll probe owners+branches and use raw/plain fallback
GITHUB_OWNERS   = ["SimplifyJobs", "pittcsc"]
GITHUB_REPOS    = ["Summer2026-Internships"]
GITHUB_BRANCHES = ["dev", "main", "master"]

# Headings inside README (emoji/punctuation ignored; case-insensitive)
SIMPLIFY_SECTIONS = {
    "Product Management":      "product management internship roles",
    "Software Engineering":    "software engineering internship roles",
    "Machine Learning & AI":   "data science ai & machine learning internship roles",
}

REQUEST_TIMEOUT = 45
RETRIES = 2
BACKOFF_SECS = 3

PRIMARY_UA   = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SECONDARY_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
BASE_HEADERS = {
    "User-Agent": PRIMARY_UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# Store state next to this script
SEEN_PATH = os.path.join(os.path.dirname(__file__), "seen.json")

# Feature flags via envs
TEST_NOTIFY = os.getenv("TEST_NOTIFY") == "1"  # Force a “new item” to test notifications

# ------------ Logging ------------
def log(*args):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts} UTC]", *args, flush=True)

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

# ------------ State (seen + dry-spell ping) ------------
def _init_state() -> Dict[str, Union[List[str], bool]]:
    """
    State format (new):
      {
        "seen": [ "<sha1>", ... ],
        "no_new_notified": false
      }
    Backwards compatible with old list-only format.
    """
    if not os.path.exists(SEEN_PATH):
        return {"seen": [], "no_new_notified": False}
    try:
        data = json.load(open(SEEN_PATH))
        # old format: list
        if isinstance(data, list):
            return {"seen": data, "no_new_notified": False}
        # new format
        if isinstance(data, dict):
            data.setdefault("seen", [])
            data.setdefault("no_new_notified", False)
            return data
    except Exception as e:
        log("WARN load_seen:", e)
    return {"seen": [], "no_new_notified": False}

def load_seen_set_and_flag():
    state = _init_state()
    return set(state["seen"]), bool(state.get("no_new_notified", False)), state

def save_state(seen_set: set, no_new_notified: bool):
    state = {"seen": sorted(list(seen_set)), "no_new_notified": bool(no_new_notified)}
    try:
        with open(SEEN_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log("WARN save_seen:", e)

def sha(item: Dict) -> str:
    # Strong dedupe; switch to URL-only by replacing key with item['url'] if you prefer
    key = f"{item.get('company','').strip()}|{item.get('title','').strip()}|{item.get('url','').strip()}"
    return hashlib.sha1(key.encode()).hexdigest()

# ------------ Notify (Twilio + Email) ------------
def twilio_send(body: str):
    sid = os.getenv("TWILIO_SID"); tok = os.getenv("TWILIO_TOKEN")
    frm = os.getenv("TWILIO_FROM"); to_list = [t.strip() for t in os.getenv("SMS_TO_LIST","").split(",") if t.strip()]
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

def email_send(subject: str, body: str):
    host = os.getenv("SMTP_HOST"); port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER"); pwd = os.getenv("SMTP_PASS")
    to_list = [t.strip() for t in os.getenv("EMAIL_TO_LIST","").split(",") if t.strip()]
    if not all([host, port, user, pwd]) or not to_list:
        log("INFO SMTP not configured or no recipients; skipping email")
        return
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo(); s.starttls(); s.login(user, pwd)
            for to in to_list:
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"] = subject
                msg["From"] = user
                msg["To"] = to
                s.sendmail(user, [to], msg.as_string())
                log("INFO Email sent to", to)
    except Exception as e:
        log("ERROR email send:", e)

def twilio_self_check():
    sid = os.getenv("TWILIO_SID"); tok = os.getenv("TWILIO_TOKEN")
    frm = os.getenv("TWILIO_FROM"); to_list = [t.strip() for t in os.getenv("SMS_TO_LIST","").split(",") if t.strip()]
    log("INFO Twilio env present?:", bool(sid and tok and frm), "recipients:", len(to_list))
    if not (sid and tok):
        return False
    try:
        r = requests.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                         auth=(sid, tok), timeout=20)
        log("INFO Twilio auth probe:", r.status_code)
        return r.status_code == 200
    except Exception as e:
        log("ERROR Twilio auth probe failed:", e)
        return False

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
    Parse Intern-List search pages. Strategy:
      A) Internal detail links with 'intern' in text or URL.
      B) If none, external links that contain 'intern'.
    """
    soup = BeautifulSoup(html, "lxml")
    items: List[Dict] = []

    # A) internal postings
    for a in soup.select("a[href]"):
        href = a.get("href",""); text = a.get_text(strip=True)
        if not href or not text:
            continue
        lower = href.lower()
        if any(x in lower for x in ["#","/privacy","/terms","mailto:", "javascript:","/sitemap"]):
            continue
        is_internal = lower.startswith("/") and lower not in ["/", "/?k=swe","/?k=da","/?k=aiml","/?k=pm"]
        looks_like_post = ("intern" in text.lower()) or ("intern" in lower)
        if is_internal and looks_like_post:
            url = _absolute(href)
            company = ""
            parent = a.find_parent()
            if parent:
                txt = parent.get_text(" ", strip=True)
                m = re.search(r"\b(?:at|@)\s+([A-Za-z0-9.&' -]{2,})", txt)
                if m: company = m.group(1)
            items.append(normalize_item("Intern List", category, text, url, company))

    # B) external postings fallback
    if not items:
        for a in soup.select("a[href^='http']"):
            href = a.get("href",""); text = a.get_text(strip=True)
            if not href or not text:
                continue
            if "intern" not in (text.lower() + " " + href.lower()):
                continue
            if href.startswith(IL_BASE) and href.endswith(("/?k=swe","/?k=da","/?k=aiml","/?k=pm")):
                continue
            items.append(normalize_item("Intern List", category, text, href))

    # de-dupe by URL within page
    uniq = {}
    for it in items:
        uniq[it["url"]] = it
    return list(uniq.values())

def parse_intern_list_tab(category: str, url: str) -> List[Dict]:
    html = fetch(url)
    return _extract_cards_from_search(html, category)

# ------------ SimplifyJobs / Summer 2026 README (Age=0d) ------------
def _fetch_2026_readme() -> str:
    """Fetch README text: try raw first, then ?plain=1; probe multiple owners/branches."""
    # raw first
    for owner in GITHUB_OWNERS:
        for repo in GITHUB_REPOS:
            for br in GITHUB_BRANCHES:
                url = f"https://raw.githubusercontent.com/{owner}/{repo}/{br}/README.md"
                try:
                    md = fetch(url, headers={"Accept":"text/plain"})
                    if md:
                        log("INFO 2026 README (raw)", owner, repo, br, "len:", len(md))
                        return md
                except Exception as e:
                    log("WARN 2026 raw miss:", url, e)
    # ?plain=1 fallback
    for owner in GITHUB_OWNERS:
        for repo in GITHUB_REPOS:
            for br in GITHUB_BRANCHES:
                url = f"https://github.com/{owner}/{repo}/blob/{br}/README.md?plain=1"
                try:
                    md = fetch(url, headers={"Accept":"text/plain"})
                    if md:
                        log("INFO 2026 README (plain)", owner, repo, br, "len:", len(md))
                        return md
                except Exception as e:
                    log("WARN 2026 plain miss:", url, e)
    return ""

def _norm(s: str) -> str:
    """Lowercase, strip emojis/punctuation that often appear in headings."""
    s = (s or "").lower()
    s = re.sub(r"[\u2600-\u27ff\U0001F000-\U0001FAFF]", "", s)  # emojis/symbols
    s = re.sub(r"[^\w\s&/+-]", " ", s)  # keep word chars and a few symbols
    return re.sub(r"\s+", " ", s).strip()

def _iter_section_lines(md: str, section_phrase: str):
    """
    Yield lines within the README that belong to the heading whose normalized text
    contains `section_phrase`. Stops when the next '## ' heading appears.
    """
    target = _norm(section_phrase)
    lines = md.splitlines()
    in_section = False
    for i, line in enumerate(lines):
        if line.startswith("#"):
            if line.startswith("## "):
                head_norm = _norm(line.lstrip("# ").strip())
                if target in head_norm:
                    in_section = True
                    continue
                elif in_section:
                    break
        if in_section:
            yield line

def _parse_markdown_table_lines(section_lines):
    """From a stream of lines inside a section, extract markdown table rows as lists of columns."""
    rows = []
    for line in section_lines:
        if not line.startswith("|"):
            continue
        # skip separators like |---|---|
        if re.fullmatch(r"\|\s*[-:\s]+\|.*", line):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append((cols, line))
    return rows

def _md_link_text(md_cell: str) -> str:
    return re.sub(r"\[(.*?)\]\(.*?\)", r"\1", md_cell or "")

def _md_first_url(md_cell: str) -> str:
    m = re.search(r"\((https?://[^\)]+)\)", md_cell or "")
    return m.group(1) if m else ""

def parse_simplify_2026_age0() -> List[Dict]:
    """
    Parse ONLY rows with Age '0d' from 3 Simplify README sections:
      - Product Management Internship Roles
      - Software Engineering Internship Roles
      - Data Science, AI & Machine Learning Internship Roles
    """
    md = _fetch_2026_readme()
    if not md:
        log("ERROR Could not fetch 2026 README from GitHub")
        return []

    all_items = []
    for category, section_name in SIMPLIFY_SECTIONS.items():
        try:
            section_lines = list(_iter_section_lines(md, section_name))
            rows = _parse_markdown_table_lines(section_lines)
            if not rows:
                log(f"WARN Simplify 2026: no table rows found in section: {category}")
                continue

            # detect Age column (if present)
            age_idx = None
            header = rows[0][0] if rows else []
            if header and any(re.match(r"(?i)^age$", h) for h in header):
                for idx, h in enumerate(header):
                    if re.match(r"(?i)^age$", h):
                        age_idx = idx
                        break

            for cols, _raw in rows:
                # skip header row
                if any(re.match(r"(?i)^company$", c) for c in cols):
                    continue
                if len(cols) < 4:
                    continue

                # '0d' filter
                is_0d = False
                if age_idx is not None and age_idx < len(cols):
                    is_0d = (_norm(cols[age_idx]) == "0d")
                else:
                    is_0d = any(_norm(c) == "0d" or " 0d" in _norm(c) for c in cols)

                if not is_0d:
                    continue

                company  = _md_link_text(cols[0])
                title    = _md_link_text(cols[1]) if len(cols) > 1 else ""
                location = cols[2] if len(cols) > 2 else ""
                url      = _md_first_url(cols[3]) if len(cols) > 3 else ""

                posted = None
                for extra in cols[4:]:
                    if "0d" in _norm(extra):
                        posted = "0d"
                        break

                if url:
                    all_items.append(
                        normalize_item(
                            "SimplifyJobs 2026",
                            category,
                            title,
                            url,
                            company,
                            location,
                            meta={"posted": posted} if posted else None,
                        )
                    )
        except Exception as e:
            log(f"ERROR Simplify 2026 section parse ({category}):", e)

    log(f"INFO Simplify 2026 age=0d total: {len(all_items)}")
    return all_items

# ------------ main ------------
def main():
    log("START run")
    log("INFO seen_path =", SEEN_PATH)
    twilio_self_check()
    seen_set, no_new_notified, _raw_state = load_seen_set_and_flag()
    log("INFO seen size =", len(seen_set))
    all_items: List[Dict] = []

    # Intern-List tabs
    for cat, url in IL_TABS:
        try:
            items = parse_intern_list_tab(cat, url)
            log(f"INFO parsed Intern List — {cat}: {len(items)} items")
            all_items += items
        except Exception as e:
            log(f"ERROR parser Intern List — {cat}:", e)

    # SimplifyJobs 2026 (only Age=0d)
    try:
        sj = parse_simplify_2026_age0()
        log(f"INFO parsed SimplifyJobs 2026 GitHub (0d): {len(sj)} items")
        all_items += sj
    except Exception as e:
        log("ERROR parser SimplifyJobs 2026 GitHub:", e)

    # Optional: inject a test item to validate notifications
    if TEST_NOTIFY:
        all_items.append(normalize_item("Intern Bot", "Test", "Test Notification", "https://example.com", "DemoCo"))

    # De-dupe vs seen
    new = []
    for it in all_items:
        key = sha(it)
        if key not in seen_set:
            seen_set.add(key)
            new.append(it)

    if not new:
        # one-time "no new internships" ping
        if not no_new_notified:
            body = "No new internships found right now. I’ll notify you as soon as new ones appear."
            twilio_send(body)
            email_send("Internship alerts — no new roles yet", body)
            no_new_notified = True
            save_state(seen_set, no_new_notified)
            log("DONE sent one-time no-new notice")
        else:
            log("DONE no new items")
        return

    # If we found new items, clear the dry-spell flag
    no_new_notified = False

    # Compose compact alert (cap to 6 lines)
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

    # Send
    twilio_send(body)
    email_send("Internship alerts — new roles", body)

    # Persist state
    save_state(seen_set, no_new_notified)
    log(f"DONE notified {len(batch)} of {len(new)} new items; seen now {len(seen_set)}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL run crashed:", e)
        sys.exit(1)
