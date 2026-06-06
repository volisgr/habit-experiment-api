from dotenv import load_dotenv
import os

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

import uuid
import secrets
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Body, Path, BackgroundTasks, Request
from fastapi.responses import JSONResponse, HTMLResponse
import psycopg
from dateutil import parser as dateparser
import resend

app = FastAPI(title="Habit Experiment API")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL env var required")

resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@improvehabit.com")
REPLY_TO = "reply@parse.improvehabit.com"
BASE_URL = os.getenv("BASE_URL", "https://habit-experiment-api.onrender.com")

# --- Y/N PARSER ---
YES_PHRASES = {
    "y", "yes", "yep", "yeah", "yup", "yeap", "yes!", "yeah!", "yep!",
    "did", "done", "completed", "complete", "sure", "absolutely",
    "correct", "true", "1", "✓", "✅", "of course", "definitely",
    "affirmative", "indeed", "certainly", "ok", "okay"
}
NO_PHRASES = {
    "n", "no", "nope", "nah", "didn't", "didnt", "didn't", "not",
    "never", "false", "0", "negative", "noway", "no way", "not done",
    "didn't do it", "didnt do it", "missed", "skip", "skipped", "x", "❌"
}

def parse_yn(text: str):
    """Parse a single line as Y/N. Returns 1, 0, or None if unrecognizable."""
    cleaned = text.strip().lower().rstrip(".")
    if cleaned in YES_PHRASES:
        return 1
    if cleaned in NO_PHRASES:
        return 0
    return None


def get_db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)


# --- SEND FIRST EMAIL ---
def send_first_email(user_email: str, goal: str, experiment_id: str, start_date: str) -> bool:
    if not resend.api_key:
        print("❌ RESEND_API_KEY is missing")
        return False
    try:
        with get_db_conn() as conn:
            template = conn.execute(
                """
                SELECT habit_1, habit_2, habit_3, link_1, link_2, link_3, description
                FROM experiment_templates
                WHERE LOWER(goal) = LOWER(%s) AND approved = true
                LIMIT 1
                """,
                (goal,),
            ).fetchone()

            if not template:
                print("🚫 No approved template for goal:", goal)
                return False

            habits = [template["habit_1"], template["habit_2"], template["habit_3"]]
            links = [template["link_1"] or "", template["link_2"] or "", template["link_3"] or ""]
            description = template["description"] or "Behavioral research study."

            habits_text = "\n".join(
                [f"• {h} {'→ ' + l if l else ''}" for h, l in zip(habits, links)]
            )

            email_html = f"""
            <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px;">
                <h2>Your 7-Day Habit Experiment: {goal}</h2>
                <p>{description}</p>
                <p><strong>Your 3 habits for the next 7 days:</strong></p>
                <pre style="background: #f8f9fa; padding: 20px; border-radius: 6px;">{habits_text}</pre>
                <p>Every evening at 6 PM you'll receive a check-in email. Simply reply with <strong>Y</strong> or <strong>N</strong> for each habit.</p>
                <p><a href="{BASE_URL}/progress/{user_email}/{experiment_id}">View your progress</a></p>
            </div>
            """

            resend.Emails.send({
                "from": EMAIL_FROM,
                "to": user_email,
                "reply_to": REPLY_TO,
                "subject": f"Your 7-Day Habit Experiment: {goal}",
                "html": email_html,
            })
            print(f"✅ Welcome email sent to {user_email} for goal={goal}")
            return True

    except Exception as e:
        print(f"💥 Exception in send_first_email: {e}")
        return False


# --- GENERATE CHECKIN TOKEN ---
def create_checkin_token(conn, user_id: str, experiment_id: str, checkin_date: date) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO checkin_tokens (token, user_id, experiment_id, checkin_date)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (token, user_id, str(experiment_id), checkin_date),
    )
    return token


