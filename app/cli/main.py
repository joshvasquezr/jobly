"""
jobly CLI — all commands.

Commands:
  auth     Gmail OAuth + Playwright install check
  fetch    Pull newest SWEList email(s), parse, deduplicate, store
  queue    Score + filter job_posts, show review table, write queue
  run      Process queue: open → fill → review → [LLM eval] → YES gate
  status   Rich dashboard: counts by status, recent runs
  open     Open job URL in default browser
  reset    Re-queue a job (reset application status)
  config   Print all resolved config paths and values
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich import box
from rich.text import Text
from sqlmodel import col, select

from app.adapters import get_adapter
from app.gmail.auth import authenticate, check_credentials_file
from app.gmail.client import fetch_digest_emails
from app.gmail.parser import parse_email_html
from app.llm.evaluator import evaluate_application
from app.models.schema import (
    Application,
    ApplicationRun,
    ApplicationStatus,
    Artifact,
    ArtifactType,
    Email,
    EmailStatus,
    JobPost,
    JobStatus,
    LLMRecommendation,
    RunStatus,
    init_db,
    get_session,
    upsert_answer,
    find_cached_answer,
)
from app.utils.browser import browser_session, random_wait, save_html, save_screenshot
from app.utils.config import AppConfig, load_config, load_profile
from app.utils.filter import score_job
from app.utils.logging import get_logger, setup_logging

log = get_logger(__name__)
console = Console()
app = typer.Typer(
    name="jobly",
    help="Automated internship application workflow from SWEList digest emails.",
    no_args_is_help=True,
)

# ─── Config state ─────────────────────────────────────────────────────────────

_cfg: Optional[AppConfig] = None


def get_cfg(config_path: Optional[Path] = None) -> AppConfig:
    global _cfg
    if _cfg is None:
        _cfg = load_config(config_path)
        _cfg.ensure_dirs()
        setup_logging(_cfg.log_dir)
        init_db(str(_cfg.db_path))
    return _cfg


# ─── Shared storage helper ────────────────────────────────────────────────────


def _store_jobs(
    session,
    jobs: list,
    source_email_db_id: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Deduplicate and store a list of ParsedJob into the DB.
    Returns (total, new_count).
    """
    total = len(jobs)
    new_count = 0
    for job in jobs:
        existing = session.exec(
            select(JobPost).where(JobPost.url_hash == job.url_hash)
        ).first()
        if existing:
            console.print(f"    [dim]↩  Duplicate: {job.company} — {job.title}[/dim]")
            continue
        if dry_run:
            console.print(
                f"    [dim](dry-run)[/dim] {job.company} — {job.title} [{job.ats_type}]"
            )
            new_count += 1
            continue
        jp = JobPost(
            url_hash=job.url_hash,
            company=job.company,
            title=job.title,
            location=job.location,
            url=job.url,
            ats_type=job.ats_type,
            source_email_id=source_email_db_id,
            discovered_at=job.discovered_at,
            status=JobStatus.discovered,
        )
        session.add(jp)
        new_count += 1
    return total, new_count


# ─── Question resolver ────────────────────────────────────────────────────────


