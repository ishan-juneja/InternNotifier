# == Intern Bot ==
# Monitors Intern List (SWE, Data Analysis, ML/AI, PM), Simplify internships, and PittCSC.
# Runs on a GitHub Actions cron (every 15 minutes) and sends SMS via Twilio to multiple recipients.
# Dedupe by (company|title|url) persisted in seen.json.


import os, re, json, time, hashlib, requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "intern-bot/1.0 (+https://github.com/)","Accept":"text/html,application/xhtml+xml"}
SEEN_PATH = "seen.json"

def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            return set(json.load(open(SEEN_PATH)))
        except Exception:
            return set()
    return set()

def save_seen(seen):
    with open(SEEN_PATH, "w") as f:
        json.dump(sorted(list(seen)), f)

def sha(item):
    key = f"{item.get('company','').strip()}|{item.get('title','').strip()}|{item.get('url','').strip()}"
    return hashlib.sha1(key.encode()).hexdigest()

def twilio_send(body):
    sid = os.getenv("TWILIO_SID"); tok = os.getenv("TWILIO_TOKEN")
    frm = os.getenv("TWILIO_FROM"); to_list = os.getenv("SMS_TO_LIST","").split(",")
    to_list = [t.strip() for t in to_list if t.strip()]
    if not all([sid, tok, frm]) or not to_list:
        print("Twilio not configured or no recipients; skipping SMS")
        return
    for to in to_list:
        try:
            r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=(sid, tok),
                data={"From": frm, "To": to, "Body": body[:1500]},
                timeout=30)
            print("Twilio status", to, r.status_code, r.text[:120])
        except Exception as e:
            print("Twilio error", to, e)

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=45)
    r.raise_for_status()
    return r.text

# ---- Parsers for Intern List ----
def _extract_links_by_prefix(html, prefix):
    soup = BeautifulSoup(html, "lxml")
    items = []
    # Any anchor that links into the given prefix is considered a listing
    for a in soup.select(f"a[href^='/{prefix}/']"):
        title = a.get_text(strip=True)
        href = a.get("href","")
        if not title or not href: continue
        url = "https://www.intern-list.com" + href if href.startswith("/") else href
        # Try to infer a company from nearby text
        company = ""
        parent = a.find_parent()
        if parent:
            txt = parent.get_text(" ", strip=True)
            # naive company capture before or after title
            m = re.search(r"\b(?:at|@)\s+([A-Za-z0-9.&' -]{2,})", txt)
            if m: company = m.group(1)
        items.append({"source":"Intern List", "category":"Software Engineering", "title": title, "company": company, "location":"", "url": url})
    return items

def parse_intern_list_swe():
    html = fetch("https://www.intern-list.com/swe-intern-list")
    return _extract_links_by_prefix(html, "swe-intern-list")

def parse_intern_list_da():
    html = fetch("https://www.intern-list.com/da-intern-list")
    items = _extract_links_by_prefix(html, "da-intern-list")
    # annotate category
    for it in items:
        it["source"]="Intern List"
        it["category"]="Data Analysis"
    return items

def parse_intern_list_ml():
    # ML & AI seems under data-science-internships detail pages
    html = fetch("https://www.intern-list.com/data-science-internships")
    items = _extract_links_by_prefix(html, "data-science-internships")
    # annotate category
    for it in items:
        it["source"]="Intern List"
        it["category"]="Machine Learning & AI"
    return items

# ---- Parser for PittCSC README (raw Markdown table) ----
def parse_pittcsc():
    raw = fetch("https://raw.githubusercontent.com/pittcsc/Summer2024-Internships/dev/README.md")
    items = []
    for line in raw.splitlines():
        if not line.strip().startswith("|"): continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 4 or cols[0] in ("Company","—","-"): continue
        company = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cols[0])
        title = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cols[1])
        loc = cols[2]
        m = re.search(r"\((https?://[^\)]+)\)", cols[3])
        url = m.group(1) if m else ""
        if url:
            items.append({"source":"PittCSC", "category": infer_category(title, default="Software Engineering"), "company": company, "title": title, "location": loc, "url": url})
    return items