# --- BUILD CHECKIN EMAIL HTML ---
def build_checkin_email(
    user_id: str,
    experiment_id: str,
    habits: list,
    dates_to_include: list,  # list of (date, is_missed, bad_response)
) -> str:
    """Build the daily check-in email with AMP-style Y/N table."""

    rows = ""
    for checkin_date, is_missed, bad_response in dates_to_include:
        date_label = checkin_date.strftime("%A %b %d")
        if is_missed and bad_response:
            note = f'<br><small style="color:#e67e22;">⚠️ We couldn\'t understand your previous response — please reply Y or N</small>'
        elif is_missed:
            note = f'<br><small style="color:#999;">Missing from yesterday</small>'
        else:
            note = ""

        rows += f"""
        <tr>
          <td colspan="3" style="padding: 8px 12px; background:#f0f0f0; font-weight:bold; font-size:13px;">
            {date_label}{note}
          </td>
        </tr>
        """
        for i, habit in enumerate(habits, 1):
            rows += f"""
            <tr>
              <td style="padding: 10px 12px; border-bottom: 1px solid #eee;">{habit}</td>
              <td style="padding: 10px 12px; border-bottom: 1px solid #eee; text-align:center;">
                <strong>Habit {i}</strong>
              </td>
              <td style="padding: 10px 12px; border-bottom: 1px solid #eee; text-align:center; color:#999; font-size:12px;">
                Y / N
              </td>
            </tr>
            """

    habit_list = "\n".join([f"{i+1}. {h}" for i, h in enumerate(habits)])
    date_lines = "\n".join([
        f"{'[MISSED] ' if is_missed else ''}{d.strftime('%b %d')}: H1: Y/N, H2: Y/N, H3: Y/N"
        for d, is_missed, _ in dates_to_include
    ])

    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px;">
      <h2 style="color:#2c3e50;">Daily Habit Check-in</h2>
      <p>Did you complete your habits today? <strong>Reply to this email with Y or N for each habit, one per line.</strong></p>

      <table style="width:100%; border-collapse:collapse; margin: 20px 0;">
        <thead>
          <tr style="background:#2c3e50; color:white;">
            <th style="padding:10px 12px; text-align:left;">Habit</th>
            <th style="padding:10px 12px;">#</th>
            <th style="padding:10px 12px;">Response</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>

      <div style="background:#f8f9fa; padding:16px; border-radius:6px; margin:20px 0;">
        <p style="margin:0 0 8px 0;"><strong>How to reply:</strong></p>
        <p style="margin:0; font-family:monospace; white-space:pre;">{habit_list}

