"""
resume_reviewer.py — Core logic for JD-driven resume selection and tailoring.

Called by `jobly review <jd-file>`. Reads base .tex resume files, calls
the Claude API to pick the best fit and produce a modified .tex, then
optionally compiles to PDF via pdflatex.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import anthropic

SYSTEM_PROMPT = """\
You are a senior engineering hiring manager at a top-tier tech company (Google, Stripe, \
Databricks, Snowflake, etc.) with 500+ software engineering interviews conducted and \
thousands of resumes screened. You have deep expertise in backend systems, distributed \
systems, infrastructure, full-stack engineering, and ML systems.

Given a job description and several resume .tex source files, you will:
1. Select the best-fit base resume from the provided files
2. Apply your full evaluation framework internally — reason through alignment analysis, \
gap analysis, bullet rewrites, strategic repositioning, and ATS optimization — but do \
NOT write out the full analysis in your response
3. Produce a modified .tex file and a concise summary

Evaluation principles:
- Think like a hiring manager making a yes/no decision in 30 seconds
- Rewrite bullets to emphasize: quantified impact, technical specificity, scale, \
ownership, systems-level thinking — zero filler words
- Identify and fix ATS keyword gaps (missing required technologies, skills, domain terms)
- Reorder the skills section so the most JD-relevant categories and items lead
- Reorder projects so the most relevant to this specific JD appears first
- Replace weak ownership language ("partnered with", "helped", "built features", \
"assisted") with strong verbs — the candidate owned the work
- Never inflate scores or pad bullets with encouragement
- Add % VERIFY: comments on their own line ABOVE the \resumeItem they refer to — \
never inside a \resumeItem{...} brace pair, as % in LaTeX comments out the \
rest of the line including any closing brace
- Add % WHY: comments on their own line above the relevant block
- Never alter LaTeX command names — preserve \Huge, \large, \textbf, etc. exactly \
as they appear in the source; do not drop backslashes or rename commands

HARD RULE — no fabricated technologies: You may NEVER add a language, framework, \
tool, library, or technology to the resume (skills section, bullet points, or \
project headings) that does not already appear in the experience bank or the base \
.tex files. ATS optimization means surfacing and reordering what the candidate \
already knows — it never means inventing skills they haven't listed. If a JD \
requires a technology the candidate hasn't used, note the gap in the SUMMARY but \
do NOT add it to the .tex output. The candidate will be interviewed on everything \
that appears on their resume; putting unknown technologies there causes interview \
failure and harms them.

When a Full Experience Bank is provided in the user message, treat it as the \
authoritative source of all available content. Select the optimal subset of projects \
and work entries for this specific JD — you are not limited to what appears in the \
base .tex files. Lightly reword bullets to sharpen relevance and strength; never \
fabricate facts or metrics. Use the base .tex files only as LaTeX \
structure/formatting references — copy their document class, packages, custom \
commands, and layout, but replace content freely from the bank.

Output format — use these EXACT delimiters. Output nothing outside them:

---COMPANY---
[company name for output filename — CamelCase or single word, no spaces or special chars]
---COMPANY_END---

---CHOSEN---
[the exact filename of the chosen base .tex, e.g. resume_data_intern.tex]
---CHOSEN_END---

---SUMMARY---
[5–8 concise bullet points covering:
  • which resume you chose and the one-sentence reason
  • 2–3 specific ATS/keyword gaps you found and fixed using existing skills
  • any required JD technologies NOT present in the bank (flag as gaps — do not add them)
  • 2–3 highest-impact bullet rewrites or reorderings you made
  • one-line competitiveness assessment for this role]
---SUMMARY_END---

