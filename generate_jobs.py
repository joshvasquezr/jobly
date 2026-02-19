#!/usr/bin/env python3
"""
Fetch the SimplifyJobs/Summer2026-Internships README and generate
a browsable jobs.html with clickable Apply links.

Usage:
    python generate_jobs.py            # generate once and open
    python generate_jobs.py --watch    # regenerate every hour (Ctrl+C to stop)
"""

from __future__ import annotations

import re
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────

_README_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"
)

# Only roles whose title contains one of these (case-insensitive)
TITLE_KEYWORDS = [
    "software engineer",
    "software developer",
    "software development",
    "swe intern",
    "backend",
    "backend engineer",
    "systems engineer",
    "infrastructure",
    "platform engineer",
    "data engineer",
    "database",
    "distributed systems",
    "site reliability",
    "sre",
    "devops",
    "ml engineer",
    "machine learning engineer",
    "ai engineer",
    "research engineer",
    "computer science",
]

# No login needed — apply in minutes
QUICK_ATS = {
    "ashby", "greenhouse", "lever",
    "workable", "breezy", "jazzhr",
    "rippling", "recruitee",
}

# Require account creation — longer process
ACCOUNT_ATS = {
    "workday", "taleo", "icims",
    "smartrecruiters", "oracle", "sap",
    "dayforce", "adp", "bamboohr", "bullhorn",
}
# Everything else (unknown ATS) defaults to Quick Apply

# Drop listings older than this many days
MAX_AGE_DAYS = 60

# ── ATS detection ──────────────────────────────────────────────────────────────

def detect_ats(url: str) -> str:
    u = url.lower()
    if "ashbyhq.com" in u or "jobs.ashby" in u:
        return "ashby"
    if "greenhouse.io" in u or "grnh.se" in u or "gh_jid=" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "myworkdayjobs.com" in u or "workday.com" in u:
        return "workday"
    if "smartrecruiters.com" in u:
        return "smartrecruiters"
    if "icims.com" in u or "icims=1" in u:
        return "icims"
    if "taleo.net" in u:
        return "taleo"
    if "workable.com" in u:
        return "workable"
    if "breezy.hr" in u:
        return "breezy"
    if "jazzhr.com" in u:
        return "jazzhr"
    if "rippling.com" in u:
        return "rippling"
    if "jobvite.com" in u:
        return "jobvite"
    if "recruitee.com" in u:
        return "recruitee"
    if "bamboohr.com" in u:
        return "bamboohr"
    if "oraclecloud.com" in u:
        return "oracle"
    if "successfactors.com" in u:
        return "sap"
    if "dayforce.com" in u:
        return "dayforce"
    if "adp.com" in u:
        return "adp"
    if "bullhorn.com" in u:
        return "bullhorn"
    return "other"


# ── Parsing ────────────────────────────────────────────────────────────────────

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "]+",
    flags=re.UNICODE,
)


def _clean(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _get_apply_url(td) -> Optional[str]:
    """Return direct ATS URL from the Apply button, or None."""
    if "🔒" in td.get_text():
        return None
    for a in td.find_all("a", href=True):
        img = a.find("img", alt=re.compile(r"^Apply$", re.I))
        if img:
            return a["href"]
    return None


def _parse_age_days(raw: str) -> int:
    m = re.match(r"(\d+)\s*(d|w|mo)?", raw.strip().lower())
    if not m:
        return 999
    n, unit = int(m.group(1)), (m.group(2) or "d")
    return n * {"d": 1, "w": 7, "mo": 30}[unit]


def fetch_jobs() -> list[dict]:
    print(f"  Fetching README...")
    resp = httpx.get(_README_URL, timeout=30, follow_redirects=True)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    # Find the main internship table
    table = None
    for t in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in t.find_all("th")]
        if "Company" in headers and "Role" in headers and "Application" in headers:
            table = t
            break
    if not table:
        raise RuntimeError("Could not find the internship table in README.")

    keywords_lower = [k.lower() for k in TITLE_KEYWORDS]
    jobs: list[dict] = []
    seen_urls: set[str] = set()
    current_company: Optional[str] = None

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        company_td, role_td, location_td, application_td = cells[:4]
        age_td = cells[4] if len(cells) > 4 else None

        # Track current company across ↳ continuation rows
        company_text = company_td.get_text(strip=True)
        if "↳" not in company_text:
            a = company_td.find("a")
            name = _clean(a.get_text(strip=True) if a else company_text)
            if name:
                current_company = name

        if not current_company:
            continue

        url = _get_apply_url(application_td)
        if not url or url in seen_urls:
            continue

        role = _clean(role_td.get_text(strip=True))
        if not role:
            continue

        # Be picky — must match at least one keyword
        role_lower = role.lower()
        if not any(kw in role_lower for kw in keywords_lower):
            continue

        ats = detect_ats(url)

        # Parse age with unit awareness; normalize to days
        age_str = _clean(age_td.get_text(strip=True)) if age_td else ""
        age_days = _parse_age_days(age_str)

        # Drop stale listings
        if age_days > MAX_AGE_DAYS:
            continue

        seen_urls.add(url)
        jobs.append({
            "company": current_company,
            "role": role,
            "location": _clean(location_td.get_text(strip=True)) or "—",
            "url": url,
            "ats": ats,
            "age_days": age_days,
        })

    return jobs


