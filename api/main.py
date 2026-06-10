from dotenv import load_dotenv
import os

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

import uuid
import secrets
import hashlib
import hmac
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Body, Path, Request, Form, Cookie
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
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


# ============================================================
# UTILITIES
# ============================================================

def get_db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(password), hashed)


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
    cleaned = text.strip().lower().rstrip(".")
    if cleaned in YES_PHRASES:
        return 1
    if cleaned in NO_PHRASES:
        return 0
    return None


def get_analyst_from_session(conn, session_token: str):
    if not session_token:
        return None
    return conn.execute(
        """
        SELECT a.id, a.email, a.name
        FROM analyst_sessions s
        JOIN analysts a ON a.id = s.analyst_id
        WHERE s.token = %s AND s.expires_at > NOW()
        """,
        (session_token,),
    ).fetchone()


# ============================================================
# EMAIL BUILDERS
# ============================================================

def send_welcome_email(user_email: str, goal: str, experiment_id: str,
                        start_date: str, cycle_number: int, habits: list,
                        research_notes: list) -> bool:
    if not resend.api_key:
        print("❌ RESEND_API_KEY is missing")
        return False
    try:
        habit_rows = ""
        for i, (habit, note) in enumerate(zip(habits, research_notes), 1):
            research = f"<p style='color:#666; font-size:13px; margin:4px 0 12px 0;'>💡 {note}</p>" if note else ""
            habit_rows += f"""
            <tr>
              <td style="padding:12px; border-bottom:1px solid #eee;">
                <strong>Habit {i}:</strong> {habit}
                {research}
              </td>
            </tr>"""

        cycle_label = f"Week {cycle_number}" if cycle_number == 1 else f"Week {cycle_number} — New Cycle"
        subject = f"Your {cycle_label} Habit Experiment: {goal}"

        email_html = f"""
        <div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:600px;">
          <h2>Your {cycle_label} Habit Experiment</h2>
          <p><strong>Goal:</strong> {goal}</p>
          <p>Here are your 3 daily habits for the next 7 days, and the research behind each one:</p>
          <table style="width:100%; border-collapse:collapse; margin:20px 0;">
            {habit_rows}
          </table>
          <p>Every evening at 6 PM you'll receive a check-in email. Simply reply with <strong>Y</strong> or <strong>N</strong> for each habit.</p>
          <p><a href="{BASE_URL}/progress/{user_email}/{experiment_id}">View your progress</a></p>
        </div>"""

        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": user_email,
            "reply_to": REPLY_TO,
            "subject": subject,
            "html": email_html,
        })
        print(f"✅ Welcome email sent to {user_email} cycle={cycle_number}")
        return True
    except Exception as e:
        print(f"💥 send_welcome_email: {e}")
        return False


def send_checkin_email(user_email: str, experiment_id: str, habits: list,
                        dates_to_include: list, day_num: int) -> bool:
    if not resend.api_key:
        return False
    try:
        rows = ""
        for checkin_date, is_missed, bad_response in dates_to_include:
            date_label = checkin_date.strftime("%A %b %d")
            note = ""
            if is_missed and bad_response:
                note = '<br><small style="color:#e67e22;">⚠️ We couldn\'t understand your previous response — please reply Y or N</small>'
            elif is_missed:
                note = '<br><small style="color:#999;">Missing from yesterday</small>'

            rows += f"""
            <tr>
              <td colspan="2" style="padding:8px 12px; background:#f0f0f0; font-weight:bold; font-size:13px;">
                {date_label}{note}
              </td>
            </tr>"""
            for i, habit in enumerate(habits, 1):
                rows += f"""
                <tr>
                  <td style="padding:10px 12px; border-bottom:1px solid #eee;">Habit {i}: {habit}</td>
                  <td style="padding:10px 12px; border-bottom:1px solid #eee; text-align:center; color:#999; font-size:13px;">Y / N</td>
                </tr>"""

        habit_list = "\n".join([f"{i+1}. {h}" for i, h in enumerate(habits)])
        missed_count = len([d for d in dates_to_include if d[1]])
        subject = f"Day {day_num}/7 Habit Check-in"
        if missed_count:
            subject += f" (+ {missed_count} missed day{'s' if missed_count > 1 else ''})"

        email_html = f"""
        <div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:600px;">
          <h2>Daily Habit Check-in</h2>
          <p>Did you complete your habits? <strong>Reply with Y or N for each habit, one per line.</strong></p>
          <table style="width:100%; border-collapse:collapse; margin:20px 0;">
            <thead>
              <tr style="background:#2c3e50; color:white;">
                <th style="padding:10px 12px; text-align:left;">Habit</th>
                <th style="padding:10px 12px;">Response</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
          <div style="background:#f8f9fa; padding:16px; border-radius:6px; margin:20px 0;">
            <p style="margin:0 0 8px 0;"><strong>How to reply:</strong></p>
            <p style="margin:0; font-family:monospace; white-space:pre;">{habit_list}

Reply with one Y or N per habit:
Y
N
Y</p>
          </div>
          <p style="color:#999; font-size:12px;">
            <a href="{BASE_URL}/progress/{user_email}/{experiment_id}">View your full progress</a>
          </p>
        </div>"""

        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": user_email,
            "reply_to": REPLY_TO,
            "subject": subject,
            "html": email_html,
        })
        return True
    except Exception as e:
        print(f"💥 send_checkin_email to {user_email}: {e}")
        return False


def send_survey_email(user_email: str, experiment_id: str, cycle_number: int,
                       goal: str, survey_token: str) -> bool:
    if not resend.api_key:
        return False
    try:
        survey_url = f"{BASE_URL}/survey/{survey_token}"
        email_html = f"""
        <div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:600px;">
          <h2>🎉 Week {cycle_number} Complete!</h2>
          <p>Congratulations on completing your 7-day habit experiment for: <strong>{goal}</strong></p>
          <p>Please take 2 minutes to complete your weekly review. Your feedback helps your analyst
             design the perfect habits for next week.</p>
          <p style="text-align:center; margin:30px 0;">
            <a href="{survey_url}"
               style="background:#2c3e50; color:white; padding:14px 28px; border-radius:6px;
                      text-decoration:none; font-size:16px;">
              Complete Week {cycle_number} Review →
            </a>
          </p>
          <p style="color:#999; font-size:12px;">
            This link expires in 7 days.<br>
            <a href="{BASE_URL}/progress/{user_email}/{experiment_id}">View your week's progress</a>
          </p>
        </div>"""

        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": user_email,
            "reply_to": REPLY_TO,
            "subject": f"Week {cycle_number} Complete — Your Habit Review",
            "html": email_html,
        })
        print(f"✅ Survey email sent to {user_email} cycle={cycle_number}")
        return True
    except Exception as e:
        print(f"💥 send_survey_email: {e}")
        return False