---TEX---
[complete modified .tex file — full content, compilable with pdflatex]
---TEX_END---\
"""


def get_experience_bank(latex_dir: Path) -> str | None:
    """Read experiences.yaml from the parent of latex_dir (~/Desktop/LaTex/)."""
    bank_path = latex_dir.parent / "experiences.yaml"
    if bank_path.exists():
        return bank_path.read_text()
    return None


def get_base_tex_files(latex_dir: Path) -> dict[str, str]:
    """
    Read base intern resume .tex files.
    Excludes company-specific variants (e.g. resume_data_intern_Tapestry.tex).
    A base file has exactly 2 underscores in its stem: resume_X_intern.
    """
    files: dict[str, str] = {}
    for f in sorted(latex_dir.glob("resume_*_intern.tex")):
        if f.stem.count("_") == 2:
            files[f.name] = f.read_text()
    return files


def _extract_block(text: str, tag: str) -> str:
    pattern = rf"---{tag}---\n(.*?)---{tag}_END---"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _call_api(
    jd_text: str,
    tex_files: dict[str, str],
    api_key: str,
    experience_bank: str | None = None,
) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    tex_block = "\n\n".join(
        f"=== {name} ===\n{content}" for name, content in tex_files.items()
    )
    bank_section = (
        f"Full Experience Bank (YAML — select content from here):\n\n{experience_bank}\n\n---\n\n"
        if experience_bank else ""
    )
    user_message = (
        f"Job Description:\n\n{jd_text}\n\n---\n\n"
        f"{bank_section}"
        f"Base .tex files (use for LaTeX structure/formatting only):\n\n{tex_block}"
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _write_tex(latex_dir: Path, chosen: str, company: str, tex_content: str) -> Path:
    company_clean = re.sub(r"[^a-zA-Z0-9]", "_", company).strip("_")
    output_path = latex_dir / f"resume_{company_clean}.tex"
    output_path.write_text(tex_content)
    return output_path


def _compile_pdf(tex_path: Path) -> bool:
    """Compile .tex to PDF via pdflatex. Cleans up aux files. Returns True on success."""
    result = subprocess.run(
        [
            "pdflatex",
            "-interaction=nonstopmode",
            "-output-directory",
            str(tex_path.parent),
            str(tex_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tex_path.parent),
    )
    for ext in (".aux", ".log", ".out", ".fls", ".fdb_latexmk"):
        aux = tex_path.with_suffix(ext)
        if aux.exists():
            aux.unlink()
    return result.returncode == 0


@dataclass
class ReviewResult:
    company: str
    chosen: str
    summary: str
    tex_path: Path
    pdf_path: Path | None  # None if compilation was skipped or failed


def review_jd(
    jd_text: str,
    latex_dir: Path,
    api_key: str,
    compile_pdf: bool = True,
) -> ReviewResult:
    """
    Full pipeline: read base .tex files → call API → write output .tex → compile PDF.
    Raises ValueError if required outputs cannot be parsed from the model response.
    """
    tex_files = get_base_tex_files(latex_dir)
    if not tex_files:
        raise ValueError(f"No base resume .tex files found in {latex_dir}")

    experience_bank = get_experience_bank(latex_dir)
    raw = _call_api(jd_text, tex_files, api_key, experience_bank=experience_bank)

    company = _extract_block(raw, "COMPANY")
    chosen = _extract_block(raw, "CHOSEN")
    summary = _extract_block(raw, "SUMMARY")
    tex = _extract_block(raw, "TEX")

    if not tex:
        raise ValueError(
            "Could not parse ---TEX--- block from model response.\n"
            f"Raw response (first 500 chars):\n{raw[:500]}"
        )

    tex_path = _write_tex(latex_dir, chosen or "resume_data_intern", company or "Company", tex)

    pdf_path: Path | None = None
    if compile_pdf:
        success = _compile_pdf(tex_path)
        if success:
            pdf_path = tex_path.with_suffix(".pdf")

    return ReviewResult(
        company=company,
        chosen=chosen,
        summary=summary,
        tex_path=tex_path,
        pdf_path=pdf_path,
    )
