# jobly

Automated internship application workflow from SWEList daily email digests.

> **Safety guarantee:** jobly never auto-submits. Every application requires you to type `YES` to confirm. The LLM step is advisory only.

---

## Quick Start

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Copy and edit configuration
mkdir -p ~/.config/jobly
cp config.example.yaml ~/.config/jobly/config.yaml
cp profile_template.json ~/.config/jobly/profile.json
# Edit profile.json with your real info

# 3. Set secrets
cp .env.example .env
# Fill in ANTHROPIC_API_KEY and RESUME_DEFAULT_PATH

# 4. Authenticate
jobly auth

# 5. Fetch → Queue → Run
jobly fetch
jobly queue
jobly run
```

---

## Setup

### 1. Google OAuth (Gmail API)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. **APIs & Services → Library** → Enable **Gmail API**
4. **APIs & Services → OAuth consent screen**
   - User type: External
   - Add your email as a test user
5. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file
6. Save it to `~/.config/jobly/credentials.json`
7. Run `jobly auth` — a browser window will open for Google sign-in

The token is saved to `~/.config/jobly/token.json` and auto-refreshed.
Token permissions are `gmail.readonly` only — jobly cannot send or modify email.

### 2. Playwright

```bash
# Installed automatically during `jobly auth`, or manually:
python -m playwright install chromium --with-deps
```

### 3. Profile

Edit `~/.config/jobly/profile.json` — copy from `profile_template.json`:

```json
{
  "personal": {
    "first_name": "Josh",
    "email": "josh@example.com",
    "phone": "+1 (555) 123-4567",
    "linkedin_url": "https://linkedin.com/in/yourhandle",
    "github_url": "https://github.com/yourhandle"
  },
  "education": [{
    "institution": "Your University",
    "degree": "Bachelor of Science",
    "field_of_study": "Computer Science",
    "end_date": "2026-05",
    "gpa": "3.8"
  }],
  "work_authorization": {
    "authorized_us": true,
    "requires_sponsorship": false
  }
}
```

### 4. Resume

Set `RESUME_DEFAULT_PATH` in `.env` or `resume_default_path` in `config.yaml`:

```yaml
resume_default_path: "~/Documents/resume.pdf"
resume_variants:
  ml: "~/Documents/resume_ml.pdf"
```

Use a variant at runtime: `jobly run --resume=ml`

### 5. Anthropic API Key (optional — for LLM eval)

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

Disable LLM eval: `jobly run --skip-llm`
Or set `llm.enabled: false` in `config.yaml`.

---

## Commands

### `jobly auth`

Verifies credentials.json, runs Gmail OAuth flow, installs Playwright Chromium,
and checks your resume file.

```bash
jobly auth
```

### `jobly fetch`

Pulls the latest SWEList digest email(s), parses job listings, deduplicates
by URL hash, and stores them in the local SQLite DB.

```bash
jobly fetch
jobly fetch --dry-run   # Preview without saving
```

### `jobly queue`

Scores all `discovered` jobs with the rules-based filter and presents a review
table. Jobs above the score threshold are queued; others are marked filtered_out.

```bash
jobly queue
jobly queue --min-score 0.20        # Lower threshold
jobly queue --show-filtered         # Show filtered-out jobs too
jobly queue --yes                   # Skip confirmation prompt
```

### `jobly run`

Processes queued applications one by one:

1. Shows job summary, asks if you want to proceed
2. Opens Chromium, navigates to the ATS
3. Fills all known fields from your profile
4. For unknown fields: pauses and asks you (answer saved for reuse)
5. Navigates to the review page
6. Optionally asks Claude for a RECOMMEND_SUBMIT / RECOMMEND_SKIP evaluation
7. **Requires you to type `YES` to submit** — any other input skips

```bash
jobly run
jobly run --skip-llm        # Skip Claude evaluation step
jobly run --limit 3         # Process only 3 applications
jobly run --resume ml       # Use a resume variant
```

### `jobly status`

Shows a dashboard of job and application counts by status, recent run history,
and the next queued applications.

```bash
jobly status
```

### `jobly open <id>`

Opens a job URL in your default browser. Accepts a job post ID or application
ID prefix (first 8 characters from `jobly status`).

```bash
jobly open abc12345
```

### `jobly reset <id>`

Re-queues an application (e.g. after an error or if you want to retry).

```bash
jobly reset abc12345
```

### `jobly config`

Prints all resolved configuration paths and values (no secrets).

```bash
jobly config
```

---

## How It Works

```
Gmail API (OAuth2, readonly)
    │
    ▼
