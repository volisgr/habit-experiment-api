from fastapi import FastAPI, HTTPException, Body
from datetime import date, timedelta
import os
import uuid
import psycopg
from dotenv import load_dotenv
from typing import Literal

load_dotenv()

app = FastAPI(title="Habit Experiment API")

# Light helpers (no Pydantic)
def is_boolish_int(x: int) -> bool:
    return x in (0, 1)


@app.post("/subscribe")
def subscribe(
    email: str = Body(..., min_length=3),  # basic sanity; client should still send valid email
    goal: str = Body(..., min_length=1),
    timezone: str = Body("UTC"),
):
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL not set")

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Simple email format check (client‑side santy, not robust, but for MVP)
            if "@" not in email or "." not in email.split("@")[-1]:
                raise HTTPException(400, "Invalid email")

            user_id = email

            # Upsert user_profiles
            cur.execute(
                """
                INSERT INTO user_profiles (user_id, goal, timezone, created_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET 
                    goal = EXCLUDED.goal, 
                    timezone = EXCLUDED.timezone
                """,
                (user_id, goal, timezone),
            )

            # Check for active experiment
            cur.execute(
                """
                SELECT id, start_date, end_date, created_at 
                FROM experiments 
                WHERE user_id = %s AND status = 'active'
                """,
                (user_id,),
            )
            active = cur.fetchone()

            if active:
                exp_id = str(active[0])
                start_date = active[1].isoformat()
                end_date = active[2].isoformat()
                sub_date = active[3].strftime("%Y-%m-%d")
                return {
                    "user_id": user_id,
                    "experiment_id": exp_id,
                    "message": f"You already subscribed on {sub_date}! Check your email (including spam/promotions) for habit instructions. Experiment runs {start_date} to {end_date}.",
                    "start_date": start_date,
                    "end_date": end_date,
                    "status": "already_subscribed",
                }

            # Create new experiment
            experiment_id = str(uuid.uuid4())
            start_date = date.today()
            end_date = start_date + timedelta(days=7)
            cur.execute(
                """
                INSERT INTO experiments (id, user_id, start_date, end_date, status, challenge_name, created_at)
                VALUES (%s, %s, %s, %s, 'active', %s, NOW())
                """,
                (experiment_id, user_id, start_date, end_date, goal),
            )

            conn.commit()
            return {
                "user_id": user_id,
                "experiment_id": experiment_id,
                "message": "New 7-day experiment started! Check your email for habit instructions.",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "status": "new_subscription",
            }


@app.post("/scores")
def post_scores(
    user_id: str = Body(..., min_length=1),
    experiment_id: str = Body(..., min_length=1),
    date: str = Body(..., min_length=10),  # "YYYY-MM-DD" as string
    habit_1: int = Body(..., ge=0, le=1),
    habit_2: int = Body(..., ge=0, le=1),
    habit_3: int = Body(..., ge=0, le=1),
):
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL not set")

    # Parse date
    try:
        score_date = date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "Invalid date format; use YYYY-MM-DD")

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Verify experiment exists
            cur.execute(
                """
                SELECT id FROM experiments 
                WHERE id = %s AND user_id = %s
                """,
                (uuid.UUID(experiment_id), user_id),
            )
            if not cur.fetchone():
                raise HTTPException(404, "Experiment not found for this user")

            # Check duplicate date
            cur.execute(
                """
                SELECT id FROM experiment_scores 
                WHERE experiment_id = %s AND date = %s
                """,
                (uuid.UUID(experiment_id), score_date),
            )
            if cur.fetchone():
                raise HTTPException(409, "Score for this date already exists")

            # Insert score
            cur.execute(
                """
                INSERT INTO experiment_scores 
                (experiment_id, user_id, date, habit_1, habit_2, habit_3, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    uuid.UUID(experiment_id),
                    user_id,
                    score_date,
                    habit_1,
                    habit_2,
                    habit_3,
                ),
            )

            conn.commit()
            return {
                "message": "Daily scores recorded successfully",
                "date": score_date.isoformat(),
            }


@app.get("/progress/{user_id}/{experiment_id}")
def get_progress(user_id: str, experiment_id: str):
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL not set")

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Fetch scores for this experiment/user
            cur.execute(
                """
                SELECT habit_1, habit_2, habit_3 FROM experiment_scores 
                WHERE experiment_id = %s AND user_id = %s
                """,
                (uuid.UUID(experiment_id), user_id),
            )
            scores = cur.fetchall()

            if not scores:
                raise HTTPException(404, "No scores found for this experiment")

            days = len(scores)
            h1_total = sum(row[0] for row in scores)
            h2_total = sum(row[1] for row in scores)
            h3_total = sum(row[2] for row in scores)

            h1_pct = round((h1_total / days) * 100, 1)
            h2_pct = round((h2_total / days) * 100, 1)
            h3_pct = round((h3_total / days) * 100, 1)
            overall_pct = round(((h1_pct + h2_pct + h3_pct) / 3), 1)

            return {
                "user_id": user_id,
                "experiment_id": experiment_id,
                "days_recorded": days,
                "habit_1_pct": h1_pct,
                "habit_2_pct": h2_pct,
                "habit_3_pct": h3_pct,
                "overall_pct": overall_pct,
            }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