Reply with one Y or N per habit per day, like:
Y
N
Y</p>
      </div>

      <p style="color:#999; font-size:12px;">
        Reply to: {REPLY_TO}<br>
        <a href="{BASE_URL}/progress/{user_id}/{experiment_id}">View your full progress</a>
      </p>
    </div>
    """


# --- SEND DAILY CHECKIN EMAILS ---
def send_daily_checkins():
    """Called by pg_cron every minute — sends checkin emails to users where it's 6 PM in their timezone."""
    now_utc = datetime.now(ZoneInfo("UTC"))

    with get_db_conn() as conn:
        # Get all active experiments
        experiments = conn.execute(
            """
            SELECT e.id, e.user_id, e.start_date, e.end_date, up.goal, up.timezone
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            WHERE e.status = 'active'
              AND e.needs_email = false
            """
        ).fetchall()

        for exp in experiments:
            user_id = exp["user_id"]
            experiment_id = exp["id"]
            goal = exp["goal"]
            tz_name = exp["timezone"] or "UTC"
            start_date = exp["start_date"]
            end_date = exp["end_date"]

            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("UTC")

            now_local = now_utc.astimezone(tz)
            today_local = now_local.date()

            # Only send between 18:00 and 18:01 local time
            if not (now_local.hour == 18 and now_local.minute == 0):
                continue

            # Check if already sent today
            already_sent = conn.execute(
                """
                SELECT id FROM daily_checkin_jobs
                WHERE user_id = %s AND checkin_date = %s AND status = 'sent'
                """,
                (user_id, today_local),
            ).fetchone()

            if already_sent:
                continue

            # Only send during the 7-day experiment window
            if not (start_date <= today_local <= end_date):
                continue

            # Get template
            template = conn.execute(
                """
                SELECT habit_1, habit_2, habit_3
                FROM experiment_templates
                WHERE LOWER(goal) = LOWER(%s) AND approved = true
                LIMIT 1
                """,
                (goal,),
            ).fetchone()

            if not template:
                continue

            habits = [template["habit_1"], template["habit_2"], template["habit_3"]]

            # Find missed dates (no score recorded, no sent job or bad response)
            dates_to_include = []

            # Check yesterday and day before for missed responses
            for days_back in range(1, 3):
                check_date = today_local - timedelta(days=days_back)
                if check_date < start_date:
                    break

                # Was score recorded?
                score = conn.execute(
                    """
                    SELECT id FROM experiment_scores
                    WHERE experiment_id = %s AND date = %s
                    """,
                    (str(experiment_id), check_date),
                ).fetchone()

                if score:
                    break  # Don't go further back if we have a score

                # Was there a bad/unrecognized response?
                bad = conn.execute(
                    """
                    SELECT id FROM daily_checkin_jobs
                    WHERE user_id = %s AND checkin_date = %s AND status = 'sent'
                    """,
                    (user_id, check_date),
                ).fetchone()

                dates_to_include.insert(0, (check_date, True, bad is not None))

            # Add today
            dates_to_include.append((today_local, False, False))

            # Build subject
            day_num = (today_local - start_date).days + 1
            missed_count = len([d for d in dates_to_include if d[1]])
            subject = f"Day {day_num}/7 Habit Check-in"
            if missed_count:
                subject += f" (+ {missed_count} missed day{'s' if missed_count > 1 else ''})"

            email_html = build_checkin_email(
                user_id=user_id,
                experiment_id=str(experiment_id),
                habits=habits,
                dates_to_include=dates_to_include,
            )

            try:
                resend.Emails.send({
                    "from": EMAIL_FROM,
                    "to": user_id,
                    "reply_to": REPLY_TO,
                    "subject": subject,
                    "html": email_html,
                })

                # Record job as sent
                conn.execute(
                    """
                    INSERT INTO daily_checkin_jobs (checkin_date, user_id, experiment_id, status, sent_at)
                    VALUES (%s, %s, %s, 'sent', NOW())
                    ON CONFLICT (checkin_date, user_id) DO UPDATE SET status = 'sent', sent_at = NOW()
                    """,
                    (today_local, user_id, str(experiment_id)),
                )
                conn.commit()
                print(f"✅ Checkin email sent to {user_id} for {today_local}")

            except Exception as e:
                print(f"💥 Failed to send checkin to {user_id}: {e}")
                conn.execute(
                    """
                    INSERT INTO daily_checkin_jobs (checkin_date, user_id, experiment_id, status, error_msg)
                    VALUES (%s, %s, %s, 'failed', %s)
                    ON CONFLICT (checkin_date, user_id) DO UPDATE SET status = 'failed', error_msg = %s
                    """,
                    (today_local, user_id, str(experiment_id), str(e), str(e)),
                )
                conn.commit()