class QuestionResolver:
    """Resolves unknown ATS form fields: DB cache → user prompt → save."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._run_cache: dict[str, str] = {}

    def get_cached_answer(self, label: str, ats_type: str) -> str:
        """Return a cached answer for the given label+ats_type, or empty string."""
        key = f"{ats_type}::{label.strip().lower()}"
        return self._run_cache.get(key, "")

    def __call__(
        self,
        label: str,
        ats_type: str,
        options: list[str],
        context: str = "",
    ) -> str:
        key = f"{ats_type}::{label.strip().lower()}"

        # 1. In-run cache
        if key in self._run_cache:
            console.print(f"  [dim]↩  Using cached answer for '[italic]{label}[/italic]'[/dim]")
            return self._run_cache[key]

        # 2. DB cache
        with get_session(str(self._cfg.db_path)) as session:
            cached = find_cached_answer(session, label, ats_type)
            if cached:
                console.print(
                    f"  [dim]↩  Using saved answer for '[italic]{label}[/italic]': "
                    f"{cached.answer[:60]}[/dim]"
                )
                self._run_cache[key] = cached.answer
                return cached.answer

        # 3. Ask user
        console.print()
        console.print(Panel(
            f"[bold yellow]Unknown field[/bold yellow]\n"
            f"[bold]{label}[/bold]\n"
            + (f"[dim]{context}[/dim]" if context and context != label else ""),
            title=f"[cyan]{ats_type.upper()} — Custom Question[/cyan]",
            border_style="yellow",
        ))
        if options:
            console.print("[dim]Options:[/dim]")
            for i, opt in enumerate(options, 1):
                console.print(f"  [cyan]{i}.[/cyan] {opt}")

        answer = Prompt.ask("[bold]Your answer[/bold]")

        # 4. Save to DB
        with get_session(str(self._cfg.db_path)) as session:
            upsert_answer(session, label, ats_type, answer)

        self._run_cache[key] = answer
        return answer


# ─── Commands ─────────────────────────────────────────────────────────────────


@app.command()
def auth(
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
) -> None:
    """Authenticate with Gmail (OAuth2) and check Playwright installation."""
    cfg = get_cfg(config_path)

    console.print(Panel("[bold cyan]jobly auth[/bold cyan]", border_style="cyan"))

    # ── Check credentials file ────────────────────────────────────────────────
    console.print("\n[bold]1. Google OAuth credentials[/bold]")
    if check_credentials_file(cfg.credentials_path):
        console.print(f"  [green]✓[/green] credentials.json found: {cfg.credentials_path}")
    else:
        console.print(f"  [red]✗[/red] credentials.json not found at: {cfg.credentials_path}")
        console.print(
            "\n  [yellow]Setup steps:[/yellow]\n"
            "  1. Go to https://console.cloud.google.com/\n"
            "  2. Create a project → APIs & Services → Enable Gmail API\n"
            "  3. OAuth consent screen → External → Desktop app\n"
            "  4. Credentials → Create → OAuth client ID → Desktop → Download JSON\n"
            f"  5. Save as: {cfg.credentials_path}\n"
            "  6. Re-run: jobly auth"
        )
        raise typer.Exit(1)

    # ── Run OAuth flow ────────────────────────────────────────────────────────
    console.print("\n[bold]2. Gmail OAuth token[/bold]")
    try:
        creds = authenticate(cfg.credentials_path, cfg.token_path)
        console.print(f"  [green]✓[/green] Authenticated. Token saved to: {cfg.token_path}")
    except Exception as e:
        console.print(f"  [red]✗[/red] OAuth failed: {e}")
        raise typer.Exit(1)

    # ── Check Playwright ──────────────────────────────────────────────────────
    console.print("\n[bold]3. Playwright browser[/bold]")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            console.print("  [green]✓[/green] Playwright Chromium installed")
        else:
            console.print(f"  [yellow]![/yellow] Playwright install output: {result.stderr[:200]}")
    except Exception as e:
        console.print(f"  [yellow]![/yellow] Could not verify Playwright: {e}")

    # ── Resume file ───────────────────────────────────────────────────────────
    console.print("\n[bold]4. Resume file[/bold]")
    try:
        resume = cfg.get_resume()
        console.print(f"  [green]✓[/green] Resume found: {resume}")
    except (ValueError, FileNotFoundError) as e:
        console.print(f"  [yellow]![/yellow] {e}")

    console.print("\n[bold green]Auth check complete.[/bold green]")


@app.command()
def fetch(
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    source: Annotated[str, typer.Option("--source")] = "github",
) -> None:
    """Fetch new jobs and store in DB. Sources: github (default), gmail, all."""
    cfg = get_cfg(config_path)
    console.print(Panel("[bold cyan]jobly fetch[/bold cyan]", border_style="cyan"))

    valid_sources = {"github", "gmail", "all"}
    if source not in valid_sources:
        console.print(
            f"[red]Invalid source:[/red] {source!r}. Choose from: {', '.join(sorted(valid_sources))}"
        )
        raise typer.Exit(1)

    total_jobs = 0
    total_new = 0

    # ── GitHub README source ──────────────────────────────────────────────────
    if source in ("github", "all"):
        from app.sources.github_readme import fetch_github_jobs

        console.print("  [bold]Source: GitHub README[/bold]")
        try:
            jobs = fetch_github_jobs(cfg.filter)
        except RuntimeError as e:
            console.print(f"  [red]GitHub fetch error:[/red] {e}")
        else:
            console.print(f"  Fetched [bold]{len(jobs)}[/bold] relevant job(s)")
            with get_session(str(cfg.db_path)) as session:
                t, n = _store_jobs(session, jobs, source_email_db_id=None, dry_run=dry_run)
                if not dry_run:
                    session.commit()
            total_jobs += t
            total_new += n

    # ── Gmail source ──────────────────────────────────────────────────────────
    if source in ("gmail", "all"):
        console.print("  [bold]Source: Gmail[/bold]")
        try:
            creds = authenticate(cfg.credentials_path, cfg.token_path)
        except FileNotFoundError as e:
            console.print(f"  [red]Error:[/red] {e}")
            raise typer.Exit(1)

        with get_session(str(cfg.db_path)) as session:
            seen_ids = set(
                row.gmail_id for row in session.exec(select(Email)).all()
            )
            console.print(
                f"  Querying Gmail for [bold]{cfg.gmail.sender_filter}[/bold] "
                f"(lookback: {cfg.gmail.lookback_days}d) ..."
            )
            try:
                raw_emails = fetch_digest_emails(creds, cfg.gmail, already_seen=seen_ids)
            except RuntimeError as e:
                console.print(f"  [red]Gmail error:[/red] {e}")
                raise typer.Exit(1)

            if not raw_emails:
                console.print("  [yellow]No new emails found.[/yellow]")
            else:
                console.print(f"  Found [bold]{len(raw_emails)}[/bold] new email(s)")

                for raw in raw_emails:
                    console.print(
                        f"\n  Processing: [bold]{raw.subject}[/bold] ({raw.received_at.date()})"
                    )

                    email_record = None
                    if not dry_run:
                        email_record = Email(
                            gmail_id=raw.gmail_id,
                            thread_id=raw.thread_id,
                            subject=raw.subject,
                            sender=raw.sender,
                            received_at=raw.received_at,
                            raw_html=raw.html_body,
                            status=EmailStatus.raw,
                        )
                        session.add(email_record)
                        session.commit()

                    jobs = parse_email_html(raw.html_body, source_email_id=raw.gmail_id)
                    console.print(f"  Parsed [bold]{len(jobs)}[/bold] job(s)")
                    total_jobs += len(jobs)

                    source_db_id = email_record.id if email_record else None
                    t, n = _store_jobs(
                        session, jobs, source_email_db_id=source_db_id, dry_run=dry_run
                    )
                    total_new += n

                    if not dry_run:
                        email_record = session.exec(
                            select(Email).where(Email.gmail_id == raw.gmail_id)
                        ).first()
                        if email_record:
                            email_record.status = EmailStatus.parsed
                            email_record.processed_at = datetime.utcnow()
                        session.commit()

    console.print(
        f"\n[bold green]Fetch complete.[/bold green] "
        f"{total_jobs} total, [bold]{total_new}[/bold] new jobs stored."
    )


@app.command()
def watch(
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
    interval: Annotated[int, typer.Option("--interval")] = 120,
    source: Annotated[str, typer.Option("--source")] = "github",
) -> None:
    """Poll for new jobs in a foreground loop every INTERVAL minutes (Ctrl+C to stop)."""
    cfg = get_cfg(config_path)
    console.print(Panel(
        f"[bold cyan]jobly watch[/bold cyan]\n"
        f"Polling every [bold]{interval}[/bold] min  •  source=[bold]{source}[/bold]\n"
        "Press [bold]Ctrl+C[/bold] to stop.",
        border_style="cyan",
    ))

    from app.sources.github_readme import fetch_github_jobs

    try:
        while True:
            now = datetime.utcnow()
            console.print(
                f"\n[dim]{now.strftime('%Y-%m-%d %H:%M:%S UTC')}[/dim] "
                f"Fetching from [bold]{source}[/bold]..."
            )

            if source in ("github", "all"):
                try:
                    jobs = fetch_github_jobs(cfg.filter)
                    console.print(f"  Fetched [bold]{len(jobs)}[/bold] relevant job(s)")
                    with get_session(str(cfg.db_path)) as session:
                        _t, n = _store_jobs(session, jobs)
                        session.commit()
                    console.print(f"  [bold green]{n}[/bold green] new job(s) stored")
                except RuntimeError as e:
                    console.print(f"  [red]GitHub fetch error:[/red] {e}")

            next_check = datetime.utcnow() + timedelta(minutes=interval)
            console.print(
                f"  [dim]Next check at {next_check.strftime('%H:%M:%S UTC')} "
                f"(in {interval} min)[/dim]"
            )
            time.sleep(interval * 60)

    except KeyboardInterrupt:
        console.print("\n[yellow]Watch stopped.[/yellow]")


@app.command()
def queue(
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
    min_score: Annotated[Optional[float], typer.Option("--min-score")] = None,
    show_filtered: Annotated[bool, typer.Option("--show-filtered")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Score and filter discovered jobs, show review table, queue approved jobs."""
    cfg = get_cfg(config_path)
    if min_score is not None:
        cfg.filter.min_score = min_score

    console.print(Panel("[bold cyan]jobly queue[/bold cyan]", border_style="cyan"))

    with get_session(str(cfg.db_path)) as session:
        discovered = session.exec(
            select(JobPost).where(JobPost.status == JobStatus.discovered)
        ).all()

        if not discovered:
            console.print("  [yellow]No discovered jobs to score. Run `jobly fetch` first.[/yellow]")
            return

        console.print(f"  Scoring [bold]{len(discovered)}[/bold] discovered job(s)...")

        to_queue: list[JobPost] = []
        filtered_out: list[JobPost] = []

        for jp in discovered:
            result = score_job(
                title=jp.title,
                company=jp.company,
                location=jp.location,
                ats_type=jp.ats_type,
                cfg=cfg.filter,
            )
            jp.fit_score = result.score
            jp.fit_reason = result.reason
            if result.should_queue:
                to_queue.append(jp)
            else:
                filtered_out.append(jp)
            session.add(jp)

        session.commit()

        # ── Review table ──────────────────────────────────────────────────────
        _print_job_table(
            to_queue,
            title=f"[green]Will Queue ({len(to_queue)} jobs)[/green]",
            show_score=True,
        )

        if show_filtered or filtered_out:
            _print_job_table(
                filtered_out,
                title=f"[yellow]Filtered Out ({len(filtered_out)} jobs, score < {cfg.filter.min_score})[/yellow]",
                show_score=True,
                dim=True,
            )

        if not to_queue:
            console.print("\n[yellow]No jobs pass the score threshold. Adjust --min-score or keywords.[/yellow]")
            return

        # ── Confirm ───────────────────────────────────────────────────────────
        if not yes:
            confirmed = Confirm.ask(
                f"\nQueue [bold]{len(to_queue)}[/bold] job(s) for application?",
                default=True,
            )
            if not confirmed:
                console.print("[yellow]Cancelled.[/yellow]")
                return

        # ── Update statuses ───────────────────────────────────────────────────
        queued_count = 0
        for jp in to_queue:
            jp.status = JobStatus.queued
            # Create application record
            existing_app = session.exec(
                select(Application).where(
                    Application.job_post_id == jp.id,
                    col(Application.status).in_([
                        ApplicationStatus.queued, ApplicationStatus.started,
                        ApplicationStatus.filled, ApplicationStatus.needs_review,
                    ]),
                )
            ).first()
            if not existing_app:
                app_record = Application(
                    job_post_id=jp.id,
                    ats_type=jp.ats_type,
                    status=ApplicationStatus.queued,
                )
                session.add(app_record)
                queued_count += 1
            session.add(jp)

        for jp in filtered_out:
            jp.status = JobStatus.filtered_out
            session.add(jp)

        session.commit()

    console.print(
        f"\n[bold green]Queued {queued_count} application(s).[/bold green] "
        "Run `jobly run` to start applying."
    )


