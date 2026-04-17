from dotenv import load_dotenv
import os
 
# Explicitly load .env from this file's directory
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
 
import uuid
from datetime import date, timedelta
from fastapi import FastAPI, HTTPException, Body, Path, BackgroundTasks, status
from fastapi.responses import JSONResponse
import psycopg
from dateutil import parser
import resend
 
app = FastAPI(title="Habit Experiment API")
 
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL env var required")
 
resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "onboarding@resend.dev")
 
 
def get_db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
 
 
# --- SEND FIRST EMAIL (direct Resend call, no microservice) ---
def send_first_email(user_email: str, goal: str, experiment_id: str, start_date: str) -> bool:
    """Send first email via Resend. Returns True on success, False on failure."""
    if not resend.api_key:
        print("❌ RESEND_API_KEY is missing")
        return False
 
    try:
        with get_db_conn() as conn:
            template = conn.execute(
                """
                SELECT habit_1, habit_2, habit_3,
                       link_1, link_2, link_3,
                       description
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
                <p><strong>Day 1/7 Habits:</strong></p>
                <pre style="background: #f8f9fa; padding: 20px; border-radius: 6px;">{habits_text}</pre>
                <p><a href="https://habit-experiment-api.onrender.com/progress/{user_email}/{experiment_id}">
                    View Progress
                </a></p>
            </div>
            """
 
            resend.Emails.send(
                {
                    "from": EMAIL_FROM,
                    "to": user_email,
                    "subject": f"Your 7-Day Habit Experiment: {goal}",
                    "html": email_html,
                }
            )
            print(f"✅ Email sent to {user_email} for goal={goal}")
            return True
 
    except Exception as e:
        print(f"💥 Exception in send_first_email: {e}")
        return False
 
 
# --- PROCESS PENDING EMAIL JOBS ---
# Called by pg_cron every minute via POST /process-pending-emails
# Also called as a background task from /trigger-email
def process_first_email_jobs(goal: str = None):
    """
    Process pending first_email_jobs.
    If goal is provided, only process jobs for that goal.
    Otherwise process all pending jobs (used by pg_cron).
    """
    with get_db_conn() as conn:
        cur = conn.cursor()
 
        # Fetch pending jobs — scoped to goal if provided
        if goal:
            cur.execute(
                """
                SELECT id, goal FROM first_email_jobs
                WHERE lower(goal) = lower(%s)
                  AND status = 'pending'
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
                # Find all active experiments that still need first email for this goal
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
                    # Confirm template is still approved
                    template = conn.execute(
                        """
                        SELECT id FROM experiment_templates
                        WHERE lower(goal) = lower(%s)
                          AND approved = true;
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
                    else:
                        print(f"❌ Failed to send email for experiment {exp['id']}")
 
                # Only mark completed if all experiments were handled
                # (no pending_exps is also a valid completed state — nothing to send)
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
                print(f"❌ Error processing job {job_id} for goal {job_goal}: {e}")
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
 
 
# --- SUBSCRIBE ---
@app.post("/subscribe")
async def subscribe(
    email: str = Body(..., embed=True, min_length=5),
    goal: str = Body(..., embed=True, min_length=3),
):
    """Create or reuse active experiment + auto-create template for researcher approval"""
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Invalid email format")
 
    with get_db_conn() as conn:
        # 1. Upsert user_profile
        conn.execute(
            """
            INSERT INTO user_profiles (user_id, goal, timezone, created_at)
            VALUES (%s, %s, 'UTC', NOW())
            ON CONFLICT (user_id) DO UPDATE SET goal = EXCLUDED.goal
        """,
            (email, goal),
        )
        print("✅ user_profiles INSERT: user_id=", email, "goal=", goal)
 
        # 2. Check/create active experiment
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
            # Reset for new cycle
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
 
        # 3. Auto-create template if missing
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
 
        # 4. Check if approved → send email directly
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
        score_date = parser.parse(date_str).date()
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
 
        existing = conn.execute(
            """
            SELECT id FROM experiment_scores
            WHERE experiment_id = %s AND date = %s
        """,
            (experiment_id, score_date),
        ).fetchone()
 
        if existing:
            raise HTTPException(status_code=409, detail="Scores already recorded for this date")
 
        conn.execute(
            """
            INSERT INTO experiment_scores (experiment_id, user_id, date, habit_1, habit_2, habit_3, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """,
            (experiment_id, user_id, score_date, habit_1, habit_2, habit_3),
        )
 
    return JSONResponse(
        {
            "message": "Daily scores recorded successfully",
            "date": score_date.isoformat(),
        }
    )
 
 
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
        h1_total, h2_total, h3_total = 0, 0, 0
 
        for row in scores:
            h1_total += row["habit_1"]
            h2_total += row["habit_2"]
            h3_total += row["habit_3"]
 
        return {
            "user_id": user_id,
            "experiment_id": experiment_id,
            "days_recorded": days_recorded,
            "habit_1_pct": round((h1_total / days_recorded) * 100, 1),
            "habit_2_pct": round((h2_total / days_recorded) * 100, 1),
            "habit_3_pct": round((h3_total / days_recorded) * 100, 1),
            "overall_pct": round(
                ((h1_total + h2_total + h3_total) / (days_recorded * 3)) * 100, 1
            ),
        }
 
 
# --- TRIGGER EMAIL (called by Supabase pg_net on approval) ---
@app.post("/trigger-email")
async def trigger_email_on_approved(
    goal: str = Body(..., embed=True),
    background_tasks: BackgroundTasks = None,
):
    """Called by Supabase when experiment_templates.approved = true.
    Inserts a pending job and returns 200 immediately.
    pg_cron will pick it up within 1 minute via /process-pending-emails."""
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
    """
    Called by Supabase pg_cron every minute.
    Processes all pending first_email_jobs and sends emails directly via Resend.
    """
    print("🔄 /process-pending-emails called")
    result = process_first_email_jobs(goal=None)
    return {"status": "ok", **result}
