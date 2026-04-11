import os
import uuid
from datetime import date, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Body, Path, status
from fastapi.responses import JSONResponse
import psycopg
from dateutil import parser
import httpx

app = FastAPI(title="Habit Experiment API")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL env var required")

EMAIL_SERVICE_URL = os.getenv("EMAIL_SERVICE_URL", "https://habit-experiment-email.onrender.com")

def get_db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)


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
            status = "already_subscribed"
            needs_email = active_exp["needs_email"]
            # Reset for new cycle
            conn.execute("UPDATE experiments SET needs_email = true WHERE id = %s", (experiment_id,))
        else:
            # Create new
            experiment_id = str(uuid.uuid4())
            start_date = date.today()
            end_date = start_date + timedelta(days=7)
            status = "new_subscription"

            conn.execute(
                """
                INSERT INTO experiments (
                    id, user_id, start_date, end_date, status, challenge_name, created_at, needs_email
                ) VALUES (%s, %s, %s, %s, 'active', %s, NOW(), true)
                """,
                (experiment_id, email, start_date, end_date, goal),
            )

        # 3. AUTO-CREATE template (researcher edits habits + approves)
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

        # 4. Check if approved → send email?
        template_approved = conn.execute(
            """
            SELECT id FROM experiment_templates
            WHERE LOWER(goal) = LOWER(%s) AND approved = true
            """,
            (goal,),
        ).fetchone()

        should_send = bool(template_approved)
        
        if should_send:
            try:
                httpx.post(
                    f"{EMAIL_SERVICE_URL}/send-first-email",
                    json={
                        "user_email": email,
                        "goal": goal,
                        "experiment_id": experiment_id,
                        "start_date": start_date.isoformat(),
                    },
                    timeout=5.0,
                )
                conn.execute("UPDATE experiments SET needs_email = false WHERE id = %s", (experiment_id,))
            except Exception:
                pass  # Don't fail response

        return {
            "status": status,
            "user_id": email,
            "experiment_id": experiment_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "template_created": template_exists is None,  # True if we auto-created
            "email_sent": should_send,
            "next_step": "Researcher: Edit experiment_templates row → set approved=true" if not should_send else "Email sent!"
        }

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
    except:
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

        habit_1_pct = round((h1_total / days_recorded) * 100, 1)
        habit_2_pct = round((h2_total / days_recorded) * 100, 1)
        habit_3_pct = round((h3_total / days_recorded) * 100, 1)
        overall_pct = round(
            ((h1_total + h2_total + h3_total) / (days_recorded * 3)) * 100, 1
        )

        return {
            "user_id": user_id,
            "experiment_id": experiment_id,
            "days_recorded": days_recorded,
            "habit_1_pct": habit_1_pct,
            "habit_2_pct": habit_2_pct,
            "habit_3_pct": habit_3_pct,
            "overall_pct": overall_pct,
        }


@app.post("/trigger-email")
async def trigger_email_on_approved(
    goal: str = Body(..., embed=True),
):
    """Called by Supabase when experiment_templates.approved = true.
    Sends the first email to all experiments with this goal that still need it."""
    with get_db_conn() as conn:
        pending_exps = conn.execute(
            """
            SELECT e.id, e.user_id, e.start_date
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            WHERE e.status = 'active'
              AND e.needs_email = true
              AND LOWER(up.goal) = LOWER(%s)
        """,
            (goal,),
        ).fetchall()

        for exp in pending_exps:
            try:
                httpx.post(
                    f"{EMAIL_SERVICE_URL}/send-first-email",
                    json={
                        "user_email": exp["user_email"],
                        "goal": goal,
                        "experiment_id": exp["id"],
                        "start_date": exp["start_date"].isoformat(),
                    },
                    timeout=5.0,
                )
                conn.execute(
                    "UPDATE experiments SET needs_email = false WHERE id = %s",
                    (exp["id"],),
                )
            except Exception as e:
                # Log if needed
                pass

    return {
        "status": "processing",
        "goal": goal,
        "pending_count": len(pending_exps),
    }