# ── HTML generation ────────────────────────────────────────────────────────────

_ATS_COLORS = {
    "ashby":           "#7C3AED",
    "greenhouse":      "#059669",
    "lever":           "#D97706",
    "workable":        "#2563EB",
    "icims":           "#0891B2",
    "breezy":          "#0D9488",
    "smartrecruiters": "#B45309",
    "workday":         "#CC0000",
    "taleo":           "#B45309",
    "jazzhr":          "#E11D48",
    "rippling":        "#6366F1",
    "jobvite":         "#0369A1",
    "recruitee":       "#0891B2",
    "bamboohr":        "#16A34A",
    "oracle":          "#C2410C",
    "sap":             "#1D4ED8",
    "dayforce":        "#7C3AED",
    "adp":             "#BE185D",
    "bullhorn":        "#0F766E",
    "other":           "#4B5563",
}

# Secondary sort key within same age — quick-apply ATS first
_ATS_ORDER = {"ashby": 0, "greenhouse": 1, "lever": 2}


def _sort_key(j: dict) -> tuple:
    return (j["age_days"], _ATS_ORDER.get(j["ats"], 3), j["company"].lower())


def _badge(ats: str) -> str:
    color = _ATS_COLORS.get(ats, "#4B5563")
    return f'<span class="badge" style="background:{color}">{ats.upper()}</span>'


def _age_label(j: dict) -> str:
    d = j["age_days"]
    if d == 0:
        color = "#3fb950"   # green  — just posted
    elif d <= 2:
        color = "#d29922"   # yellow — very fresh
    elif d <= 7:
        color = "#8b949e"   # grey   — recent
    else:
        color = "#484f58"   # dim    — older
    return f'<span style="color:{color};font-size:0.78rem;font-weight:600">{d}d</span>'


def _rows(jobs: list[dict], section_cls: str) -> str:
    if not jobs:
        return "<tr><td colspan='8' class='empty'>No jobs in this category.</td></tr>"
    parts = []
    for j in jobs:
        url_esc = j["url"].replace("'", "&#39;")
        parts.append(
            f"<tr data-url='{url_esc}' data-section='{section_cls}'>"
            f"<td class='age'>{_age_label(j)}</td>"
            f"<td class='co'>{j['company']}</td>"
            f"<td class='role'>{j['role']}</td>"
            f"<td class='loc'>{j['location']}</td>"
            f"<td>{_badge(j['ats'])}</td>"
            f"<td><button class='btn' onclick='applyClicked(this)'>Apply →</button></td>"
            f"<td class='save-cell'><button class='save-btn' onclick='markSaved(this)' title='Save for later'>&#9733;</button></td>"
            f"<td class='skip-cell'><button class='skip-btn' onclick='markSkipped(this)' title='Not interested'>✕</button></td>"
            f"</tr>"
        )
    return "\n".join(parts)


def _section(title: str, jobs: list[dict], cls: str) -> str:
    jobs_sorted = sorted(jobs, key=_sort_key)
    return f"""
<div class="section {cls}">
  <h2>{title} <span class="cnt">({len(jobs)})</span></h2>
  <table>
    <thead><tr><th>Age</th><th>Company</th><th>Role</th><th>Location</th><th>ATS</th><th></th><th></th><th></th></tr></thead>
    <tbody id="{cls}-tbody">{_rows(jobs_sorted, cls)}</tbody>
  </table>
</div>"""