# --- PROCESS PENDING EMAIL JOBS ---
def process_first_email_jobs(goal: str = None):
    with get_db_conn() as conn:
        cur = conn.cursor()

        if goal:
            cur.execute(
                """
                SELECT id, goal FROM first_email_jobs
                WHERE lower(goal) = lower(%s) AND status = 'pending'
                FOR UPDATE SKIP LOCKED;
                """,
                (goal,),
            )
        else:
            cur.execute(
                """
                SELECT id, goal FROM first_email_jobs
                WHERE status = 'pending'
                FOR UPDATE SKIP LOCKED;
                """
            )

        jobs = cur.fetchall()
        if not jobs:
            print("ℹ️ No pending email jobs found")
            return {"processed": 0}

        processed = 0
        for job in jobs:
            job_id = job["id"]
            job_goal = job["goal"]

            try:
                cur.execute(
                    """
                    SELECT e.id, e.user_id, e.start_date, up.goal
                    FROM experiments e
                    JOIN user_profiles up ON up.user_id = e.user_id
                    WHERE e.status = 'active'
                      AND e.needs_email = true
                      AND lower(up.goal) = lower(%s);
                    """,
                    (job_goal,),
                )
                pending_exps = cur.fetchall()

                emails_sent = 0
                for exp in pending_exps:
                    template = conn.execute(
                        """
                        SELECT id FROM experiment_templates
                        WHERE lower(goal) = lower(%s) AND approved = true;
                        """,
                        (exp["goal"],),
                    ).fetchone()

                    if not template:
                        continue

                    success = send_first_email(
                        user_email=exp["user_id"],
                        goal=exp["goal"],
                        experiment_id=str(exp["id"]),
                        start_date=exp["start_date"].isoformat(),
                    )

                    if success:
                        conn.execute(
                            "UPDATE experiments SET needs_email = false WHERE id = %s",
                            (exp["id"],),
                        )
                        emails_sent += 1

                cur.execute(
                    """
                    UPDATE first_email_jobs
                    SET status = 'completed', completed_at = NOW()
                    WHERE id = %s;
                    """,
                    (job_id,),
                )
                conn.commit()
                print(f"✅ Job {job_id} completed: {emails_sent} emails sent for goal={job_goal}")
                processed += 1

            except Exception as e:
                print(f"❌ Error processing job {job_id}: {e}")
                conn.rollback()
                cur.execute(
                    """
                    UPDATE first_email_jobs
                    SET status = 'failed', error_msg = %s
                    WHERE id = %s;
                    """,
                    (str(e), job_id),
                )
                conn.commit()

        return {"processed": processed}


# ============================================================
# FASTAPI ENDPOINTS
# ============================================================

