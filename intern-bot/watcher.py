# == Intern Bot ==
# Monitors Intern List search tabs (SWE, Data Analysis, ML/AI, PM) + SimplifyJobs Summer 2026 sections (Age=0d).
# Runs on GitHub Actions (every 15 minutes). Sends SMS (Twilio) + Email (SMTP).
# Dedupe by (company|title|url) persisted in seen.json.
# One-time "No new internships found" notification persisted in state.json.

import os, re, json, time, hashlib, requests, sys, smtplib
from typing import List, Dict, Optional, Tuple
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

# SimplifyJobs Summer 2026 README (GitHub HTML) locations to try
GITHUB_OWNERS   = ["SimplifyJobs", "pittcsc"]
GITHUB_REPOS    = ["Summer2026-Internships"]
GITHUB_BRANCHES = ["dev", "main", "master"]

# Section names (normalized, emoji/format-insensitive)
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

# Files (stored next to this script)
HERE = os.path.dirname(__file__)
SEEN_PATH  = os.path.join(HERE, "seen.json")
STATE_PATH = os.path.join(HERE, "state.json")  # {"no_new_notified": bool}

# Feature flag to force a test notification (set TEST_NOTIFY=1 in workflow_dispatch)
TEST_NOTIFY = os.getenv("TEST_NOTIFY") == "1"

# ------------ Logging ------------
def log(*args):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    print(f"[{ts} UTC]", *args, flush=True)

# ------------ State (seen + state) ------------
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

def load_state():
    try:
        if os.path.exists(STATE_PATH):
            return json.load(open(STATE_PATH))
    except Exception as e:
        log("WARN load_state:", e)
    return {"no_new_notified": False}