@app.command()
def run(
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
    resume_variant: Annotated[Optional[str], typer.Option("--resume")] = None,
    skip_llm: Annotated[bool, typer.Option("--skip-llm")] = False,
    limit: Annotated[Optional[int], typer.Option("--limit", "-n")] = None,
) -> None:
    """Process queued applications: open → fill → review → [LLM eval] → YES gate."""
    cfg = get_cfg(config_path)

    try:
        resume_path = cfg.get_resume(resume_variant)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Resume error:[/red] {e}")
        raise typer.Exit(1)

    try:
        profile = load_profile(cfg.profile_path)
    except FileNotFoundError as e:
        console.print(f"[red]Profile error:[/red] {e}")
        raise typer.Exit(1)

    console.print(Panel("[bold cyan]jobly run[/bold cyan]", border_style="cyan"))
    console.print(f"  Resume: [bold]{resume_path}[/bold]")
    console.print(f"  Profile: [bold]{cfg.profile_path}[/bold]")

    with get_session(str(cfg.db_path)) as session:
        queued_apps = session.exec(
            select(Application)
            .where(Application.status == ApplicationStatus.queued)
            .order_by(col(Application.created_at))
        ).all()

        if limit:
            queued_apps = queued_apps[:limit]

        if not queued_apps:
            console.print(
                "\n[yellow]No queued applications. Run `jobly fetch` then `jobly queue` first.[/yellow]"
            )
            return

        console.print(f"\n  [bold]{len(queued_apps)}[/bold] application(s) queued.\n")

        # ── Create run record ─────────────────────────────────────────────────
        run_record = ApplicationRun(status=RunStatus.running)
        session.add(run_record)
        session.commit()

        resolver = QuestionResolver(cfg)
        stats = {"submitted": 0, "skipped": 0, "error": 0}

        try:
            for i, app_record in enumerate(queued_apps, 1):
                job = session.get(JobPost, app_record.job_post_id)
                if not job:
                    continue

                console.rule(f"[bold cyan]Application {i}/{len(queued_apps)}[/bold cyan]")
                _print_job_summary(job)

                # Confirm before opening browser
                proceed = Confirm.ask("  Open and fill this application?", default=True)
                if not proceed:
                    app_record.status = ApplicationStatus.skipped
                    app_record.updated_at = datetime.utcnow()
                    session.commit()
                    stats["skipped"] += 1
                    run_record.jobs_skipped += 1
                    continue

                # ── Process ───────────────────────────────────────────────────
                success = asyncio.run(
                    _process_application(
                        app_record=app_record,
                        job=job,
                        cfg=cfg,
                        profile=profile,
                        resume_path=resume_path,
                        resolver=resolver,
                        session=session,
                        run_record=run_record,
                        skip_llm=skip_llm,
                    )
                )

                if success is True:
                    stats["submitted"] += 1
                elif success is False:
                    stats["skipped"] += 1
                else:
                    stats["error"] += 1

                run_record.jobs_processed += 1
                session.commit()

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted. Progress saved.[/yellow]")
            run_record.status = RunStatus.interrupted
        else:
            run_record.status = RunStatus.completed

        run_record.finished_at = datetime.utcnow()
        run_record.jobs_submitted = stats["submitted"]
        run_record.jobs_skipped = stats["skipped"]
        run_record.jobs_errored = stats["error"]
        session.commit()

    console.print()
    console.print(Panel(
        f"[bold green]Run complete[/bold green]\n"
        f"Submitted: [green]{stats['submitted']}[/green]  "
        f"Skipped: [yellow]{stats['skipped']}[/yellow]  "
        f"Errors: [red]{stats['error']}[/red]",
        border_style="green",
    ))