# --- SUBSCRIBE ---
@app.post("/subscribe")
async def subscribe(
    email: str = Body(..., embed=True, min_length=5),
    goal: str = Body(..., embed=True, min_length=3),
):
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Invalid email format")

    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_profiles (user_id, goal, timezone, created_at)
            VALUES (%s, %s, 'UTC', NOW())
            ON CONFLICT (user_id) DO UPDATE SET goal = EXCLUDED.goal
            """,
            (email, goal),
        )
        print("✅ user_profiles INSERT: user_id=", email, "goal=", goal)

        active_exp = conn.execute(
            """
            SELECT id, start_date, end_date, status, needs_email
            FROM experiments
            WHERE user_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
            """,
            (email,),
        ).fetchone()

        if active_exp:
            experiment_id = active_exp["id"]
            start_date = active_exp["start_date"]
            end_date = active_exp["end_date"]
            sub_status = "already_subscribed"
            conn.execute(
                "UPDATE experiments SET needs_email = true WHERE id = %s", (experiment_id,)
            )
        else:
            experiment_id = str(uuid.uuid4())
            start_date = date.today()
            end_date = start_date + timedelta(days=7)
            sub_status = "new_subscription"
            conn.execute(
                """
                INSERT INTO experiments (
                    id, user_id, start_date, end_date, status, challenge_name, created_at, needs_email
                ) VALUES (%s, %s, %s, %s, 'active', %s, NOW(), true)
                """,
                (experiment_id, email, start_date, end_date, goal),
            )

        template_exists = conn.execute(
            "SELECT id FROM experiment_templates WHERE LOWER(goal) = LOWER(%s)",
            (goal,),
        ).fetchone()

        if not template_exists:
            conn.execute(
                """
                INSERT INTO experiment_templates (goal, habit_1, habit_2, habit_3, approved, created_at)
                VALUES (LOWER(%s), 'Habit 1: Coming soon', 'Habit 2: Coming soon', 'Habit 3: Coming soon', false, NOW())
                """,
                (goal,),
            )

        template_approved = conn.execute(
            """
            SELECT id FROM experiment_templates
            WHERE LOWER(goal) = LOWER(%s) AND approved = true
            """,
            (goal,),
        ).fetchone()

        email_sent = False
        if template_approved:
            email_sent = send_first_email(
                user_email=email,
                goal=goal,
                experiment_id=str(experiment_id),
                start_date=start_date.isoformat(),
            )
            if email_sent:
                conn.execute(
                    "UPDATE experiments SET needs_email = false WHERE id = %s",
                    (experiment_id,),
                )

        conn.commit()

        return {
            "status": sub_status,
            "user_id": email,
            "experiment_id": experiment_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "template_created": template_exists is None,
            "email_sent": email_sent,
            "next_step": "Researcher: Edit experiment_templates row → set approved=true"
            if not template_approved
            else "Email sent!" if email_sent else "Email failed — check logs",
        }


# --- RECORD DAILY SCORES ---
@app.post("/scores")
async def record_scores(
    user_id: str = Body(..., embed=True),
    experiment_id: str = Body(..., embed=True),
    date_str: str = Body(..., embed=True),
    habit_1: int = Body(..., embed=True, ge=0, le=1),
    habit_2: int = Body(..., embed=True, ge=0, le=1),
    habit_3: int = Body(..., embed=True, ge=0, le=1),
):
    try:
        score_date = dateparser.parse(date_str).date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format (use YYYY-MM-DD)")

    with get_db_conn() as conn:
        exp = conn.execute(
            """
            SELECT id FROM experiments
            WHERE id = %s AND user_id = %s AND status = 'active'
            """,
            (experiment_id, user_id),
        ).fetchone()

        if not exp:
            raise HTTPException(status_code=404, detail="Experiment not found or access denied")

        conn.execute(
            """
            INSERT INTO experiment_scores (experiment_id, user_id, date, habit_1, habit_2, habit_3, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (experiment_id, date) DO UPDATE
              SET habit_1 = EXCLUDED.habit_1,
                  habit_2 = EXCLUDED.habit_2,
                  habit_3 = EXCLUDED.habit_3
            """,
            (experiment_id, user_id, score_date, habit_1, habit_2, habit_3),
        )

    return JSONResponse({"message": "Daily scores recorded successfully", "date": score_date.isoformat()})


# --- FETCH PROGRESS ---
@app.get("/progress/{user_id}/{experiment_id}")
async def get_progress(user_id: str = Path(...), experiment_id: str = Path(...)):
    with get_db_conn() as conn:
        scores = conn.execute(
            """
            SELECT date, habit_1, habit_2, habit_3
            FROM experiment_scores
            WHERE experiment_id = %s AND user_id = %s
            ORDER BY date
            """,
            (experiment_id, user_id),
        ).fetchall()

        if not scores:
            raise HTTPException(status_code=404, detail="No scores found")

        days_recorded = len(scores)
        h1_total = sum(r["habit_1"] for r in scores)
        h2_total = sum(r["habit_2"] for r in scores)
        h3_total = sum(r["habit_3"] for r in scores)

        return {
            "user_id": user_id,
            "experiment_id": experiment_id,
            "days_recorded": days_recorded,
            "habit_1_pct": round((h1_total / days_recorded) * 100, 1),
            "habit_2_pct": round((h2_total / days_recorded) * 100, 1),
            "habit_3_pct": round((h3_total / days_recorded) * 100, 1),
            "overall_pct": round(((h1_total + h2_total + h3_total) / (days_recorded * 3)) * 100, 1),
        }


# --- TRIGGER EMAIL (called by Supabase pg_net on approval) ---
@app.post("/trigger-email")
async def trigger_email_on_approved(
    goal: str = Body(..., embed=True),
    background_tasks: BackgroundTasks = None,
):
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO first_email_jobs (goal, created_at, status)
            VALUES (lower(%s), NOW(), 'pending')
            ON CONFLICT (goal) DO UPDATE SET status = 'pending', completed_at = NULL;
            """,
            (goal,),
        )
        conn.commit()

    return {
        "status": "queued",
        "goal": goal,
        "message": "Job queued — pg_cron will process within 1 minute",
    }