def save_state(state: Dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log("WARN save_state:", e)

def sha(item: Dict) -> str:
    # Strong dedupe; switch to url-only by using key = item['url']
    key = f"{item.get('company','').strip()}|{item.get('title','').strip()}|{item.get('url','').strip()}"
    return hashlib.sha1(key.encode()).hexdigest()

# ------------ Notify (Twilio + Email) ------------
def twilio_send(body: str):
    sid = os.getenv("TWILIO_SID"); tok = os.getenv("TWILIO_TOKEN")
    frm = os.getenv("TWILIO_FROM"); to_list = [t.strip() for t in os.getenv("SMS_TO_LIST","").split(",") if t.strip()]
    if not all([sid, tok, frm]) or not to_list:
        log("INFO Twilio not configured or no recipients; skipping SMS")
        return

    api_base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    for to in to_list:
        try:
            data = {"To": to, "Body": body[:1500]}
            # Support Messaging Service SIDs (start with "MG")
            if frm.startswith("MG"):
                data["MessagingServiceSid"] = frm
            else:
                data["From"] = frm

            r = requests.post(f"{api_base}/Messages.json",
                              auth=(sid, tok), data=data, timeout=REQUEST_TIMEOUT)
            status = r.status_code
            j = {}
            try:
                j = r.json()
            except Exception:
                pass

            msg_sid = j.get("sid")
            err = j.get("error_code")
            log("INFO Twilio send", to, status, "sid:", msg_sid, "err:", err)

            # If created, poll once or twice for delivery result and log details
            if status == 201 and msg_sid:
                # short sleep to let Twilio update status
                time.sleep(2.0)
                try:
                    g = requests.get(f"{api_base}/Messages/{msg_sid}.json",
                                     auth=(sid, tok), timeout=20)
                    gj = g.json()
                    log("INFO Twilio status",
                        "to:", to,
                        "sid:", msg_sid,
                        "status:", gj.get("status"),
                        "err_code:", gj.get("error_code"),
                        "err_msg:", gj.get("error_message"))
                except Exception as e:
                    log("WARN Twilio status poll failed:", e)

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

# ------------ SimplifyJobs / Summer 2026 (sections via GitHub HTML, Age=0d) ------------
def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\u2600-\u27ff\U0001F000-\U0001FAFF]", "", s)  # strip emojis/symbols
    s = re.sub(r"[^\w\s&/+-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _fetch_2026_html() -> str:
    # Prefer GitHub HTML (not ?plain=1), try multiple owners/branches
    for owner in GITHUB_OWNERS:
        for repo in GITHUB_REPOS:
            for br in GITHUB_BRANCHES:
                url = f"https://github.com/{owner}/{repo}/blob/{br}/README.md"
                try:
                    html = fetch(url, headers={"Accept":"text/html"})
                    if html:
                        log("INFO 2026 README HTML", owner, repo, br, "len:", len(html))
                        return html
                except Exception as e:
                    log("WARN 2026 HTML miss:", url, e)
    return ""

def _normalize_heading_text(s: str) -> str:
    return _norm(s)

def parse_simplify_2026_age0() -> List[Dict]:
    """
    Parse only Age=0d rows from the three sections by reading GitHub's HTML and extracting tables.
    """
    html = _fetch_2026_html()
    if not html:
        log("ERROR Could not fetch 2026 README HTML from GitHub")
        return []

    soup = BeautifulSoup(html, "lxml")
    all_items: List[Dict] = []

    # Find H2 headings, match by normalized text
    h2s = soup.select("h2")
    for category, section_phrase in SIMPLIFY_SECTIONS.items():
        target = _normalize_heading_text(section_phrase)
        section_table = None

        # locate heading and the next table following it
        for h in h2s:
            head_txt = h.get_text(" ", strip=True)
            if target in _normalize_heading_text(head_txt):
                nxt = h.find_next(["table","h2"])
                if nxt and nxt.name == "table":
                    section_table = nxt
                break

        if section_table is None:
            log(f"WARN Simplify 2026 HTML: no table found for section: {category}")
            continue

        rows = section_table.select("tr")
        if not rows:
            log(f"WARN Simplify 2026 HTML: empty table for section: {category}")
            continue

        def cell_text(td): return td.get_text(" ", strip=True) if td else ""
        def cell_url(td):
            a = td.select_one("a[href]")
            return a["href"] if a and a.has_attr("href") else ""

        # header & age column index
        header_cells = rows[0].select("th,td")
        age_idx = None
        header_norm = [ _normalize_heading_text(cell_text(c)) for c in header_cells ]
        for idx, hx in enumerate(header_norm):
            if hx == "age":
                age_idx = idx
                break

        # iterate data rows
        data_rows = rows[1:] if header_cells else rows
        for tr in data_rows:
            tds = tr.select("td")
            if len(tds) < 4:
                continue

            texts = [cell_text(td) for td in tds]
            urls  = [cell_url(td)  for td in tds]

            # require Age=0d
            def has_0d():
                if age_idx is not None and age_idx < len(texts):
                    return _norm(texts[age_idx]) == "0d"
                return any(" 0d" in _norm(x) or _norm(x) == "0d" for x in texts)

            if not has_0d():
                continue

            company  = texts[0]
            title    = texts[1] if len(texts) > 1 else ""
            location = texts[2] if len(texts) > 2 else ""
            url      = urls[3]  if len(urls)  > 3 else cell_url(tds[3])

            # absolutize GitHub-relative links if any
            if url and url.startswith("/"):
                url = "https://github.com" + url

            if url:
                all_items.append(
                    normalize_item(
                        "SimplifyJobs 2026",
                        category,
                        title,
                        url,
                        company,
                        location,
                        meta={"posted": "0d"},
                    )
                )

    log(f"INFO Simplify 2026 age=0d total (HTML): {len(all_items)}")
    return all_items

# ------------ main ------------
def main():
    log("START run")
    log("INFO seen_path =", SEEN_PATH, "state_path =", STATE_PATH)
    twilio_self_check()
    seen = load_seen()
    state = load_state()
    log("INFO seen size =", len(seen), "no_new_notified =", state.get("no_new_notified"))

    all_items: List[Dict] = []

    # Intern-List tabs
    for cat, url in IL_TABS:
        try:
            items = parse_intern_list_tab(cat, url)
            log(f"INFO parsed Intern List — {cat}: {len(items)} items")
            all_items += items
        except Exception as e:
            log(f"ERROR parser Intern List — {cat}:", e)

    # SimplifyJobs 2026 (sections; Age=0d only)
    try:
        sj = parse_simplify_2026_age0()
        log(f"INFO parsed SimplifyJobs 2026 GitHub (Age=0d): {len(sj)} items")
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

    # TEST: force one synthetic item
    if TEST_NOTIFY and not new:
        new = [normalize_item("Intern Bot", "Test", "Test Notification", "https://example.com", "DemoCo")]

    if new:
        # reset "no new" flag; we found new items
        state["no_new_notified"] = False

        # Compose compact alert (cap to 10 lines)
        batch = new[:10]
        lines = [
            "• [" + (i.get("category") or "?") + "] [" + (i.get("source") or "?") + "] "
            + (i.get("company", "")[:40] or "Unknown Company") + " — " + (i.get("title", "")[:70] or "Role")
            + ((" — " + i.get("location", "")) if i.get("location") else "")
            + ((" — " + str(i.get("posted"))) if i.get("posted") else "")
            + "\n" + i["url"]
            for i in batch
        ]
        tail = "" if len(new) <= len(batch) else f"\n(+{len(new)-len(batch)} more new roles)"
        body = "New internships:\n" + "\n".join(lines) + tail + "\nReply STOP to opt out."

        twilio_send(body)
        email_send("Internship alerts", body)

        save_seen(seen)
        save_state(state)
        log(f"DONE notified {len(batch)} of {len(new)} new items; seen now {len(seen)}; no_new_notified={state['no_new_notified']}")
        return

    # No new items — send one-time heads-up if not already sent
    if not state.get("no_new_notified"):
        msg = "No new internships found in the last check. You'll only see this once until new roles appear."
        twilio_send(msg)
        email_send("Internship alerts — no new roles", msg)
        state["no_new_notified"] = True
        save_state(state)
        log("DONE no new items (sent one-time no-new notice)")
        return

    log("DONE no new items (no notice — already sent previously)")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL run crashed:", e)
        sys.exit(1)
