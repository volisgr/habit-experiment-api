# api/trigger_pending_emails.py
import httpx
import psycopg
import os
from datetime import date

DATABASE_URL = os.getenv("DATABASE_URL")
EMAIL_SERVICE_URL = os.getenv("EMAIL_SERVICE_URL", "http://localhost:8001")


def get_db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)


def trigger_pending_emails():
    """Send first‑email to experiments that have an approved template and needs_email=true."""
    with get_db_conn() as conn:
        pending_exps = conn.execute("""
            SELECT e.id, e.user_id, e.start_date, up.goal
            FROM experiments e
            JOIN user_profiles up ON up.user_id = e.user_id
            WHERE e.status = 'active'
              AND e.needs_email = true
            LIMIT 100
        """).fetchall()

        for exp in pending_exps:
            template = conn.execute("""
                SELECT id FROM experiment_templates
                WHERE LOWER(goal) = LOWER(%s) AND approved = true
            """, (exp["goal"],)).fetchone()

            if template:
                try:
                    resp = httpx.post(
                        f"{EMAIL_SERVICE_URL}/send-first-email",
                        json={
                            "user_email": exp["user_id"],
                            "goal": exp["goal"],
                            "experiment_id": exp["id"],
                            "start_date": exp["start_date"].isoformat(),
                        },
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        conn.execute(
                            "UPDATE experiments SET needs_email = false WHERE id = %s",
                            (exp["id"],),
                        )
                except Exception as e:
                    # Log as needed, but don’t fail
                    pass