# --- PROCESS PENDING EMAILS (called by pg_cron every minute) ---
@app.post("/process-pending-emails")
async def process_pending_emails():
    print("🔄 /process-pending-emails called")
    result = process_first_email_jobs(goal=None)
    return {"status": "ok", **result}


# --- SEND DAILY CHECKINS (called by pg_cron every minute) ---
@app.post("/send-daily-checkins")
async def send_daily_checkins_endpoint():
    print("🔄 /send-daily-checkins called")
    send_daily_checkins()
    return {"status": "ok"}


# --- INBOUND EMAIL WEBHOOK (SendGrid parses replies) ---
@app.post("/inbound-email")
async def inbound_email(request: Request):
    form = await request.form()

    from_email = form.get("from", "")
    text_body = form.get("text", "") or ""

    print(f"📨 Inbound email from {from_email}")

    # Extract email address from "Name <email>" format
    if "<" in from_email and ">" in from_email:
        user_email = from_email.split("<")[1].split(">")[0].strip()
    else:
        user_email = from_email.strip()

    with get_db_conn() as conn:
        # Find active experiment for this user
        exp = conn.execute(
            """
            SELECT e.id, e.start_date, e.end_date, up.timezone, up.goal
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            WHERE e.user_id = %s AND e.status = 'active'
            ORDER BY e.created_at DESC LIMIT 1
            """,
            (user_email,),
        ).fetchone()

        if not exp:
            print(f"⚠️ No active experiment for {user_email}")
            return {"status": "ignored"}

        experiment_id = str(exp["id"])
        tz_name = exp["timezone"] or "UTC"

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        today_local = datetime.now(ZoneInfo("UTC")).astimezone(tz).date()

        # Parse lines from reply — skip quoted/empty lines
        lines = [
            l.strip() for l in text_body.splitlines()
            if l.strip() and not l.strip().startswith(">") and not l.strip().startswith("On ")
        ]

        # Find dates that need responses (today + any missed days)
        pending_dates = conn.execute(
            """
            SELECT dcj.checkin_date
            FROM daily_checkin_jobs dcj
            LEFT JOIN experiment_scores es
              ON es.experiment_id = %s AND es.date = dcj.checkin_date
            WHERE dcj.user_id = %s
              AND dcj.status = 'sent'
              AND es.id IS NULL
            ORDER BY dcj.checkin_date ASC
            """,
            (experiment_id, user_email),
        ).fetchall()

        if not pending_dates:
            print(f"ℹ️ No pending dates for {user_email}")
            return {"status": "no_pending_dates"}

        # Try to parse 3 Y/N values per pending date
        all_parsed = True
        line_index = 0
        results = []

        for row in pending_dates:
            checkin_date = row["checkin_date"]
            day_values = []

            for _ in range(3):
                if line_index >= len(lines):
                    all_parsed = False
                    break
                val = parse_yn(lines[line_index])
                if val is None:
                    all_parsed = False
                    break
                day_values.append(val)
                line_index += 1

            if len(day_values) == 3:
                results.append((checkin_date, day_values))
            else:
                all_parsed = False
                break

        if results:
            for checkin_date, vals in results:
                conn.execute(
                    """
                    INSERT INTO experiment_scores (experiment_id, user_id, date, habit_1, habit_2, habit_3, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (experiment_id, date) DO UPDATE
                      SET habit_1 = EXCLUDED.habit_1,
                          habit_2 = EXCLUDED.habit_2,
                          habit_3 = EXCLUDED.habit_3
                    """,
                    (experiment_id, user_email, checkin_date, vals[0], vals[1], vals[2]),
                )
                print(f"✅ Scores recorded for {user_email} on {checkin_date}: {vals}")
            conn.commit()

        if not all_parsed and not results:
            # Log unrecognized response — next email will include the note
            conn.execute(
                """
                INSERT INTO daily_checkin_jobs (checkin_date, user_id, experiment_id, status, error_msg)
                VALUES (%s, %s, %s, 'sent', 'unrecognized_response')
                ON CONFLICT (checkin_date, user_id) DO UPDATE SET error_msg = 'unrecognized_response'
                """,
                (today_local, user_email, experiment_id),
            )
            conn.commit()
            print(f"⚠️ Unrecognized response from {user_email}: {lines[:5]}")

    return {"status": "ok"}