# JavaScript is defined as a plain string so braces don't need escaping
_JS = """
// ── Helpers ───────────────────────────────────────────────────────────────────
const SUBMITTED_KEY = "jobly_submitted";
const SKIPPED_KEY   = "jobly_skipped";
const SAVED_KEY     = "jobly_saved";

function getSubmitted() { return JSON.parse(localStorage.getItem(SUBMITTED_KEY) || "{}"); }
function getSkipped()   { return JSON.parse(localStorage.getItem(SKIPPED_KEY)   || "{}"); }
function getSaved()     { return JSON.parse(localStorage.getItem(SAVED_KEY)     || "{}"); }
function saveSubmitted(o) { localStorage.setItem(SUBMITTED_KEY, JSON.stringify(o)); }
function saveSkipped(o)   { localStorage.setItem(SKIPPED_KEY,   JSON.stringify(o)); }
function saveSaved(o)     { localStorage.setItem(SAVED_KEY,     JSON.stringify(o)); }

function findRow(url) {
  const all = document.querySelectorAll("tr[data-url]");
  for (const r of all) { if (r.dataset.url === url) return r; }
  return null;
}

// ── Apply → confirmation ──────────────────────────────────────────────────────
function applyClicked(btn) {
  const row = btn.closest("tr");
  window.open(row.dataset.url, "_blank");
  btn.closest("td").innerHTML =
    "Submitted? " +
    "<button class='confirm-btn yes-btn' onclick='confirmSubmit(this)'>&#10003; Yes</button> " +
    "<button class='confirm-btn no-btn'  onclick='cancelSubmit(this)'>&#10007; No</button>";
}

function confirmSubmit(btn) {
  const row  = btn.closest("tr");
  const url  = row.dataset.url;
  const now  = new Date().toLocaleDateString("en-US", {month:"short", day:"numeric", year:"numeric"});
  const s    = getSubmitted();
  s[url]     = { date_submitted: now };
  saveSubmitted(s);
  moveToSubmitted(row, now);
}

function cancelSubmit(btn) {
  btn.closest("td").innerHTML =
    "<button class='btn' onclick='applyClicked(this)'>Apply &#8594;</button>";
}

function moveToSubmitted(row, dateStr) {
  const section = row.dataset.section;
  const cells = row.querySelectorAll("td");
  cells[5].innerHTML = "<span class='date-submitted'>" + dateStr + "</span>";
  cells[6].innerHTML = "";
  cells[7].innerHTML = "<button class='undo-btn' onclick='undoSubmitted(this)'>Undo</button>";
  row.classList.add("submitted-row");
  document.getElementById("submitted-tbody").appendChild(row);
  updateSubmittedCount();
  _updateTabCount(section);
}

function undoSubmitted(btn) {
  const row     = btn.closest("tr");
  const url     = row.dataset.url;
  const section = row.dataset.section;
  const s = getSubmitted();
  delete s[url];
  saveSubmitted(s);
  const cells = row.querySelectorAll("td");
  cells[5].innerHTML = "<button class='btn' onclick='applyClicked(this)'>Apply &#8594;</button>";
  cells[6].innerHTML = "<button class='save-btn' onclick='markSaved(this)' title='Save for later'>&#9733;</button>";
  cells[7].innerHTML = "<button class='skip-btn' onclick='markSkipped(this)' title='Not interested'>&#10005;</button>";
  row.classList.remove("submitted-row");
  const tbody = document.getElementById(section + "-tbody");
  if (tbody) tbody.appendChild(row);
  updateSubmittedCount();
  _updateTabCount(section);
}

// ── Save / Saved ──────────────────────────────────────────────────────────────
function markSaved(btn) {
  const row     = btn.closest("tr");
  const url     = row.dataset.url;
  const section = row.dataset.section;
  const sv  = getSaved();
  sv[url]   = {};
  saveSaved(sv);
  _doSave(row);
  updateSavedCount();
  _updateTabCount(section);
}

function _doSave(row) {
  const cells = row.querySelectorAll("td");
  cells[6].innerHTML = "<button class='undo-btn' onclick='undoSaved(this)'>Undo</button>";
  row.classList.add("saved-row");
  document.getElementById("saved-tbody").appendChild(row);
}

function undoSaved(btn) {
  const row     = btn.closest("tr");
  const url     = row.dataset.url;
  const section = row.dataset.section;
  const sv = getSaved();
  delete sv[url];
  saveSaved(sv);
  const cells = row.querySelectorAll("td");
  cells[6].innerHTML = "<button class='save-btn' onclick='markSaved(this)' title='Save for later'>&#9733;</button>";
  row.classList.remove("saved-row");
  const tbody = document.getElementById(section + "-tbody");
  if (tbody) tbody.appendChild(row);
  updateSavedCount();
  _updateTabCount(section);
}

// ── Skip / Hidden ─────────────────────────────────────────────────────────────
function markSkipped(btn) {
  const row     = btn.closest("tr");
  const url     = row.dataset.url;
  const section = row.dataset.section;
  const sk  = getSkipped();
  sk[url]   = {};
  saveSkipped(sk);
  _doSkip(row);
  updateHiddenCount();
  _updateTabCount(section);
}

function _doSkip(row) {
  const cells = row.querySelectorAll("td");
  cells[7].innerHTML = "<button class='undo-btn' onclick='undoSkipped(this)'>Undo</button>";
  row.classList.add("skipped-row");
  document.getElementById("hidden-tbody").appendChild(row);
}

function undoSkipped(btn) {
  const row     = btn.closest("tr");
  const url     = row.dataset.url;
  const section = row.dataset.section;
  const sk = getSkipped();
  delete sk[url];
  saveSkipped(sk);
  const cells = row.querySelectorAll("td");
  cells[7].innerHTML = "<button class='skip-btn' onclick='markSkipped(this)' title='Not interested'>&#10005;</button>";
  row.classList.remove("skipped-row");
  const tbody = document.getElementById(section + "-tbody");
  if (tbody) tbody.appendChild(row);
  updateHiddenCount();
  _updateTabCount(section);
}

// ── Tab navigation ────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach(p =>
    p.classList.toggle("active", p.id === "tab-" + name));
  const showSearch = name === "quick" || name === "account";
  document.getElementById("search-wrap").style.display = showSearch ? "" : "none";
}

// ── Section counts ────────────────────────────────────────────────────────────
function updateSubmittedCount() {
  const n = document.getElementById("submitted-tbody").rows.length;
  document.getElementById("submitted-tab-cnt").textContent = "(" + n + ")";
}

function updateHiddenCount() {
  const n = document.getElementById("hidden-tbody").rows.length;
  document.getElementById("hidden-tab-cnt").textContent = "(" + n + ")";
}

function updateSavedCount() {
  const n = document.getElementById("saved-tbody").rows.length;
  document.getElementById("saved-tab-cnt").textContent = "(" + n + ")";
}

function _updateTabCount(section) {
  const tbody = document.getElementById(section + "-tbody");
  const span  = document.getElementById(section + "-tab-cnt");
  if (tbody && span) span.textContent = "(" + tbody.rows.length + ")";
}

// ── Search ────────────────────────────────────────────────────────────────────
function filterRows(q) {
  q = q.toLowerCase();
  document.querySelectorAll("#quick-tbody tr, #account-tbody tr").forEach(function(row) {
    var text = row.textContent.toLowerCase();
    row.classList.toggle("hidden", q.length > 0 && !text.includes(q));
  });
}

// ── Page-load restore ─────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", function() {
  const submitted = getSubmitted();
  for (const [url, meta] of Object.entries(submitted)) {
    const row = findRow(url);
    if (row) moveToSubmitted(row, meta.date_submitted);
  }
  const saved = getSaved();
  for (const url of Object.keys(saved)) {
    const row = findRow(url);
    if (row) _doSave(row);
  }
  const skipped = getSkipped();
  for (const url of Object.keys(skipped)) {
    const row = findRow(url);
    if (row) _doSkip(row);
  }
  updateSavedCount();
  updateHiddenCount();
  _updateTabCount("quick");
  _updateTabCount("account");
  switchTab("quick");
  document.getElementById("search").focus();
});
"""