async def _process_application(
    app_record: Application,
    job: JobPost,
    cfg: AppConfig,
    profile: dict,
    resume_path: Path,
    resolver: QuestionResolver,
    session,
    run_record: ApplicationRun,
    skip_llm: bool,
) -> Optional[bool]:
    """
    Core async application loop.
    Returns True (submitted), False (skipped), or None (error).
    """
    adapter = get_adapter(job.url)

    if not adapter:
        console.print(
            f"  [yellow]No adapter for ATS type '{job.ats_type}'. Skipping.[/yellow]\n"
            f"  You can open it manually: [link={job.url}]{job.url}[/link]"
        )
        app_record.status = ApplicationStatus.skipped
        app_record.error_message = f"No adapter for ATS: {job.ats_type}"
        app_record.updated_at = datetime.utcnow()
        session.commit()
        return False

    # Special notice for Workday
    if adapter.ats_type == "workday":
        from app.adapters.workday import WorkdayAdapter
        console.print(WorkdayAdapter.GUIDED_MODE_NOTICE)
        proceed = Confirm.ask("  Continue in guided mode?", default=True)
        if not proceed:
            app_record.status = ApplicationStatus.skipped
            session.commit()
            return False

    # ── Mark started ──────────────────────────────────────────────────────────
    app_record.status = ApplicationStatus.started
    app_record.run_id = run_record.id
    app_record.ats_type = adapter.ats_type
    app_record.updated_at = datetime.utcnow()
    session.commit()

    async with browser_session(cfg.browser, cfg.artifacts_dir) as (context, page):
        label = f"{job.company}_{job.id[:8]}"
        try:
            # ── Open ──────────────────────────────────────────────────────────
            console.print(f"  Opening [bold]{job.url}[/bold] ...")
            await adapter.open_and_prepare(page, job.url)
            await random_wait(cfg.browser)

            # ── Fill ──────────────────────────────────────────────────────────
            console.print("  Filling form ...")
            fill_result = await adapter.fill_form(page, profile, resume_path, resolver)

            _print_fill_summary(fill_result)
            app_record.status = ApplicationStatus.filled
            app_record.set_answers({
                **{f: "filled" for f in fill_result.filled_fields},
                **{q.label: "[user-provided]" for q in fill_result.unknown_questions},
            })
            app_record.updated_at = datetime.utcnow()
            session.commit()

            # ── Navigate to review ────────────────────────────────────────────
            # For Workday: manual gate
            if adapter.ats_type == "workday":
                console.print(
                    "\n  [bold yellow]Workday guided mode:[/bold yellow] "
                    "Please complete the form in the browser,\n"
                    "  then come back here when you reach the review/confirm page."
                )
                input("  Press ENTER when you're on the review page...")
            else:
                console.print("  Navigating to review step ...")
                await adapter.reach_review_step(page)

            await save_screenshot(page, cfg.artifacts_dir, f"review_{label}")
            app_record.status = ApplicationStatus.needs_review
            app_record.screenshot_path = str(
                cfg.artifacts_dir / f"review_{label}_latest.png"
            )
            app_record.updated_at = datetime.utcnow()
            session.commit()

            # ── LLM evaluation ────────────────────────────────────────────────
            llm_rec = None
            if cfg.llm.enabled and not skip_llm:
                console.print("  Requesting LLM evaluation ...")
                custom_answers = {
                    q.label: resolver.get_cached_answer(q.label, adapter.ats_type)
                    for q in fill_result.unknown_questions
                }
                eval_result = await evaluate_application(
                    company=job.company,
                    title=job.title,
                    location=job.location,
                    ats_type=job.ats_type,
                    fit_score=job.fit_score,
                    fit_reason=job.fit_reason,
                    profile=profile,
                    submitted_fields={f: "filled" for f in fill_result.filled_fields},
                    custom_answers=custom_answers,
                    api_key=cfg.anthropic_api_key,
                    model=cfg.llm.model,
                    max_tokens=cfg.llm.max_tokens,
                )
                app_record.llm_recommendation = eval_result.recommendation
                app_record.llm_rationale = eval_result.rationale
                app_record.updated_at = datetime.utcnow()
                session.commit()
                _print_llm_result(eval_result)
                llm_rec = eval_result.recommendation

            # ── Human gate ────────────────────────────────────────────────────
            _print_submit_gate(job, llm_rec)
            confirmed = _ask_submit_confirmation()

            if confirmed:
                console.print("  [bold green]Submitting...[/bold green]")
                await adapter.submit(page)
                await save_screenshot(page, cfg.artifacts_dir, f"submitted_{label}")

                app_record.status = ApplicationStatus.submitted
                app_record.updated_at = datetime.utcnow()
                job.status = JobStatus.skipped  # don't re-queue
                session.add(job)
                session.commit()
                console.print("  [bold green]✓ Submitted![/bold green]")
                return True
            else:
                app_record.status = ApplicationStatus.skipped
                app_record.updated_at = datetime.utcnow()
                session.commit()
                console.print("  [yellow]Skipped.[/yellow]")
                return False

        except KeyboardInterrupt:
            # Graceful stop — leave application in current status
            snap_path = await save_html(page, cfg.artifacts_dir, f"interrupt_{label}")
            app_record.html_snapshot_path = str(snap_path)
            app_record.updated_at = datetime.utcnow()
            session.commit()
            raise

        except Exception as e:
            console.print(f"  [red]Error:[/red] {e}")
            log.exception("application_error", job_id=job.id, error=str(e))

            try:
                ss_path = await save_screenshot(page, cfg.artifacts_dir, f"error_{label}")
                html_path = await save_html(page, cfg.artifacts_dir, f"error_{label}")
                app_record.screenshot_path = str(ss_path)
                app_record.html_snapshot_path = str(html_path)

                art = Artifact(
                    application_id=app_record.id,
                    artifact_type=ArtifactType.screenshot,
                    file_path=str(ss_path),
                )
                session.add(art)
            except Exception:
                pass

            app_record.status = ApplicationStatus.error
            app_record.error_message = str(e)[:500]
            app_record.updated_at = datetime.utcnow()
            session.commit()
            run_record.jobs_errored += 1
            return None