SWEList email HTML
    │
gmail/parser.py  ──── multi-strategy HTML parser
    │                  (structured cards → ATS links → path-based fallback)
    ▼
job_posts table  ──── URL-hash deduplication
    │
utils/filter.py  ──── rules-based score (keywords + ATS + location)
    │
CLI review table ──── user approves queue
    │
adapters/        ──── Playwright automation
    ├── ashby.py      (React SPA, /apply flow)
    ├── greenhouse.py (HTML form, single-page)
    ├── lever.py      (React SPA, /apply suffix)
    └── workday.py    (guided mode — best-effort + manual assist)
    │
llm/evaluator.py ──── Claude RECOMMEND_SUBMIT | RECOMMEND_SKIP (advisory)
    │
CLI gate         ──── user types YES  ← NON-NEGOTIABLE HUMAN GATE
    │
adapter.submit() ──── final click
    │
SQLite DB        ──── full audit trail (emails, jobs, applications, artifacts)
```

---

## Adding a New ATS Adapter

1. Create `app/adapters/your_ats.py`:

```python
from app.adapters.base import BaseAdapter, FillResult, QuestionResolver
import re

class YourATSAdapter(BaseAdapter):
    ats_type = "yourATS"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(re.search(r"yourats\.com", url, re.I))

    async def open_and_prepare(self, page, url): ...
    async def fill_form(self, page, profile, resume_path, resolver) -> FillResult: ...
    async def reach_review_step(self, page): ...
    async def submit(self, page): ...
```

2. Register it in `app/adapters/__init__.py`:

```python
from app.adapters.your_ats import YourATSAdapter

_REGISTRY: list[Type[BaseAdapter]] = [
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    YourATSAdapter,   # add here
    WorkdayAdapter,
]
```

3. Add URL detection to `app/utils/hashing.py → detect_ats_from_url()`.

4. Write tests in `tests/test_adapters.py`.

---

## Troubleshooting

### Selectors break after ATS update

ATS vendors update their DOM regularly. When a selector stops working:

1. Check the error screenshot in `~/.local/share/jobly/artifacts/`
2. Open the job URL manually: `jobly open <id>`
3. Use browser DevTools to find the new selector
4. Update the selector in the relevant adapter file
5. Test with `jobly run --limit 1`

Prefer resilient selectors in this order:
- `data-testid` / `data-qa` attributes (most stable)
- `aria-label` attributes
- `name` attributes
- `id` attributes
- Class names (least stable — avoid)

### CAPTCHAs

Some ATS systems (especially Workday) use CAPTCHAs or bot-detection.

Options:
- Run with `browser.headless: false` (default) — human-like behaviour
- Increase `browser.slow_mo_ms` in `config.yaml` (e.g., 200–500)
- Increase `browser.min_wait_ms` and `browser.max_wait_ms`
- If CAPTCHA appears, solve it manually in the open browser window —
  Playwright will wait during user input pauses

### OAuth token expired

```bash
rm ~/.config/jobly/token.json
jobly auth
```

### Application stuck in `started` status

```bash
jobly reset <id>
```

### DB corruption / fresh start

```bash
rm ~/.local/share/jobly/jobly.db
jobly fetch
jobly queue
```

---

## Database Schema

Located at `~/.local/share/jobly/jobly.db` (SQLite, WAL mode).

| Table | Purpose |
|-------|---------|
| `emails` | Raw email metadata + HTML (audit trail) |
| `job_posts` | Extracted + deduplicated job listings |
| `application_runs` | Batch run metadata |
| `applications` | Individual application attempts (full lifecycle) |
| `question_answers` | Cached answers to ATS custom questions |
| `artifacts` | Screenshots and HTML snapshots |

Application status lifecycle:
```
queued → started → filled → needs_review → submitted
                                         → skipped
                  ↓ (any stage)
                  error
```

---

## Running Tests

```bash
# All tests (no browser required)
pytest

# With coverage
pytest --cov=app --cov-report=term-missing

# Specific module
pytest tests/test_parser.py -v
pytest tests/test_filter.py -v
pytest tests/test_adapters.py -v
```

---

## Security Notes

- OAuth token (`token.json`) is chmod 600 — keep it out of git
- `profile.json` contains PII — keep it out of git
- API keys are read from env only, never logged or stored in DB
- The log processor actively redacts common API key patterns

Add to `.gitignore`:
```
.env
~/.config/jobly/
*.db
artifacts/
```