# --- CHECKIN PAGE (fallback for non-Gmail users) ---
@app.get("/checkin/{token}", response_class=HTMLResponse)
async def checkin_page(token: str):
    with get_db_conn() as conn:
        row = conn.execute(
            """
            SELECT ct.user_id, ct.experiment_id, ct.checkin_date,
                   et.habit_1, et.habit_2, et.habit_3, up.goal
            FROM checkin_tokens ct
            JOIN user_profiles up ON up.user_id = ct.user_id
            JOIN experiment_templates et ON LOWER(et.goal) = LOWER(up.goal) AND et.approved = true
            WHERE ct.token = %s AND ct.expires_at > NOW()
            """,
            (token,),
        ).fetchone()

        if not row:
            return HTMLResponse("<h2>This link has expired or is invalid.</h2>", status_code=404)

        habits = [row["habit_1"], row["habit_2"], row["habit_3"]]
        habit_rows = ""
        for i, habit in enumerate(habits, 1):
            habit_rows += f"""
            <tr>
              <td style="padding:12px; border-bottom:1px solid #eee;">{habit}</td>
              <td style="padding:12px; border-bottom:1px solid #eee; text-align:center;">
                <a href="/checkin/{token}/submit?habit={i}&val=1" style="background:#27ae60; color:white; padding:6px 16px; border-radius:4px; text-decoration:none; margin-right:8px;">Y</a>
                <a href="/checkin/{token}/submit?habit={i}&val=0" style="background:#e74c3c; color:white; padding:6px 16px; border-radius:4px; text-decoration:none;">N</a>
              </td>
            </tr>
            """

        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif; max-width:500px; margin:40px auto; padding:0 20px;">
          <h2>Habit Check-in — {row['checkin_date']}</h2>
          <table style="width:100%; border-collapse:collapse;">
            <thead><tr style="background:#2c3e50; color:white;">
              <th style="padding:10px; text-align:left;">Habit</th>
              <th style="padding:10px;">Did you do it?</th>
            </tr></thead>
            <tbody>{habit_rows}</tbody>
          </table>
        </body></html>
        """)


@app.get("/checkin/{token}/submit", response_class=HTMLResponse)
async def checkin_submit(token: str, habit: int, val: int):
    if habit not in (1, 2, 3) or val not in (0, 1):
        raise HTTPException(status_code=400, detail="Invalid parameters")

    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT user_id, experiment_id, checkin_date FROM checkin_tokens WHERE token = %s AND expires_at > NOW()",
            (token,),
        ).fetchone()

        if not row:
            return HTMLResponse("<h2>Link expired.</h2>", status_code=404)

        field = f"habit_{habit}"
        conn.execute(
            f"""
            INSERT INTO experiment_scores (experiment_id, user_id, date, habit_1, habit_2, habit_3, created_at)
            VALUES (%s, %s, %s, 0, 0, 0, NOW())
            ON CONFLICT (experiment_id, date) DO UPDATE SET {field} = %s
            """,
            (str(row["experiment_id"]), row["user_id"], row["checkin_date"], val),
        )
        conn.commit()
        print(f"✅ Checkin submit: {row['user_id']} habit_{habit}={val} for {row['checkin_date']}")

    return HTMLResponse("""
    <html><body style="font-family:sans-serif; text-align:center; margin-top:80px;">
      <h2>✅ Got it!</h2>
      <p>Your response has been recorded.</p>
      <script>setTimeout(() => window.close(), 1500);</script>
    </body></html>
    """)