# ============================================================
# BACKGROUND JOBS
# ============================================================

def process_first_email_jobs():
    with get_db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, user_id, goal, cycle_number FROM first_email_jobs
            WHERE status = 'pending'
            FOR UPDATE SKIP LOCKED;
            """
        )
        jobs = cur.fetchall()
        if not jobs:
            return {"processed": 0}

        processed = 0
        for job in jobs:
            job_id = job["id"]
            user_id = job["user_id"]
            job_goal = job["goal"]
            cycle_number = job["cycle_number"]

            try:
                # Get experiment
                exp = conn.execute(
                    """
                    SELECT id, start_date FROM experiments
                    WHERE user_id = %s AND status = 'waiting'
                      AND cycle_number = %s
                    """,
                    (user_id, cycle_number),
                ).fetchone()

                if not exp:
                    print(f"⚠️ No waiting experiment for {user_id} cycle={cycle_number}")
                    cur.execute(
                        "UPDATE first_email_jobs SET status = 'failed', error_msg = 'no waiting experiment' WHERE id = %s",
                        (job_id,),
                    )
                    conn.commit()
                    continue

                experiment_id = str(exp["id"])

                # Get template
                template = conn.execute(
                    """
                    SELECT habit_1, habit_2, habit_3,
                           habit_1_research, habit_2_research, habit_3_research
                    FROM experiment_templates
                    WHERE user_id = %s AND cycle_number = %s AND approved = true
                    """,
                    (user_id, cycle_number),
                ).fetchone()

                if not template:
                    print(f"⚠️ No approved template for {user_id} cycle={cycle_number}")
                    cur.execute(
                        "UPDATE first_email_jobs SET status = 'failed', error_msg = 'no approved template' WHERE id = %s",
                        (job_id,),
                    )
                    conn.commit()
                    continue

                habits = [template["habit_1"], template["habit_2"], template["habit_3"]]
                research = [
                    template["habit_1_research"] or "",
                    template["habit_2_research"] or "",
                    template["habit_3_research"] or "",
                ]

                today = date.today()
                success = send_welcome_email(
                    user_email=user_id,
                    goal=job_goal,
                    experiment_id=experiment_id,
                    start_date=today.isoformat(),
                    cycle_number=cycle_number,
                    habits=habits,
                    research_notes=research,
                )

                if success:
                    # Activate experiment
                    conn.execute(
                        """
                        UPDATE experiments
                        SET status = 'active',
                            start_date = %s,
                            end_date = %s,
                            needs_email = false
                        WHERE id = %s
                        """,
                        (today, today + timedelta(days=6), experiment_id),
                    )
                    cur.execute(
                        "UPDATE first_email_jobs SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (job_id,),
                    )
                    conn.commit()
                    print(f"✅ Welcome email sent, experiment activated for {user_id} cycle={cycle_number}")
                    processed += 1
                else:
                    cur.execute(
                        "UPDATE first_email_jobs SET status = 'failed', error_msg = 'email send failed' WHERE id = %s",
                        (job_id,),
                    )
                    conn.commit()

            except Exception as e:
                print(f"❌ Error processing job {job_id}: {e}")
                conn.rollback()
                cur.execute(
                    "UPDATE first_email_jobs SET status = 'failed', error_msg = %s WHERE id = %s",
                    (str(e), job_id),
                )
                conn.commit()

        return {"processed": processed}


def send_daily_checkins():
    now_utc = datetime.now(ZoneInfo("UTC"))

    with get_db_conn() as conn:
        experiments = conn.execute(
            """
            SELECT e.id, e.user_id, e.start_date, e.end_date, e.cycle_number,
                   up.goal, up.timezone
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            WHERE e.status = 'active'
            """
        ).fetchall()

        for exp in experiments:
            user_id = exp["user_id"]
            experiment_id = exp["id"]
            goal = exp["goal"]
            cycle_number = exp["cycle_number"]
            tz_name = exp["timezone"] or "UTC"
            start_date = exp["start_date"]
            end_date = exp["end_date"]

            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("UTC")

            now_local = now_utc.astimezone(tz)
            today_local = now_local.date()

            if not (now_local.hour == 18 and now_local.minute == 0):
                continue

            if not start_date or not end_date:
                continue

            if not (start_date <= today_local <= end_date):
                continue

            already_sent = conn.execute(
                """
                SELECT id FROM daily_checkin_jobs
                WHERE user_id = %s AND checkin_date = %s AND status = 'sent'
                """,
                (user_id, today_local),
            ).fetchone()

            if already_sent:
                continue

            # Day 7 — send survey instead of checkin
            day_num = (today_local - start_date).days + 1
            if day_num == 7:
                _send_end_of_week_survey(conn, user_id, experiment_id, cycle_number, goal, today_local)
                continue

            # Get template
            template = conn.execute(
                """
                SELECT habit_1, habit_2, habit_3
                FROM experiment_templates
                WHERE user_id = %s AND cycle_number = %s AND approved = true
                """,
                (user_id, cycle_number),
            ).fetchone()

            if not template:
                continue

            habits = [template["habit_1"], template["habit_2"], template["habit_3"]]

            # Find missed dates
            dates_to_include = []
            for days_back in range(1, 3):
                check_date = today_local - timedelta(days=days_back)
                if check_date < start_date:
                    break
                score = conn.execute(
                    "SELECT id FROM experiment_scores WHERE experiment_id = %s AND date = %s",
                    (str(experiment_id), check_date),
                ).fetchone()
                if score:
                    break
                bad = conn.execute(
                    "SELECT error_msg FROM daily_checkin_jobs WHERE user_id = %s AND checkin_date = %s AND status = 'sent'",
                    (user_id, check_date),
                ).fetchone()
                dates_to_include.insert(0, (check_date, True, bad and bad["error_msg"] == "unrecognized_response"))

            dates_to_include.append((today_local, False, False))

            success = send_checkin_email(
                user_email=user_id,
                experiment_id=str(experiment_id),
                habits=habits,
                dates_to_include=dates_to_include,
                day_num=day_num,
            )

            if success:
                conn.execute(
                    """
                    INSERT INTO daily_checkin_jobs (checkin_date, user_id, experiment_id, status, sent_at)
                    VALUES (%s, %s, %s, 'sent', NOW())
                    ON CONFLICT (checkin_date, user_id) DO UPDATE SET status = 'sent', sent_at = NOW()
                    """,
                    (today_local, user_id, str(experiment_id)),
                )
                conn.commit()
                print(f"✅ Checkin sent to {user_id} day={day_num}")


def _send_end_of_week_survey(conn, user_id, experiment_id, cycle_number, goal, today_local):
    survey_token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO weekly_surveys (user_id, experiment_id, cycle_number, token)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (user_id, str(experiment_id), cycle_number, survey_token),
    )

    success = send_survey_email(
        user_email=user_id,
        experiment_id=str(experiment_id),
        cycle_number=cycle_number,
        goal=goal,
        survey_token=survey_token,
    )

    if success:
        conn.execute(
            """
            UPDATE experiments SET survey_sent_at = NOW()
            WHERE id = %s
            """,
            (str(experiment_id),),
        )
        conn.execute(
            """
            INSERT INTO daily_checkin_jobs (checkin_date, user_id, experiment_id, status, sent_at)
            VALUES (%s, %s, %s, 'sent', NOW())
            ON CONFLICT (checkin_date, user_id) DO UPDATE SET status = 'sent', sent_at = NOW()
            """,
            (today_local, user_id, str(experiment_id)),
        )
        conn.commit()
        print(f"✅ Survey email sent to {user_id} cycle={cycle_number}")


def complete_experiment_and_create_next(conn, user_id: str, experiment_id: str,
                                         cycle_number: int, goal: str, new_goal: str = None):
    """Complete current experiment, save history, create next waiting experiment."""
    # Calculate stats
    scores = conn.execute(
        """
        SELECT habit_1, habit_2, habit_3 FROM experiment_scores
        WHERE experiment_id = %s
        """,
        (experiment_id,),
    ).fetchall()

    days = len(scores)
    if days > 0:
        h1 = round(sum(r["habit_1"] for r in scores) / days * 100, 2)
        h2 = round(sum(r["habit_2"] for r in scores) / days * 100, 2)
        h3 = round(sum(r["habit_3"] for r in scores) / days * 100, 2)
        overall = round((h1 + h2 + h3) / 3, 2)
    else:
        h1 = h2 = h3 = overall = 0

    # Get survey response
    survey = conn.execute(
        "SELECT * FROM weekly_surveys WHERE experiment_id = %s LIMIT 1",
        (experiment_id,),
    ).fetchone()

    exp = conn.execute(
        "SELECT start_date, end_date FROM experiments WHERE id = %s",
        (experiment_id,),
    ).fetchone()

    # Save goal history
    conn.execute(
        """
        INSERT INTO user_goal_history (
            user_id, goal, cycle_number, experiment_id,
            start_date, end_date,
            habit_1_pct, habit_2_pct, habit_3_pct, overall_pct,
            q1_progress, q2_felt_change, q6_next_strategy
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            user_id, goal, cycle_number, experiment_id,
            exp["start_date"] if exp else None,
            exp["end_date"] if exp else None,
            h1, h2, h3, overall,
            survey["q1_progress"] if survey else None,
            survey["q2_felt_change"] if survey else None,
            survey["q6_next_strategy"] if survey else None,
        ),
    )

    # Update goal if changed
    active_goal = goal
    if new_goal and new_goal.strip() and new_goal.strip().lower() != goal.lower():
        active_goal = new_goal.strip()
        conn.execute(
            "UPDATE user_profiles SET goal = %s WHERE user_id = %s",
            (active_goal, user_id),
        )
        print(f"✅ Goal updated for {user_id}: {goal} → {active_goal}")

    # Complete current experiment
    conn.execute(
        "UPDATE experiments SET status = 'completed' WHERE id = %s",
        (experiment_id,),
    )

    # Create next waiting experiment
    next_cycle = cycle_number + 1
    next_exp_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO experiments (
            id, user_id, start_date, end_date, status,
            challenge_name, created_at, needs_email, cycle_number
        ) VALUES (%s, %s, NULL, NULL, 'waiting', %s, NOW(), true, %s)
        """,
        (next_exp_id, user_id, active_goal, next_cycle),
    )

    # Create blank template for analyst
    conn.execute(
        """
        INSERT INTO experiment_templates (
            user_id, goal, cycle_number,
            habit_1, habit_2, habit_3, approved, created_at
        ) VALUES (%s, %s, %s, 'TBD', 'TBD', 'TBD', false, NOW())
        """,
        (user_id, active_goal, next_cycle),
    )

    conn.commit()
    print(f"✅ Experiment {experiment_id} completed, cycle {next_cycle} created for {user_id}")


# ============================================================
# SURVEY PAGE
# ============================================================

@app.get("/survey/{token}", response_class=HTMLResponse)
async def survey_page(token: str):
    with get_db_conn() as conn:
        survey = conn.execute(
            """
            SELECT ws.*, up.goal, et.habit_1, et.habit_2, et.habit_3
            FROM weekly_surveys ws
            JOIN user_profiles up ON up.user_id = ws.user_id
            JOIN experiment_templates et
              ON et.user_id = ws.user_id AND et.cycle_number = ws.cycle_number
            WHERE ws.token = %s AND ws.expires_at > NOW() AND ws.status = 'pending'
            """,
            (token,),
        ).fetchone()

        if not survey:
            return HTMLResponse("""
            <html><body style="font-family:sans-serif; text-align:center; margin-top:80px;">
              <h2>This survey link has expired or has already been completed.</h2>
            </body></html>""", status_code=404)

        goal = survey["goal"]
        habits = [survey["habit_1"], survey["habit_2"], survey["habit_3"]]
        cycle = survey["cycle_number"]

        habit_options = "".join([
            f'<label style="display:block; margin:8px 0;"><input type="radio" name="q4" value="habit_{i}" required> {h}</label>'
            for i, h in enumerate(habits, 1)
        ])

        return HTMLResponse(f"""
        <html>
        <head>
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Week {cycle} Review</title>
          <style>
            body {{ font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:600px; margin:40px auto; padding:0 20px; color:#2c3e50; }}
            h1 {{ font-size:24px; }}
            h3 {{ font-size:16px; margin-top:28px; }}
            .section {{ background:#f8f9fa; border-radius:8px; padding:20px; margin:20px 0; }}
            .radio-group label {{ display:inline-block; margin:6px 8px 6px 0; }}
            .radio-group input[type=radio] {{ display:none; }}
            .radio-group input[type=radio] + span {{
              display:inline-block; padding:8px 16px; border:2px solid #ddd;
              border-radius:20px; cursor:pointer; font-size:14px;
            }}
            .radio-group input[type=radio]:checked + span {{
              background:#2c3e50; color:white; border-color:#2c3e50;
            }}
            textarea {{ width:100%; box-sizing:border-box; border:1px solid #ddd; border-radius:6px; padding:10px; font-size:14px; }}
            button {{ background:#2c3e50; color:white; border:none; padding:14px 28px; border-radius:6px; font-size:16px; cursor:pointer; width:100%; margin-top:20px; }}
            button:hover {{ background:#34495e; }}
          </style>
        </head>
        <body>
          <h1>Week {cycle} Review</h1>
          <p>Goal: <strong>{goal}</strong></p>

          <form method="POST" action="/survey/{token}">

            <div class="section">
              <h3>1. On a scale of 1–5, how much closer do you feel to your goal compared to a week ago?</h3>
              <div class="radio-group">
                {"".join([f'<label><input type="radio" name="q1" value="{i}" required><span>{i}</span></label>' for i in range(1,6)])}
              </div>
              <p style="font-size:12px; color:#999; margin:4px 0 0 0;">1 = No closer &nbsp;·&nbsp; 5 = Significantly closer</p>
            </div>

            <div class="section">
              <h3>2. What is the most noticeable difference you felt this week?</h3>
              <div class="radio-group">
                <label><input type="radio" name="q2" value="more_energy_focus" required><span>More energy / focus</span></label>
                <label><input type="radio" name="q2" value="better_structure_routine"><span>Better structure / routine</span></label>
                <label><input type="radio" name="q2" value="less_anxiety_stress"><span>Less anxiety / stress</span></label>
                <label><input type="radio" name="q2" value="no_noticeable_change"><span>No noticeable change yet</span></label>
              </div>
            </div>

            <div class="section">
              <h3>3. Can you pinpoint a specific moment this week where you noticed that feeling? <span style="color:#999; font-size:13px;">(optional)</span></h3>
              <textarea name="q3" rows="3" placeholder="e.g., On Wednesday morning I didn't feel the usual urge to snooze..."></textarea>
            </div>

            <div class="section">
              <h3>4. Which habit felt the heaviest or most difficult?</h3>
              {habit_options}
              <label style="display:block; margin:8px 0;"><input type="radio" name="q4" value="none"> None, all felt manageable</label>
            </div>

            <div class="section">
              <h3>5. What do you think caused that friction? <span style="color:#999; font-size:13px;">(optional)</span></h3>
              <textarea name="q5" rows="3" placeholder="e.g., Too tired after work, forgot about it, bad timing..."></textarea>
            </div>

            <div class="section">
              <h3>6. How do you want your analyst to approach next week's habits?</h3>
              <div class="radio-group">
                <label><input type="radio" name="q6" value="deepen" required><span>Deepen them</span></label>
                <label><input type="radio" name="q6" value="swap"><span>Swap them</span></label>
                <label><input type="radio" name="q6" value="fresh_start"><span>Fresh start</span></label>
              </div>
              <p style="font-size:12px; color:#999; margin:8px 0 0 0;">
                Deepen = keep working habits, increase intensity<br>
                Swap = replace habits that caused friction<br>
                Fresh start = completely new angles
              </p>
            </div>

            <div class="section">
              <h3>Want to update your goal for next week? <span style="color:#999; font-size:13px;">(optional)</span></h3>
              <p style="font-size:13px; color:#666;">Current goal: <strong>{goal}</strong></p>
              <input type="text" name="new_goal" placeholder="Leave blank to keep current goal"
                     style="width:100%; box-sizing:border-box; border:1px solid #ddd; border-radius:6px; padding:10px; font-size:14px;">
            </div>

            <button type="submit">Submit Week {cycle} Review</button>
          </form>
        </body>
        </html>
        """)


@app.post("/survey/{token}", response_class=HTMLResponse)
async def survey_submit(
    token: str,
    q1: int = Form(...),
    q2: str = Form(...),
    q3: str = Form(""),
    q4: str = Form(...),
    q5: str = Form(""),
    q6: str = Form(...),
    new_goal: str = Form(""),
):
    with get_db_conn() as conn:
        survey = conn.execute(
            "SELECT * FROM weekly_surveys WHERE token = %s AND expires_at > NOW() AND status = 'pending'",
            (token,),
        ).fetchone()

        if not survey:
            return HTMLResponse("<h2>Survey expired or already submitted.</h2>", status_code=404)

        # Save responses
        conn.execute(
            """
            UPDATE weekly_surveys SET
                q1_progress = %s, q2_felt_change = %s, q3_specific_moment = %s,
                q4_hardest_habit = %s, q5_friction_cause = %s, q6_next_strategy = %s,
                new_goal = %s, status = 'completed', completed_at = NOW()
            WHERE token = %s
            """,
            (q1, q2, q3 or None, q4, q5 or None, q6, new_goal or None, token),
        )

        # Complete experiment and create next cycle
        complete_experiment_and_create_next(
            conn=conn,
            user_id=survey["user_id"],
            experiment_id=str(survey["experiment_id"]),
            cycle_number=survey["cycle_number"],
            goal=conn.execute(
                "SELECT goal FROM user_profiles WHERE user_id = %s",
                (survey["user_id"],)
            ).fetchone()["goal"],
            new_goal=new_goal or None,
        )

        next_cycle = survey["cycle_number"] + 1

        return HTMLResponse(f"""
        <html>
        <head>
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Thank You!</title>
          <style>
            body {{ font-family:-apple-system,BlinkMacSystemFont,sans-serif;
                   text-align:center; margin-top:80px; color:#2c3e50; }}
          </style>
        </head>
        <body>
          <h2>Thank you! 🎉</h2>
          <p>Your feedback has been sent to your analyst.</p>
          <p>Keep an eye on your inbox for your customized <strong>Week {next_cycle}</strong> habits.</p>
        </body>
        </html>
        """)


# ============================================================
# ANALYST DASHBOARD
# ============================================================

@app.get("/analyst", response_class=HTMLResponse)
async def analyst_login_page():
    return HTMLResponse("""
    <html>
    <head>
      <title>Analyst Login</title>
      <style>
        body {{ font-family:sans-serif; max-width:400px; margin:80px auto; padding:0 20px; }}
        input {{ width:100%; box-sizing:border-box; padding:10px; margin:8px 0; border:1px solid #ddd; border-radius:6px; font-size:14px; }}
        button {{ width:100%; padding:12px; background:#2c3e50; color:white; border:none; border-radius:6px; font-size:16px; cursor:pointer; margin-top:8px; }}
        .error {{ color:red; font-size:13px; }}
      </style>
    </head>
    <body>
      <h2>Analyst Login</h2>
      <form method="POST" action="/analyst/login">
        <input type="email" name="email" placeholder="Email" required>
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Login</button>
      </form>
    </body>
    </html>
    """)


@app.post("/analyst/login")
async def analyst_login(email: str = Form(...), password: str = Form(...)):
    with get_db_conn() as conn:
        analyst = conn.execute(
            "SELECT * FROM analysts WHERE email = %s",
            (email,),
        ).fetchone()

        if not analyst or not verify_password(password, analyst["password_hash"]):
            return HTMLResponse("""
            <html><body style="font-family:sans-serif; text-align:center; margin-top:80px;">
              <h2>Invalid email or password.</h2>
              <a href="/analyst">Try again</a>
            </body></html>""", status_code=401)

        session_token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO analyst_sessions (analyst_id, token) VALUES (%s, %s)",
            (analyst["id"], session_token),
        )
        conn.execute(
            "UPDATE analysts SET last_login_at = NOW() WHERE id = %s",
            (analyst["id"],),
        )
        conn.commit()

    response = RedirectResponse(url="/analyst/dashboard", status_code=302)
    response.set_cookie("analyst_session", session_token, httponly=True, max_age=604800)
    return response


@app.get("/analyst/dashboard", response_class=HTMLResponse)
async def analyst_dashboard(analyst_session: str = Cookie(default=None)):
    with get_db_conn() as conn:
        analyst = get_analyst_from_session(conn, analyst_session)
        if not analyst:
            return RedirectResponse(url="/analyst", status_code=302)

        # Users needing template (waiting with no approved template)
        needs_template = conn.execute(
            """
            SELECT e.user_id, e.cycle_number, up.goal, e.created_at
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            LEFT JOIN experiment_templates et
              ON et.user_id = e.user_id AND et.cycle_number = e.cycle_number AND et.approved = true
            WHERE e.status = 'waiting' AND et.id IS NULL
            ORDER BY e.created_at ASC
            """
        ).fetchall()

        # Active users
        active_users = conn.execute(
            """
            SELECT e.user_id, e.cycle_number, e.start_date, e.end_date,
                   up.goal,
                   COUNT(es.id) as days_recorded
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            LEFT JOIN experiment_scores es ON es.experiment_id = e.id
            WHERE e.status = 'active'
            GROUP BY e.user_id, e.cycle_number, e.start_date, e.end_date, up.goal
            ORDER BY e.start_date DESC
            """
        ).fetchall()

        # Ready for review (survey completed)
        ready_review = conn.execute(
            """
            SELECT e.user_id, e.cycle_number, up.goal,
                   ws.q1_progress, ws.q2_felt_change, ws.q6_next_strategy,
                   ws.completed_at
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            JOIN weekly_surveys ws ON ws.experiment_id = e.id AND ws.status = 'completed'
            LEFT JOIN experiment_templates et
              ON et.user_id = e.user_id AND et.cycle_number = (e.cycle_number + 1)
            WHERE e.status = 'completed' AND et.id IS NULL
            ORDER BY ws.completed_at DESC
            """
        ).fetchall()

        def user_rows(users, cols):
            if not users:
                return f'<tr><td colspan="{len(cols)}" style="padding:12px; color:#999; text-align:center;">None</td></tr>'
            rows = ""
            for u in users:
                rows += "<tr>" + "".join([f'<td style="padding:10px 12px; border-bottom:1px solid #eee;">{u.get(c, "") or ""}</td>' for c in cols]) + "</tr>"
            return rows

        needs_rows = ""
        for u in needs_template:
            needs_rows += f"""
            <tr>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{u['user_id']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{u['goal']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">Week {u['cycle_number']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">
                <a href="/analyst/template/{u['user_id']}/{u['cycle_number']}"
                   style="background:#2c3e50; color:white; padding:6px 14px; border-radius:4px; text-decoration:none; font-size:13px;">
                  Create Template
                </a>
              </td>
            </tr>"""

        active_rows = ""
        for u in active_users:
            day_num = (date.today() - u['start_date']).days + 1 if u['start_date'] else "?"
            active_rows += f"""
            <tr>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{u['user_id']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{u['goal']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">Week {u['cycle_number']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">Day {day_num}/7</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{u['days_recorded']} days recorded</td>
            </tr>"""

        review_rows = ""
        for u in ready_review:
            strategy_map = {"deepen": "Deepen", "swap": "Swap", "fresh_start": "Fresh Start"}
            review_rows += f"""
            <tr>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{u['user_id']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{u['goal']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">Week {u['cycle_number']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{u['q1_progress']}/5</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{strategy_map.get(u['q6_next_strategy'] or '', '?')}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">
                <a href="/analyst/user/{u['user_id']}"
                   style="background:#27ae60; color:white; padding:6px 14px; border-radius:4px; text-decoration:none; font-size:13px;">
                  Review
                </a>
              </td>
            </tr>"""

        return HTMLResponse(f"""
        <html>
        <head>
          <title>Analyst Dashboard</title>
          <style>
            body {{ font-family:-apple-system,BlinkMacSystemFont,sans-serif; max-width:1000px; margin:0 auto; padding:20px; color:#2c3e50; }}
            h1 {{ font-size:24px; }}
            h2 {{ font-size:18px; margin-top:32px; }}
            table {{ width:100%; border-collapse:collapse; margin:12px 0; }}
            thead tr {{ background:#2c3e50; color:white; }}
            th {{ padding:10px 12px; text-align:left; font-weight:500; }}
            .badge {{ display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:bold; }}
            .needs {{ background:#ffeaa7; color:#d35400; }}
            .active {{ background:#d5f5e3; color:#1e8449; }}
            .review {{ background:#fadbd8; color:#c0392b; }}
            nav {{ display:flex; justify-content:space-between; align-items:center; padding:12px 0; border-bottom:1px solid #eee; margin-bottom:20px; }}
          </style>
        </head>
        <body>
          <nav>
            <h1>Analyst Dashboard</h1>
            <span>👋 {analyst['name'] or analyst['email']} &nbsp;·&nbsp; <a href="/analyst/logout">Logout</a></span>
          </nav>

          <h2><span class="badge needs">Needs Template</span> ({len(needs_template)})</h2>
          <table>
            <thead><tr><th>User</th><th>Goal</th><th>Cycle</th><th>Action</th></tr></thead>
            <tbody>{needs_rows}</tbody>
          </table>

          <h2><span class="badge review">Ready for Review</span> ({len(ready_review)})</h2>
          <table>
            <thead><tr><th>User</th><th>Goal</th><th>Cycle</th><th>Progress</th><th>Strategy</th><th>Action</th></tr></thead>
            <tbody>{review_rows}</tbody>
          </table>

          <h2><span class="badge active">Active</span> ({len(active_users)})</h2>
          <table>
            <thead><tr><th>User</th><th>Goal</th><th>Cycle</th><th>Day</th><th>Scores</th></tr></thead>
            <tbody>{active_rows}</tbody>
          </table>
        </body>
        </html>
        """)


@app.get("/analyst/template/{user_id}/{cycle_number}", response_class=HTMLResponse)
async def analyst_template_page(user_id: str, cycle_number: int,
                                  analyst_session: str = Cookie(default=None)):
    with get_db_conn() as conn:
        analyst = get_analyst_from_session(conn, analyst_session)
        if not analyst:
            return RedirectResponse(url="/analyst", status_code=302)

        user = conn.execute(
            "SELECT goal FROM user_profiles WHERE user_id = %s",
            (user_id,),
        ).fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Get existing template if any
        tmpl = conn.execute(
            "SELECT * FROM experiment_templates WHERE user_id = %s AND cycle_number = %s",
            (user_id, cycle_number),
        ).fetchone()

        # Get previous cycle survey for context
        prev_survey = conn.execute(
            """
            SELECT ws.*, e.id as exp_id
            FROM weekly_surveys ws
            JOIN experiments e ON e.id = ws.experiment_id
            WHERE ws.user_id = %s AND ws.cycle_number = %s AND ws.status = 'completed'
            """,
            (user_id, cycle_number - 1),
        ).fetchone() if cycle_number > 1 else None

        # Get previous habits for context
        prev_template = conn.execute(
            "SELECT habit_1, habit_2, habit_3 FROM experiment_templates WHERE user_id = %s AND cycle_number = %s",
            (user_id, cycle_number - 1),
        ).fetchone() if cycle_number > 1 else None

        context_html = ""
        if prev_survey and prev_template:
            strategy_map = {"deepen": "Deepen", "swap": "Swap", "fresh_start": "Fresh Start"}
            felt_map = {
                "more_energy_focus": "More energy/focus",
                "better_structure_routine": "Better structure/routine",
                "less_anxiety_stress": "Less anxiety/stress",
                "no_noticeable_change": "No noticeable change"
            }
            context_html = f"""
            <div style="background:#f0f7ff; border-radius:8px; padding:16px; margin-bottom:20px;">
              <h3 style="margin-top:0;">Previous Week Context</h3>
              <p><strong>Habits:</strong> {prev_template['habit_1']}, {prev_template['habit_2']}, {prev_template['habit_3']}</p>
              <p><strong>Progress score:</strong> {prev_survey['q1_progress']}/5</p>
              <p><strong>Felt change:</strong> {felt_map.get(prev_survey['q2_felt_change'] or '', '?')}</p>
              <p><strong>Strategy requested:</strong> {strategy_map.get(prev_survey['q6_next_strategy'] or '', '?')}</p>
              {"<p><strong>Friction:</strong> " + (prev_survey['q5_friction_cause'] or '') + "</p>" if prev_survey['q5_friction_cause'] else ""}
            </div>"""

        def val(field):
            return (tmpl[field] or "") if tmpl else ""

        return HTMLResponse(f"""
        <html>
        <head>
          <title>Create Template</title>
          <style>
            body {{ font-family:sans-serif; max-width:700px; margin:40px auto; padding:0 20px; color:#2c3e50; }}
            input, textarea {{ width:100%; box-sizing:border-box; padding:10px; margin:6px 0 14px 0;
                               border:1px solid #ddd; border-radius:6px; font-size:14px; }}
            label {{ font-weight:500; font-size:14px; }}
            button {{ background:#2c3e50; color:white; border:none; padding:12px 24px;
                      border-radius:6px; font-size:15px; cursor:pointer; }}
            .back {{ color:#666; text-decoration:none; font-size:13px; }}
          </style>
        </head>
        <body>
          <a href="/analyst/dashboard" class="back">← Back to Dashboard</a>
          <h2>Create Template — Week {cycle_number}</h2>
          <p>User: <strong>{user_id}</strong> &nbsp;·&nbsp; Goal: <strong>{user['goal']}</strong></p>
          {context_html}
          <form method="POST" action="/analyst/template/{user_id}/{cycle_number}">
            <label>Habit 1</label>
            <input type="text" name="habit_1" value="{val('habit_1')}" placeholder="e.g., Meditate for 10 minutes after waking up" required>
            <label>Why this habit supports the goal</label>
            <textarea name="habit_1_research" rows="2" placeholder="Research note for the user...">{val('habit_1_research')}</textarea>

            <label>Habit 2</label>
            <input type="text" name="habit_2" value="{val('habit_2')}" placeholder="e.g., Write 3 talking points before any meeting" required>
            <label>Why this habit supports the goal</label>
            <textarea name="habit_2_research" rows="2" placeholder="Research note for the user...">{val('habit_2_research')}</textarea>

            <label>Habit 3</label>
            <input type="text" name="habit_3" value="{val('habit_3')}" placeholder="e.g., Record one voice memo per day summarising your thoughts" required>
            <label>Why this habit supports the goal</label>
            <textarea name="habit_3_research" rows="2" placeholder="Research note for the user...">{val('habit_3_research')}</textarea>

            <label>General research notes (shown to user in welcome email)</label>
            <textarea name="research_notes" rows="3" placeholder="Optional: overall context for this week's approach...">{val('research_notes')}</textarea>

            <button type="submit">Approve &amp; Send Welcome Email</button>
          </form>
        </body>
        </html>
        """)


@app.post("/analyst/template/{user_id}/{cycle_number}")
async def analyst_template_submit(
    user_id: str,
    cycle_number: int,
    habit_1: str = Form(...),
    habit_2: str = Form(...),
    habit_3: str = Form(...),
    habit_1_research: str = Form(""),
    habit_2_research: str = Form(""),
    habit_3_research: str = Form(""),
    research_notes: str = Form(""),
    analyst_session: str = Cookie(default=None),
):
    with get_db_conn() as conn:
        analyst = get_analyst_from_session(conn, analyst_session)
        if not analyst:
            return RedirectResponse(url="/analyst", status_code=302)

        goal = conn.execute(
            "SELECT goal FROM user_profiles WHERE user_id = %s", (user_id,)
        ).fetchone()["goal"]

        # Upsert template and approve
        conn.execute(
            """
            INSERT INTO experiment_templates (
                user_id, goal, cycle_number,
                habit_1, habit_2, habit_3,
                habit_1_research, habit_2_research, habit_3_research,
                research_notes, approved, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, NOW())
            ON CONFLICT (user_id, cycle_number) DO UPDATE SET
                habit_1 = EXCLUDED.habit_1,
                habit_2 = EXCLUDED.habit_2,
                habit_3 = EXCLUDED.habit_3,
                habit_1_research = EXCLUDED.habit_1_research,
                habit_2_research = EXCLUDED.habit_2_research,
                habit_3_research = EXCLUDED.habit_3_research,
                research_notes = EXCLUDED.research_notes,
                approved = true
            """,
            (user_id, goal, cycle_number, habit_1, habit_2, habit_3,
             habit_1_research or None, habit_2_research or None,
             habit_3_research or None, research_notes or None),
        )

        # Queue welcome email job
        conn.execute(
            """
            INSERT INTO first_email_jobs (user_id, goal, cycle_number, created_at, status)
            VALUES (%s, %s, %s, NOW(), 'pending')
            ON CONFLICT (user_id, cycle_number) DO UPDATE SET status = 'pending', completed_at = NULL
            """,
            (user_id, goal, cycle_number),
        )
        conn.commit()

    return RedirectResponse(url="/analyst/dashboard", status_code=302)


@app.get("/analyst/user/{user_id}", response_class=HTMLResponse)
async def analyst_user_detail(user_id: str, analyst_session: str = Cookie(default=None)):
    with get_db_conn() as conn:
        analyst = get_analyst_from_session(conn, analyst_session)
        if not analyst:
            return RedirectResponse(url="/analyst", status_code=302)

        user = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = %s", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        history = conn.execute(
            "SELECT * FROM user_goal_history WHERE user_id = %s ORDER BY cycle_number DESC",
            (user_id,),
        ).fetchall()

        surveys = conn.execute(
            "SELECT * FROM weekly_surveys WHERE user_id = %s ORDER BY cycle_number DESC",
            (user_id,),
        ).fetchall()

        history_rows = ""
        for h in history:
            history_rows += f"""
            <tr>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">Week {h['cycle_number']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{h['goal']}</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{h['overall_pct']}%</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{h['habit_1_pct']}% / {h['habit_2_pct']}% / {h['habit_3_pct']}%</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{h['q1_progress'] or '—'}/5</td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{h['q6_next_strategy'] or '—'}</td>
            </tr>"""

        survey_detail = ""
        for s in surveys:
            if s["status"] == "completed":
                survey_detail += f"""
                <div style="background:#f8f9fa; border-radius:8px; padding:16px; margin:12px 0;">
                  <h4 style="margin-top:0;">Week {s['cycle_number']} Survey</h4>
                  <p>Progress: {s['q1_progress']}/5 &nbsp;·&nbsp; Felt: {s['q2_felt_change']} &nbsp;·&nbsp; Strategy: {s['q6_next_strategy']}</p>
                  {"<p>Specific moment: " + (s['q3_specific_moment'] or '') + "</p>" if s['q3_specific_moment'] else ""}
                  {"<p>Friction: " + (s['q5_friction_cause'] or '') + "</p>" if s['q5_friction_cause'] else ""}
                  {"<p>New goal: " + (s['new_goal'] or '') + "</p>" if s['new_goal'] else ""}
                </div>"""

        # Find current waiting experiment for this user
        waiting = conn.execute(
            "SELECT cycle_number FROM experiments WHERE user_id = %s AND status = 'waiting' ORDER BY cycle_number DESC LIMIT 1",
            (user_id,),
        ).fetchone()

        action_btn = ""
        if waiting:
            action_btn = f'<a href="/analyst/template/{user_id}/{waiting["cycle_number"]}" style="background:#2c3e50; color:white; padding:10px 20px; border-radius:6px; text-decoration:none;">Create Week {waiting["cycle_number"]} Template</a>'

        return HTMLResponse(f"""
        <html>
        <head>
          <title>User Detail</title>
          <style>
            body {{ font-family:sans-serif; max-width:800px; margin:40px auto; padding:0 20px; color:#2c3e50; }}
            table {{ width:100%; border-collapse:collapse; }}
            thead tr {{ background:#2c3e50; color:white; }}
            th {{ padding:10px 12px; text-align:left; }}
            .back {{ color:#666; text-decoration:none; font-size:13px; }}
          </style>
        </head>
        <body>
          <a href="/analyst/dashboard" class="back">← Back to Dashboard</a>
          <h2>{user_id}</h2>
          <p>Current goal: <strong>{user['goal']}</strong> &nbsp;·&nbsp; Timezone: {user['timezone']}</p>
          {action_btn}

          <h3>Goal & Cycle History</h3>
          <table>
            <thead><tr><th>Cycle</th><th>Goal</th><th>Overall</th><th>H1/H2/H3</th><th>Progress</th><th>Strategy</th></tr></thead>
            <tbody>{history_rows if history_rows else '<tr><td colspan="6" style="padding:12px; color:#999;">No history yet</td></tr>'}</tbody>
          </table>

          <h3>Survey Responses</h3>
          {survey_detail if survey_detail else '<p style="color:#999;">No surveys yet.</p>'}
        </body>
        </html>
        """)


@app.get("/analyst/logout")
async def analyst_logout(analyst_session: str = Cookie(default=None)):
    with get_db_conn() as conn:
        if analyst_session:
            conn.execute("DELETE FROM analyst_sessions WHERE token = %s", (analyst_session,))
            conn.commit()
    response = RedirectResponse(url="/analyst", status_code=302)
    response.delete_cookie("analyst_session")
    return response


# ============================================================
# CORE API ENDPOINTS
# ============================================================

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

        # Check for existing active/waiting experiment
        existing = conn.execute(
            """
            SELECT id, status, cycle_number FROM experiments
            WHERE user_id = %s AND status IN ('waiting', 'active')
            ORDER BY created_at DESC LIMIT 1
            """,
            (email,),
        ).fetchone()

        if existing:
            conn.commit()
            return {
                "status": "already_enrolled",
                "user_id": email,
                "experiment_status": existing["status"],
                "cycle_number": existing["cycle_number"],
            }

        # Get next cycle number
        last = conn.execute(
            "SELECT MAX(cycle_number) as max_cycle FROM experiments WHERE user_id = %s",
            (email,),
        ).fetchone()
        cycle_number = (last["max_cycle"] or 0) + 1

        experiment_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO experiments (
                id, user_id, start_date, end_date, status,
                challenge_name, created_at, needs_email, cycle_number
            ) VALUES (%s, %s, NULL, NULL, 'waiting', %s, NOW(), true, %s)
            """,
            (experiment_id, email, goal, cycle_number),
        )

        # Create blank template for analyst
        conn.execute(
            """
            INSERT INTO experiment_templates (
                user_id, goal, cycle_number,
                habit_1, habit_2, habit_3, approved, created_at
            ) VALUES (%s, %s, %s, 'TBD', 'TBD', 'TBD', false, NOW())
            ON CONFLICT (user_id, cycle_number) DO NOTHING
            """,
            (email, goal, cycle_number),
        )
        conn.commit()

    return {
        "status": "subscribed",
        "user_id": email,
        "experiment_id": experiment_id,
        "cycle_number": cycle_number,
        "next_step": "Your analyst is preparing your personalized habits. Watch your inbox!",
    }


@app.post("/trigger-email")
async def trigger_email_on_approved(
    user_id: str = Body(..., embed=True),
    goal: str = Body(..., embed=True),
    cycle_number: int = Body(..., embed=True),
):
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO first_email_jobs (user_id, goal, cycle_number, created_at, status)
            VALUES (%s, lower(%s), %s, NOW(), 'pending')
            ON CONFLICT (user_id, cycle_number) DO UPDATE SET status = 'pending', completed_at = NULL
            """,
            (user_id, goal, cycle_number),
        )
        conn.commit()

    return {
        "status": "queued",
        "user_id": user_id,
        "cycle_number": cycle_number,
        "message": "Job queued — pg_cron will process within 1 minute",
    }


@app.post("/process-pending-emails")
async def process_pending_emails():
    print("🔄 /process-pending-emails called")
    result = process_first_email_jobs()
    return {"status": "ok", **result}


@app.post("/send-daily-checkins")
async def send_daily_checkins_endpoint():
    print("🔄 /send-daily-checkins called")
    send_daily_checkins()
    return {"status": "ok"}


@app.post("/inbound-email")
async def inbound_email(request: Request):
    form = await request.form()
    from_email = form.get("from", "")
    text_body = form.get("text", "") or ""

    if "<" in from_email and ">" in from_email:
        user_email = from_email.split("<")[1].split(">")[0].strip()
    else:
        user_email = from_email.strip()

    print(f"📨 Inbound email from {user_email}")

    with get_db_conn() as conn:
        exp = conn.execute(
            """
            SELECT e.id, e.start_date, e.cycle_number, up.timezone
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            WHERE e.user_id = %s AND e.status = 'active'
            ORDER BY e.created_at DESC LIMIT 1
            """,
            (user_email,),
        ).fetchone()

        if not exp:
            return {"status": "ignored"}

        experiment_id = str(exp["id"])
        tz_name = exp["timezone"] or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        today_local = datetime.now(ZoneInfo("UTC")).astimezone(tz).date()

        lines = [
            l.strip() for l in text_body.splitlines()
            if l.strip() and not l.strip().startswith(">") and not l.strip().startswith("On ")
        ]

        pending_dates = conn.execute(
            """
            SELECT dcj.checkin_date
            FROM daily_checkin_jobs dcj
            LEFT JOIN experiment_scores es
              ON es.experiment_id = %s AND es.date = dcj.checkin_date
            WHERE dcj.user_id = %s AND dcj.status = 'sent' AND es.id IS NULL
            ORDER BY dcj.checkin_date ASC
            """,
            (experiment_id, user_email),
        ).fetchall()

        if not pending_dates:
            return {"status": "no_pending_dates"}

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
            conn.commit()
            print(f"✅ {len(results)} days recorded for {user_email}")

        if not all_parsed and not results:
            conn.execute(
                """
                INSERT INTO daily_checkin_jobs (checkin_date, user_id, experiment_id, status, error_msg)
                VALUES (%s, %s, %s, 'sent', 'unrecognized_response')
                ON CONFLICT (checkin_date, user_id) DO UPDATE SET error_msg = 'unrecognized_response'
                """,
                (today_local, user_email, experiment_id),
            )
            conn.commit()
            print(f"⚠️ Unrecognized response from {user_email}")

    return {"status": "ok"}


@app.get("/checkin/{token}", response_class=HTMLResponse)
async def checkin_page(token: str):
    with get_db_conn() as conn:
        row = conn.execute(
            """
            SELECT ct.user_id, ct.experiment_id, ct.checkin_date,
                   et.habit_1, et.habit_2, et.habit_3
            FROM checkin_tokens ct
            JOIN experiments e ON e.id = ct.experiment_id
            JOIN experiment_templates et
              ON et.user_id = ct.user_id AND et.cycle_number = e.cycle_number AND et.approved = true
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
                <a href="/checkin/{token}/submit?habit={i}&val=1"
                   style="background:#27ae60; color:white; padding:6px 16px; border-radius:4px; text-decoration:none; margin-right:8px;">Y</a>
                <a href="/checkin/{token}/submit?habit={i}&val=0"
                   style="background:#e74c3c; color:white; padding:6px 16px; border-radius:4px; text-decoration:none;">N</a>
              </td>
            </tr>"""

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

    return HTMLResponse("""
    <html><body style="font-family:sans-serif; text-align:center; margin-top:80px;">
      <h2>✅ Got it!</h2>
      <p>Your response has been recorded.</p>
      <script>setTimeout(() => window.close(), 1500);</script>
    </body></html>
    """)


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

        days = len(scores)
        h1 = sum(r["habit_1"] for r in scores)
        h2 = sum(r["habit_2"] for r in scores)
        h3 = sum(r["habit_3"] for r in scores)

        return {
            "user_id": user_id,
            "experiment_id": experiment_id,
            "days_recorded": days,
            "habit_1_pct": round(h1 / days * 100, 1),
            "habit_2_pct": round(h2 / days * 100, 1),
            "habit_3_pct": round(h3 / days * 100, 1),
            "overall_pct": round((h1 + h2 + h3) / (days * 3) * 100, 1),
        }
@app.get("/", response_class=HTMLResponse)
async def landing_page():
    path = os.path.join(os.path.dirname(__file__), "landing.html")
    with open(path, "r") as f:
        return HTMLResponse(f.read())

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return HTMLResponse("""
    <html>
    <head><title>Privacy Policy — ImproveHabit</title>
    <style>body{font-family:sans-serif;max-width:680px;margin:60px auto;padding:0 24px;color:#24292f;line-height:1.7;}h1{font-size:24px;margin-bottom:8px;}h2{font-size:16px;margin:28px 0 8px;}p{font-size:14px;color:#57606a;margin-bottom:12px;}a{color:#3fb950;}</style>
    </head>
    <body>
    <h1>Privacy Policy</h1>
    <p>Last updated: June 2026</p>
    <h2>What we collect</h2>
    <p>We collect your email address and goal at registration. During the study we collect your daily habit responses (Y/N) and weekly survey answers. No other personal data is collected.</p>
    <h2>How we use it</h2>
    <p>Your data is used exclusively for this behavioral research study — to assign habits, track progress, and analyze aggregate patterns. We do not use it for advertising or any commercial purpose.</p>
    <h2>Who sees your data</h2>
    <p>Only the behavioral analysts running this study. Individual data is never shared publicly. Published findings use anonymized, aggregated data only.</p>
    <h2>Emails we send</h2>
    <p>Welcome email, daily check-in emails, weekly review emails, and a weekly research newsletter. You may unsubscribe at any time by replying "unsubscribe" to any email.</p>
    <h2>Data retention</h2>
    <p>We retain your data for the duration of your participation and up to 12 months afterward. You may request deletion at any time by emailing noreply@improvehabit.com.</p>
    <h2>Contact</h2>
    <p>noreply@improvehabit.com</p>
    <p><a href="/">← Back to ImproveHabit</a></p>
    </body></html>
    """)

# --- CREATE ANALYST (one-time setup endpoint) ---
@app.post("/admin/create-analyst")
async def create_analyst(
    email: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
    name: str = Body(..., embed=True),
    admin_key: str = Body(..., embed=True),
):
    if admin_key != os.getenv("ADMIN_KEY", ""):
        raise HTTPException(status_code=403, detail="Invalid admin key")

    with get_db_conn() as conn:
        existing = conn.execute("SELECT id FROM analysts WHERE email = %s", (email,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Analyst already exists")

        conn.execute(
            "INSERT INTO analysts (email, password_hash, name) VALUES (%s, %s, %s)",
            (email, hash_password(password), name),
        )
        conn.commit()

    return {"status": "created", "email": email}