@app.command()
def status(
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
) -> None:
    """Show application dashboard: counts by status and recent runs."""
    cfg = get_cfg(config_path)

    console.print(Panel("[bold cyan]jobly status[/bold cyan]", border_style="cyan"))

    with get_session(str(cfg.db_path)) as session:
        all_jobs = session.exec(select(JobPost)).all()
        all_apps = session.exec(select(Application)).all()
        all_runs = session.exec(
            select(ApplicationRun).order_by(col(ApplicationRun.started_at).desc()).limit(5)
        ).all()

    # ── Job posts by status ───────────────────────────────────────────────────
    job_counts: dict[str, int] = {}
    for jp in all_jobs:
        job_counts[jp.status] = job_counts.get(jp.status, 0) + 1

    jt = Table(title="Job Posts", box=box.ROUNDED)
    jt.add_column("Status", style="bold")
    jt.add_column("Count", justify="right")
    for status_val, count in sorted(job_counts.items()):
        color = {"queued": "cyan", "filtered_out": "dim", "discovered": "white"}.get(status_val, "white")
        jt.add_row(Text(status_val, style=color), str(count))
    console.print(jt)

    # ── Applications by status ────────────────────────────────────────────────
    app_counts: dict[str, int] = {}
    for a in all_apps:
        app_counts[a.status] = app_counts.get(a.status, 0) + 1

    at = Table(title="Applications", box=box.ROUNDED)
    at.add_column("Status", style="bold")
    at.add_column("Count", justify="right")
    status_colors = {
        "submitted": "green", "error": "red", "skipped": "yellow",
        "needs_review": "cyan", "filled": "blue", "started": "blue",
        "queued": "white",
    }
    for status_val, count in sorted(app_counts.items()):
        color = status_colors.get(status_val, "white")
        at.add_row(Text(status_val, style=color), str(count))
    console.print(at)

    # ── Recent runs ───────────────────────────────────────────────────────────
    if all_runs:
        rt = Table(title="Recent Runs", box=box.ROUNDED)
        rt.add_column("Started")
        rt.add_column("Status")
        rt.add_column("Submitted", justify="right", style="green")
        rt.add_column("Skipped", justify="right", style="yellow")
        rt.add_column("Errors", justify="right", style="red")
        for r in all_runs:
            rt.add_row(
                r.started_at.strftime("%Y-%m-%d %H:%M"),
                r.status,
                str(r.jobs_submitted),
                str(r.jobs_skipped),
                str(r.jobs_errored),
            )
        console.print(rt)

    # ── Queued applications ───────────────────────────────────────────────────
    with get_session(str(cfg.db_path)) as session:
        queued_apps = session.exec(
            select(Application)
            .where(Application.status == ApplicationStatus.queued)
            .limit(10)
        ).all()
        # Fetch associated job posts
        queued: list[tuple[Application, Optional[JobPost]]] = [
            (a, session.get(JobPost, a.job_post_id)) for a in queued_apps
        ]

    if queued:
        qt = Table(title=f"Queued Applications (next {len(queued)})", box=box.ROUNDED)
        qt.add_column("ID", style="dim", width=8)
        qt.add_column("Company")
        qt.add_column("Title")
        qt.add_column("ATS")
        qt.add_column("Score", justify="right")
        for app_r, job in queued:
            qt.add_row(
                app_r.id[:8],
                job.company if job else "?",
                (job.title[:50] if job else "?"),
                (job.ats_type or "?") if job else "?",
                f"{job.fit_score:.2f}" if job else "?",
            )
        console.print(qt)


