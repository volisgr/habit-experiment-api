import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import resend
import psycopg

app = FastAPI(title="Habit Experiment Email Service")

DATABASE_URL = os.getenv("DATABASE_URL")
resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@habitexperiment.com")

class FirstEmailRequest(BaseModel):
    user_email: str
    goal: str
    experiment_id: str
    start_date: str

def get_db_conn():
    return psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)

@app.post("/send-first-email")
async def send_first_email(req: FirstEmailRequest):
    print("🌀 /send-first-email called", req.dict())  # <-- log request

    if not resend.api_key:
        return {"status": "noop", "message": "No RESEND_API_KEY"}

    try:
        with get_db_conn() as conn:
            # Debug: confirm connection
            print("✅ DB connection opened")

            template = conn.execute("""
                SELECT habit_1, habit_2, habit_3,
                       link_1, link_2, link_3,
                       description
                FROM experiment_templates
                WHERE LOWER(goal) = LOWER(%s) AND approved = true
                LIMIT 1
            """, (req.goal,)).fetchone()

            if not template:
                print("🚫 No approved template for goal:", req.goal)
                return {"status": "skip", "message": f"No approved template for '{req.goal}'"}

            print("✅ Template found:", template)

            habits = [template["habit_1"], template["habit_2"], template["habit_3"]]
            links = [template["link_1"] or "", template["link_2"] or "", template["link_3"] or ""]
            description = template["description"] or "Behavioral research study."

            habits_text = "\n".join([f"• {h} {'→ ' + l if l else ''}" for h, l in zip(habits, links)])

            email_html = f"""
            <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px;">
                <h2>Your 7-Day Habit Experiment: {req.goal}</h2>
                <p>{description}</p>
                <p><strong>Day 1/7 Habits:</strong></p>
                <pre style="background: #f8f9fa; padding: 20px; border-radius: 6px;">{habits_text}</pre>
                <p><a href="https://habit-experiment-api.onrender.com/progress/{req.user_email}/{req.experiment_id}">
                    View Progress
                </a></p>
            </div>
            """

            print("📧 Sending email to:", req.user_email)

            resend.Emails.send({
                "from": EMAIL_FROM,
                "to": req.user_email,
                "subject": f"Your 7-Day Habit Experiment: {req.goal}",
                "html": email_html
            })

            return {"status": "sent", "user_email": req.user_email}

    except Exception as e:
        print("💥 ERROR in /send-first-email:", str(e))  # <-- important
        raise HTTPException(status_code=500, detail=str(e))
