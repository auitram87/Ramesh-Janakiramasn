# Job application script

`job_apply.py` emails a tailored cover letter and your resume to each job in a CSV list, and logs every attempt so you never apply to the same job twice.

## Setup

```bash
cp profile.example.json profile.json   # edit with your details and resume path
cp jobs.example.csv jobs.csv           # replace with your real job list
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@example.com
export SMTP_PASSWORD='app-password'    # use an app password, not your login password
```

`profile.json` and `jobs.csv` hold personal data; keep them out of git (add them to `.gitignore`).

## Usage

```bash
python job_apply.py                    # dry run: prints every email, sends nothing
python job_apply.py --send             # send for real
python job_apply.py --send --limit 5   # send at most 5 this run
python job_apply.py --send --delay 60  # wait 60 s between emails
```

## Jobs CSV

Required columns: `title`, `company`, `email`. Optional: `job_id`, `location`, `url`, `description`.
Rows without a valid email address are skipped with a warning.

## Log

Each attempt is appended to `applications_log.csv` with status `sent` or `failed`. Jobs marked `sent` are skipped on later runs; failed ones are retried.

## Notes

- Only use this for applications you genuinely intend to send, and check each employer's instructions. Many job sites forbid automated submissions and some ask for applications through their own forms, which this script does not handle.
- The script is dry-run by default for that reason: review the output before adding `--send`.