@app.command(name="open")
def open_job(
    job_id: Annotated[str, typer.Argument(help="Job post ID (or prefix)")],
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
) -> None:
    """Open a job URL in the default browser."""
    cfg = get_cfg(config_path)

    with get_session(str(cfg.db_path)) as session:
        job = session.exec(
            select(JobPost).where(col(JobPost.id).startswith(job_id))
        ).first()
        if not job:
            # Try by application ID
            app_r = session.exec(
                select(Application).where(col(Application.id).startswith(job_id))
            ).first()
            if app_r:
                job = session.get(JobPost, app_r.job_post_id)

    if not job:
        console.print(f"[red]Job not found:[/red] {job_id}")
        raise typer.Exit(1)

    console.print(f"  Opening: [link={job.url}]{job.url}[/link]")
    webbrowser.open(job.url)


@app.command()
def reset(
    job_id: Annotated[str, typer.Argument(help="Job post ID or application ID (or prefix)")],
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
) -> None:
    """Re-queue a job (resets its application status back to queued)."""
    cfg = get_cfg(config_path)

    with get_session(str(cfg.db_path)) as session:
        # Find by application ID prefix first
        app_r = session.exec(
            select(Application).where(col(Application.id).startswith(job_id))
        ).first()

        if not app_r:
            # Try by job post ID
            job = session.exec(
                select(JobPost).where(col(JobPost.id).startswith(job_id))
            ).first()
            if job:
                app_r = session.exec(
                    select(Application).where(Application.job_post_id == job.id)
                ).first()

        if not app_r:
            console.print(f"[red]Application not found:[/red] {job_id}")
            raise typer.Exit(1)

        old_status = app_r.status
        app_r.status = ApplicationStatus.queued
        app_r.error_message = None
        app_r.updated_at = datetime.utcnow()
        session.commit()

        job = session.get(JobPost, app_r.job_post_id)
        console.print(
            f"  Reset [bold]{job.company if job else '?'} — {job.title if job else '?'}[/bold]\n"
            f"  Status: [dim]{old_status}[/dim] → [bold green]queued[/bold green]"
        )