def parse_simplify():
    """
    Parse Simplify.jobs internships page (all categories including PM/SWE/DA/ML).
    Extract job cards with company, title, url.
    """
    html = fetch("https://simplify.jobs/internships")
    soup = BeautifulSoup(html, "lxml")
    items=[]
    for a in soup.select("a[href*='/jobs/']"):
        title = a.get_text(strip=True)
        href = a.get("href","")
        if not title or not href: continue
        url = "https://simplify.jobs" + href if href.startswith("/") else href
        # infer company if possible from parent container text
        company = ""
        parent = a.find_parent()
        if parent:
            txt = parent.get_text(" ", strip=True)
            # company name heuristic
            m = re.search(r"([A-Z][A-Za-z0-9.&' -]{2,}) Internship", txt)
            if m: company = m.group(1)
        items.append({"source":"simplify","company":company,"title":title,"location":"","url":url,"category":"Simplify"})
    return items


def main():
    seen = load_seen()
    all_items = []
    try: all_items += parse_intern_list_swe()
    except Exception as e: print("SWE parse err:", e)
    try: all_items += parse_intern_list_da()
    except Exception as e: print("DA parse err:", e)
    try: all_items += parse_intern_list_ml()
    except Exception as e: print("ML parse err:", e)
    try: all_items += parse_intern_list_pm()
    except Exception as e: print("ML parse err:", e)
    try: all_items += parse_pittcsc()
    except Exception as e: print("PittCSC parse err:", e)
    try: all_items += parse_simplify()
    except Exception as e: print("PittCSC parse err:", e)

    # de-dupe and find new
    new = []
    for it in all_items:
        k = sha(it)
        if k not in seen:
            seen.add(k)
            new.append(it)

    if new:
        # limit per SMS and batch if needed
        batch = new[:6]
        lines = [
        "• [" + (i.get("category") or "?") + "]" + " [" + (i.get("source") or "?") + "] "
        + (i.get("company","")[:40] or "Unknown Company") + " — " + (i.get("title","")[:70] or "Role")
        + ((" — " + i.get("location","")) if i.get("location") else "")
        + "
" + i["url"]
        for i in batch
    ]} — {i.get('title','')[:70]}\n{i['url']}" for i in batch]
        tail = "" if len(new)<=6 else f"\n(+{len(new)-6} more new roles)"
        body = "New internships (Intern-List + PittCSC):\n" + "\n".join(lines) + tail + "\nReply STOP to opt out."
        twilio_send(body)
        print(f"Notified {len(batch)} of {len(new)} new items")
        save_seen(seen)
    else:
        print("No new items")

if __name__ == "__main__":
    main()

def parse_intern_list_pm():
    html = fetch("https://www.intern-list.com/pm-intern-list")
    items = _extract_links_by_prefix(html, "pm-intern-list")
    # annotate category
    for it in items:
        it["source"]="Intern List"
        it["category"]="Product Management"
    return items


def infer_category(title: str, default="SWE"):
    """
    Infer a broad category from a job title/role text.
    Falls back to `default` when no keyword matches.
    """
    t = (title or "").lower()
    # Product Management
    if any(k in t for k in ["product manager", "apm", "product management", "pm intern", "product intern"]):
        return "Product Management"
    # Data Analysis
    if any(k in t for k in ["data analyst", "analytics", "business analyst", "data analysis"]):
        return "Data Analysis"
    # Machine Learning & AI
    if any(k in t for k in ["machine learning", "ml ", " ml", " ai", "artificial intelligence", "research scientist", "deep learning"]):
        return "Machine Learning & AI"
    # Software Engineering (default)
    if any(k in t for k in ["software engineer", "swe", "backend", "frontend", "full stack", "mobile", "android", "ios"]):
        return "Software Engineering"
    return default
