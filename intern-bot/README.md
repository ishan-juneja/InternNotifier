
# Intern Bot — SWE/DA/ML Internships (Intern-List + PittCSC)

This bot checks **every 10 minutes** for new internships from:
- intern-list.com — **Software Engineering**, **Data Analysis**, **Machine Learning & AI**
- Pitt CSC × Simplify GitHub list

When new roles appear, it sends SMS alerts via Twilio to one or more subscribers.

## Quick start

1) Fork or create a new private repository and copy these files.
2) In GitHub → **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `TWILIO_SID` – Twilio Account SID
   - `TWILIO_TOKEN` – Twilio Auth Token
   - `TWILIO_FROM` – your Twilio phone number (E.164, e.g. `+14155551234`)
   - `SMS_TO_LIST` – comma-separated list of recipient numbers (e.g. `+14155551234,+14085551234`)
3) Commit and push. GitHub Actions will run every 10 minutes.
4) Check the Actions logs for the first run, then you’ll get texts only when NEW items are found.

### Sources
- Intern List (SWE): https://www.intern-list.com/swe-intern-list
- Intern List (Data Analysis): https://www.intern-list.com/da-intern-list
- Intern List (Machine Learning & AI): category pages under https://www.intern-list.com/data-science-internships/
- Pitt CSC × Simplify (raw README): https://raw.githubusercontent.com/pittcsc/Summer2024-Internships/dev/README.md

### Notes
- We dedupe on `(company|title|url)` and persist seen hashes in `seen.json`.
- If Intern List HTML changes, tweak selectors in `watcher.py` (look for `parse_intern_list_*` functions).
- For more users, add numbers to `SMS_TO_LIST` or wire into a DB later.

**Now also monitors Intern List — Product Management:** https://www.intern-list.com/pm-intern-list


### Now also monitoring Simplify
- Simplify internships: https://simplify.jobs/internships (category inferred from title)

### Schedule
- The GitHub Actions workflow is set to run **every 15 minutes**.

### SMS Message Format
Each alert line includes:
`• [Category] [Source] Company — Title — Location (if available)`
followed by the job URL.
