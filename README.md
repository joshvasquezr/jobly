# jobly

Fetches the [SimplifyJobs Summer 2026 Internships](https://github.com/SimplifyJobs/Summer2026-Internships) list and generates a local `jobs.html` dashboard — filterable, tabbed, and tracks your applications in the browser.

---

## What you get

| Tab | What's in it |
|---|---|
| ⭐ Preferred | Ashby, Greenhouse, Lever — the cleanest apply flows |
| Other | All other direct links |
| ✓ Submitted | Jobs you've marked as applied — with date |
| Hidden | Jobs you've dismissed |

- **Search bar** — filter by company or role name instantly
- **Apply flow** — click Apply → opens the job → confirm → row moves to Submitted tab with a date stamp
- **Undo** — move any row back out of Submitted or Hidden
- **Persists across reloads** — your submitted and hidden jobs are saved in `localStorage` (no server, no account)

---

## Setup

**Requirements:** Python 3.11+

```bash
# 1. Clone the repo
git clone https://github.com/joshvasquezr/jobly.git
cd jobly

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
source .venv/bin/activate        # Mac / Linux
.venv\Scripts\activate           # Windows

# 4. Install dependencies
pip install httpx beautifulsoup4 lxml

# 5. Run it
python generate_jobs.py
```

A browser tab opens automatically with your dashboard.

> **Using uv?** Steps 2–5 become: `uv sync && uv run python generate_jobs.py`

---

## Run it again anytime

```bash
# Make sure your venv is active first
source .venv/bin/activate

python generate_jobs.py
```

Or to keep it auto-refreshing every hour in the background:

```bash
python generate_jobs.py --watch
```

---

## Bookmark it

After the first run, your terminal will print:

```
Written → /Users/you/jobly/jobs.html
```

Copy that path and bookmark it in your browser as:

```
file:///Users/you/jobly/jobs.html
```

Next time you want to check jobs, just open the bookmark. If the script is running with `--watch`, the page refreshes itself every hour automatically.

---

## Auto-update without a terminal (Mac only)

This makes your Mac regenerate the dashboard every hour in the background — no terminal needed.

**1. Create the file** `~/Library/LaunchAgents/com.jobly.plist` with this content (replace both paths with your actual paths):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jobly</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/jobly/.venv/bin/python</string>
    <string>/Users/you/jobly/generate_jobs.py</string>
  </array>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/jobly.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/jobly.log</string>
</dict>
</plist>
```

**2. Load it:**

```bash
launchctl load ~/Library/LaunchAgents/com.jobly.plist
```

Done. The dashboard regenerates every hour, every time you log in.

**To stop it:**

```bash
launchctl unload ~/Library/LaunchAgents/com.jobly.plist
```

---

## Customize what shows up

Open `generate_jobs.py` and edit these variables near the top:

```python
# Which role titles to include — add anything you want to see
TITLE_KEYWORDS = [
    "software engineer",
    "backend",
    "data engineer",
    ...
]

# ATS platforms shown on the Preferred tab
PREFERRED_ATS = {"ashby", "greenhouse", "lever"}

# ATS platforms to skip entirely (painful apply flows)
SKIP_ATS = {"workday", "taleo"}

# Drop listings older than this many days
MAX_AGE_DAYS = 60
```

Re-run `python generate_jobs.py` after any change to see the updated dashboard.
