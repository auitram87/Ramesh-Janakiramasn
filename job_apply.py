#!/usr/bin/env python3
"""
job_apply.py - apply to jobs from a CSV list by emailing your application.

How it works
------------
1. Reads your profile from profile.json (name, contact details, resume path,
   cover-letter template).
2. Reads the jobs to apply to from jobs.csv (one row per job with a
   contact email address).
3. For each job that has not been applied to yet, builds a personalised cover
   letter, attaches your resume and sends it over SMTP.
4. Records every attempt in applications_log.csv so the same job is never
   applied to twice.

Safety
------
* Dry-run is the DEFAULT. Nothing is sent unless you pass --send.
* SMTP credentials are read from environment variables, never from files
  in the repository:
      SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD

Usage
-----
    python job_apply.py                     # dry run, shows what would be sent
    python job_apply.py --send              # actually send the emails
    python job_apply.py --send --limit 5    # send at most 5 applications
    python job_apply.py --jobs other.csv --profile me.json

Standard library only; Python 3.8+.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import mimetypes
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from string import Template

LOG_FIELDS = ["applied_at", "status", "company", "title", "email", "job_id", "detail"]
REQUIRED_PROFILE_KEYS = ["full_name", "email"]
REQUIRED_JOB_COLUMNS = ["title", "company", "email"]

log = logging.getLogger("job_apply")


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    email: str
    location: str = ""
    url: str = ""
    description: str = ""

    @property
    def key(self) -> str:
        """Stable identifier used to detect already-applied jobs."""
        return self.job_id or f"{self.email.lower()}|{self.title.lower()}|{self.company.lower()}"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_profile(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Profile not found: {path}. Copy profile.example.json to get started.")
    with path.open(encoding="utf-8") as fh:
        profile = json.load(fh)
    missing = [k for k in REQUIRED_PROFILE_KEYS if not profile.get(k)]
    if missing:
        raise SystemExit(f"Profile is missing required keys: {', '.join(missing)}")
    resume = profile.get("resume_path")
    if resume and not Path(resume).expanduser().exists():
        raise SystemExit(f"Resume file not found: {resume}")
    return profile


def load_jobs(path: Path) -> list[Job]:
    if not path.exists():
        raise SystemExit(f"Jobs file not found: {path}. Copy jobs.example.csv to get started.")
    jobs: list[Job] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_JOB_COLUMNS if c not in header]
        if missing:
            raise SystemExit(f"Jobs CSV is missing columns: {', '.join(missing)}")
        for line_no, row in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            if not row.get("email") or "@" not in row["email"]:
                log.warning("Line %d skipped: no valid email address", line_no)
                continue
            jobs.append(
                Job(
                    job_id=row.get("job_id", ""),
                    title=row["title"],
                    company=row["company"],
                    email=row["email"],
                    location=row.get("location", ""),
                    url=row.get("url", ""),
                    description=row.get("description", ""),
                )
            )
    return jobs


def load_applied_keys(log_path: Path) -> set[str]:
    """Keys of jobs already applied to successfully (failed attempts are retried)."""
    if not log_path.exists():
        return set()
    done: set[str] = set()
    with log_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("status") == "sent":
                done.add(row.get("job_id") or f"{row['email'].lower()}|{row['title'].lower()}|{row['company'].lower()}")
    return done


# --------------------------------------------------------------------------- #
# Building the application
# --------------------------------------------------------------------------- #
DEFAULT_COVER_LETTER = """Dear Hiring Team at $company,

I am writing to apply for the $title position$location. $summary

Thank you for considering my application. I would welcome the chance to discuss how I can contribute to $company.

Kind regards,
$full_name
$email$phone"""


def build_cover_letter(profile: dict, job: Job) -> str:
    template = Template(profile.get("cover_letter_template") or DEFAULT_COVER_LETTER)
    location = f" in {job.location}" if job.location else ""
    summary = profile.get("summary", "").strip()
    phone = f"\n{profile['phone']}" if profile.get("phone") else ""
    return template.safe_substitute(
        company=job.company,
        title=job.title,
        location=location,
        summary=summary,
        full_name=profile["full_name"],
        email=profile["email"],
        phone=phone,
    )


def build_message(profile: dict, job: Job, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f'{profile["full_name"]} <{profile["email"]}>'
    msg["To"] = job.email
    msg["Subject"] = profile.get("subject_template", "Application for {title} - {full_name}").format(
        title=job.title, company=job.company, full_name=profile["full_name"]
    )
    msg.set_content(body)

    resume = profile.get("resume_path")
    if resume:
        path = Path(resume).expanduser()
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def smtp_config() -> dict:
    cfg = {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
    }
    if not cfg["host"] or not cfg["user"] or not cfg["password"]:
        raise SystemExit(
            "Set SMTP_HOST, SMTP_USER and SMTP_PASSWORD environment variables before using --send."
        )
    return cfg


def send_message(cfg: dict, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(cfg["user"], cfg["password"])
        server.send_message(msg)


def append_log(log_path: Path, row: dict) -> None:
    new_file = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in LOG_FIELDS})


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply to jobs listed in a CSV by email.")
    p.add_argument("--jobs", type=Path, default=Path("jobs.csv"), help="CSV of jobs (default: jobs.csv)")
    p.add_argument("--profile", type=Path, default=Path("profile.json"), help="profile JSON (default: profile.json)")
    p.add_argument("--log", type=Path, default=Path("applications_log.csv"), help="application log CSV")
    p.add_argument("--send", action="store_true", help="actually send emails (default is dry run)")
    p.add_argument("--limit", type=int, default=0, help="max applications to attempt this run (0 = no limit)")
    p.add_argument("--delay", type=float, default=20.0, help="seconds to wait between sends (default 20)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    profile = load_profile(args.profile)
    jobs = load_jobs(args.jobs)
    already = load_applied_keys(args.log)

    pending = [j for j in jobs if j.key not in already]
    log.info("%d jobs in file, %d already applied, %d pending", len(jobs), len(jobs) - len(pending), len(pending))
    if args.limit > 0:
        pending = pending[: args.limit]

    if not pending:
        log.info("Nothing to do.")
        return 0

    if not args.send:
        log.info("DRY RUN - no emails will be sent. Pass --send to apply for real.")
        cfg = None
    else:
        cfg = smtp_config()

    sent = failed = 0
    for i, job in enumerate(pending):
        body = build_cover_letter(profile, job)
        if not args.send:
            print(f"\n--- [{i + 1}/{len(pending)}] To: {job.email} | {job.title} at {job.company}")
            print(body)
            continue

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        base = {"applied_at": stamp, "company": job.company, "title": job.title,
                "email": job.email, "job_id": job.key}
        try:
            send_message(cfg, build_message(profile, job, body))
            append_log(args.log, {**base, "status": "sent", "detail": ""})
            sent += 1
            log.info("Sent: %s at %s -> %s", job.title, job.company, job.email)
        except (smtplib.SMTPException, OSError) as exc:
            append_log(args.log, {**base, "status": "failed", "detail": str(exc)[:200]})
            failed += 1
            log.error("Failed: %s at %s: %s", job.title, job.company, exc)

        if i < len(pending) - 1 and args.delay > 0:
            time.sleep(args.delay)

    if args.send:
        log.info("Finished: %d sent, %d failed. Log: %s", sent, failed, args.log)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