@app.command(name="config")
def show_config(
    config_path: Annotated[Optional[Path], typer.Option("--config", "-c")] = None,
) -> None:
    """Print all resolved configuration paths and key settings."""
    cfg = get_cfg(config_path)

    t = Table(title="jobly Configuration", box=box.ROUNDED, show_header=False)
    t.add_column("Key", style="bold cyan")
    t.add_column("Value")

    rows = [
        ("config_dir", str(cfg.config_dir)),
        ("data_dir", str(cfg.data_dir)),
        ("db_path", str(cfg.db_path)),
        ("artifacts_dir", str(cfg.artifacts_dir)),
        ("log_dir", str(cfg.log_dir)),
        ("credentials_path", str(cfg.credentials_path)),
        ("token_path", str(cfg.token_path)),
        ("profile_path", str(cfg.profile_path)),
        ("resume_default_path", str(cfg.resume_default_path or "[not set]")),
        ("gmail.sender_filter", cfg.gmail.sender_filter),
        ("gmail.lookback_days", str(cfg.gmail.lookback_days)),
        ("filter.min_score", str(cfg.filter.min_score)),
        ("filter.preferred_ats", ", ".join(cfg.filter.preferred_ats)),
        ("browser.headless", str(cfg.browser.headless)),
        ("llm.enabled", str(cfg.llm.enabled)),
        ("llm.model", cfg.llm.model),
        ("anthropic_api_key", "[set]" if cfg.anthropic_api_key else "[not set]"),
    ]
    for k, v in rows:
        t.add_row(k, v)

    console.print(t)