def generate_html(jobs: list[dict]) -> str:
    quick   = [j for j in jobs if j["ats"] in QUICK_ATS or j["ats"] not in ACCOUNT_ATS]
    account = [j for j in jobs if j["ats"] in ACCOUNT_ATS]
    ts = datetime.now().strftime("%b %d %Y, %I:%M %p")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="3600">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Summer 2026 Internships</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0d1117;color:#c9d1d9;padding:20px;font-size:14px}}
h1{{font-size:1.5rem;font-weight:700;color:#f0f6fc;margin-bottom:4px}}
.meta{{color:#8b949e;font-size:0.82rem;margin-bottom:8px}}
.search-wrap{{margin-bottom:28px;margin-top:16px}}
#search{{width:100%;max-width:420px;padding:8px 12px;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:0.9rem;outline:none}}
#search:focus{{border-color:#58a6ff}}
#search::placeholder{{color:#484f58}}
.section{{margin-bottom:36px}}
h2{{font-size:1rem;font-weight:600;padding:10px 14px;border-radius:6px;margin-bottom:1px}}
.quick h2{{background:#0d2818;color:#3fb950;border-left:3px solid #238636}}
.account h2{{background:#1a1200;color:#d29922;border-left:3px solid #9e6a03}}
.saved-section h2{{background:#0d1b2e;color:#79c0ff;border-left:3px solid #1f6feb}}
.submitted-section h2{{background:#0a2a1a;color:#6e9f7f;border-left:3px solid #238636}}
.hidden-section h2{{background:#1a1212;color:#6e7681;border-left:3px solid #30363d}}
.tab-bar{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:0;border-bottom:1px solid #21262d;padding-bottom:0}}
.tab{{background:none;color:#8b949e;padding:8px 16px;border-radius:6px 6px 0 0;font-size:0.88rem;font-weight:500;border:1px solid transparent;border-bottom:none;margin-bottom:-1px;cursor:pointer}}
.tab:hover{{color:#c9d1d9;background:#161b22}}
.tab.active{{background:#0d1117;color:#f0f6fc;border-color:#30363d;border-bottom-color:#0d1117}}
.tab-panel{{display:none;padding-top:20px}}
.tab-panel.active{{display:block}}
.cnt{{font-weight:400;color:#8b949e;font-size:0.85rem}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;padding:7px 8px;color:#8b949e;border-bottom:1px solid #21262d;font-weight:500;font-size:0.75rem;text-transform:uppercase;letter-spacing:.06em}}
td{{padding:7px 8px;border-bottom:1px solid #161b22;vertical-align:middle}}
tr.hidden{{display:none}}
tr:hover td{{background:#161b22}}
.age{{text-align:center;white-space:nowrap;width:36px}}
.co{{font-weight:600;color:#f0f6fc}}
.role{{color:#c9d1d9;overflow-wrap:break-word;word-break:break-word}}
.loc{{color:#8b949e;font-size:0.82rem}}
.badge{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:0.68rem;font-weight:700;color:#fff;letter-spacing:.05em;white-space:nowrap}}
button{{font-family:inherit;cursor:pointer;border:none}}
.btn{{display:inline-block;padding:4px 11px;background:#21262d;color:#79c0ff;border-radius:5px;font-size:0.8rem;font-weight:500;white-space:nowrap}}
.btn:hover{{background:#30363d;color:#58a6ff}}
.save-cell{{width:28px;text-align:center}}
.save-btn{{background:none;color:#484f58;padding:2px 6px;border-radius:3px;font-size:0.9rem}}
.save-btn:hover{{color:#f0c040;background:#1a1600}}
.skip-cell{{width:28px;text-align:center}}
.skip-btn{{background:none;color:#484f58;padding:2px 6px;border-radius:3px;font-size:0.9rem}}
.skip-btn:hover{{color:#f85149;background:#1a0808}}
.undo-btn{{background:none;border:1px solid #30363d;color:#8b949e;padding:2px 8px;border-radius:4px;font-size:0.75rem}}
.undo-btn:hover{{border-color:#8b949e;color:#c9d1d9}}
.confirm-btn{{padding:3px 9px;border-radius:4px;font-size:0.8rem;margin:0 2px}}
.yes-btn{{background:#238636;color:#fff}}
.yes-btn:hover{{background:#2ea043}}
.no-btn{{background:#21262d;color:#8b949e}}
.no-btn:hover{{background:#30363d}}
.date-submitted{{color:#8b949e;font-size:0.82rem}}
.submitted-row td{{opacity:0.65}}
.submitted-row .co,.submitted-row .role{{text-decoration:line-through}}
.submitted-row .date-submitted,.submitted-row .undo-btn{{text-decoration:none;opacity:1}}
.saved-row td{{opacity:0.8}}
.skipped-row td{{opacity:0.45}}
.skipped-row .co,.skipped-row .role{{text-decoration:line-through}}
.skipped-row .undo-btn{{opacity:1}}
.empty{{color:#484f58;padding:20px;text-align:center}}
</style>
</head>
<body>
<h1>Summer 2026 Internships</h1>
<p class="meta">Generated {ts} &nbsp;·&nbsp; {len(quick)} quick apply &nbsp;·&nbsp; {len(account)} account required &nbsp;·&nbsp; {len(jobs)} total</p>

<div class="tab-bar">
  <button class="tab" data-tab="quick"     onclick="switchTab('quick')">Quick Apply <span id="quick-tab-cnt">({len(quick)})</span></button>
  <button class="tab" data-tab="account"   onclick="switchTab('account')">Account Required <span id="account-tab-cnt">({len(account)})</span></button>
  <button class="tab" data-tab="saved"     onclick="switchTab('saved')">Saved <span id="saved-tab-cnt">(0)</span></button>
  <button class="tab" data-tab="submitted" onclick="switchTab('submitted')">Submitted <span id="submitted-tab-cnt">(0)</span></button>
  <button class="tab" data-tab="hidden"    onclick="switchTab('hidden')">Hidden <span id="hidden-tab-cnt">(0)</span></button>
</div>

<div id="search-wrap" class="search-wrap">
  <input id="search" type="text" placeholder="Filter by company or role..." oninput="filterRows(this.value)">
</div>

<div id="tab-quick" class="tab-panel active">
{_section("Quick Apply", quick, "quick")}
</div>

<div id="tab-account" class="tab-panel">
{_section("Account Required", account, "account")}
</div>

<div id="tab-saved" class="tab-panel">
  <div class="section saved-section">
    <h2>Saved</h2>
    <table>
      <thead><tr><th>Age</th><th>Company</th><th>Role</th><th>Location</th><th>ATS</th><th></th><th></th><th></th></tr></thead>
      <tbody id="saved-tbody"></tbody>
    </table>
  </div>
</div>

<div id="tab-submitted" class="tab-panel">
  <div class="section submitted-section">
    <h2>Submitted</h2>
    <table>
      <thead><tr><th>Age</th><th>Company</th><th>Role</th><th>Location</th><th>ATS</th><th>Submitted</th><th></th><th></th></tr></thead>
      <tbody id="submitted-tbody"></tbody>
    </table>
  </div>
</div>

<div id="tab-hidden" class="tab-panel">
  <div class="section hidden-section">
    <h2>Hidden</h2>
    <table>
      <thead><tr><th>Age</th><th>Company</th><th>Role</th><th>Location</th><th>ATS</th><th></th><th></th><th></th></tr></thead>
      <tbody id="hidden-tbody"></tbody>
    </table>
  </div>
</div>

<script>
{_JS}
</script>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

INTERVAL_MINUTES = 60
OUT = Path("jobs.html")


def run_once(open_browser: bool = False) -> None:
    jobs = fetch_jobs()
    quick   = [j for j in jobs if j["ats"] in QUICK_ATS or j["ats"] not in ACCOUNT_ATS]
    account = [j for j in jobs if j["ats"] in ACCOUNT_ATS]
    print(f"  {len(quick)} quick apply  |  {len(account)} account required  |  {len(jobs)} total")
    OUT.write_text(generate_html(jobs), encoding="utf-8")
    print(f"  Written → {OUT.resolve()}")
    if open_browser:
        webbrowser.open(f"file://{OUT.resolve()}")


if __name__ == "__main__":
    watch = "--watch" in sys.argv

    print("Summer 2026 Internships — GitHub README parser")
    print("─" * 48)

    if not watch:
        run_once(open_browser=True)
    else:
        print(f"Watching — refreshing every {INTERVAL_MINUTES} min  (Ctrl+C to stop)")
        print(f"Open once: file://{OUT.resolve()}\n")
        try:
            while True:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching...")
                try:
                    run_once(open_browser=False)
                except Exception as e:
                    print(f"  Error: {e} — will retry next cycle")
                next_run = datetime.now() + timedelta(minutes=INTERVAL_MINUTES)
                print(f"  Next update at {next_run.strftime('%H:%M:%S')}\n")
                time.sleep(INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            print("\nStopped.")