# ─── Rich display helpers ──────────────────────────────────────────────────────


def _print_job_table(
    jobs: list[JobPost],
    title: str,
    show_score: bool = False,
    dim: bool = False,
) -> None:
    if not jobs:
        return
    t = Table(title=title, box=box.ROUNDED)
    t.add_column("#", style="dim", width=3)
    t.add_column("Company", style="bold" if not dim else "dim")
    t.add_column("Title", max_width=45)
    t.add_column("ATS", width=12)
    t.add_column("Location", width=15)
    if show_score:
        t.add_column("Score", justify="right", width=6)
        t.add_column("Reason", style="dim", max_width=35)
    for i, jp in enumerate(jobs, 1):
        row = [
            str(i),
            jp.company,
            jp.title,
            jp.ats_type or "?",
            jp.location or "",
        ]
        if show_score:
            score_str = f"{jp.fit_score:.2f}"
            row += [score_str, jp.fit_reason[:35]]
        t.add_row(*row)
    console.print(t)


def _print_job_summary(job: JobPost) -> None:
    console.print(Panel(
        f"[bold]{job.company}[/bold] — {job.title}\n"
        f"[dim]ATS:[/dim] {job.ats_type or '?'}  "
        f"[dim]Score:[/dim] {job.fit_score:.2f}  "
        f"[dim]Location:[/dim] {job.location or 'N/A'}\n"
        f"[link={job.url}]{job.url}[/link]",
        border_style="blue",
    ))


def _print_fill_summary(fill_result) -> None:
    filled = ", ".join(fill_result.filled_fields[:8]) or "none"
    skipped = ", ".join(fill_result.skipped_fields[:4]) or "none"
    console.print(
        f"  [green]Filled:[/green] {filled}\n"
        f"  [yellow]Skipped:[/yellow] {skipped}"
    )


def _print_llm_result(eval_result) -> None:
    rec = eval_result.recommendation
    color = "green" if rec == "RECOMMEND_SUBMIT" else "yellow" if rec == "RECOMMEND_SKIP" else "dim"
    icon = "✓" if rec == "RECOMMEND_SUBMIT" else "⚠" if rec == "RECOMMEND_SKIP" else "?"
    console.print(Panel(
        f"[{color}][bold]{icon} {rec}[/bold][/{color}] "
        f"[dim]({eval_result.confidence} confidence)[/dim]\n\n"
        f"{eval_result.rationale}"
        + (
            "\n\n[red]Red flags:[/red]\n" + "\n".join(f"• {f}" for f in eval_result.red_flags)
            if eval_result.red_flags else ""
        ),
        title="[cyan]LLM Evaluation[/cyan]",
        border_style="cyan",
    ))


def _print_submit_gate(job: JobPost, llm_recommendation: Optional[str]) -> None:
    console.print()
    console.print(Panel(
        f"[bold]Ready to submit:[/bold] {job.company} — {job.title}\n\n"
        + (
            f"LLM recommendation: [bold]{llm_recommendation}[/bold]\n\n"
            if llm_recommendation and llm_recommendation != "NA" else ""
        )
        + "[yellow bold]This will submit your application. This action cannot be undone.[/yellow bold]",
        title="[bold red]Submit Confirmation Required[/bold red]",
        border_style="red",
    ))


def _ask_submit_confirmation() -> bool:
    """
    Require the user to type YES (case-insensitive) to confirm submission.
    Any other input = skip.
    """
    console.print(
        "  Type [bold green]YES[/bold green] to submit, "
        "or anything else to skip: ",
        end="",
    )
    answer = input().strip()
    return answer.upper() == "YES"
