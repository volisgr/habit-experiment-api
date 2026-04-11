import os
from fastapi import FastAPI, HTTPException
import resend
from pydantic import BaseModel
import psycopg

app = FastAPI(title="Habit Experiment Email Service")

# Same Supabase DB for habit templates
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
    """Send first-day email (researcher-approved habits from DB)"""
    if not resend.api_key:
        return {"status": "noop", "message": "No RESEND_API_KEY"}
    
    with get_db_conn() as conn:
        # Fetch researcher-approved habits for this goal
        template = conn.execute("""
            SELECT habits, links, description 
            FROM experiment_templates 
            WHERE LOWER(goal) = LOWER(%s) AND approved = true
            LIMIT 1
        """, (req.goal,)).fetchone()
        
        if not template:
            return {"status": "skip", "message": f"No approved template for '{req.goal}'"}
        
        # Reconstruct habits list
        habits = template["habits"].split("||")  # Stored as "Habit1||Habit2||Habit3"
        links = template["links"].split("||")
        description = template["description"]
        
        habits_text = "\n".join([f"• {h} → {l}" for h, l in zip(habits, links)])
        
        email_html = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px; line-height: 1.6;">
            <h2>Hi,</h2>
            <p>You said you want to improve <strong>'{req.goal}'</strong>.</p>
            <p>{description}</p>
            
            <p><strong>Try these 3 habits daily:</strong></p>
            <div style="background: #f8f9fa; padding: 20px; border-left: 4px solid #007cba; border-radius: 6px; margin: 20px 0;">
                <pre style="margin: 0; font-size: 16px; line-height: 1.5; white-space: pre-wrap;">
{habits_text}
                </pre>
            </div>
            
            <p>You'll get a short daily check-in email—just tap to respond.</p>
            <p>At the end of 7 days, we'll ask what changed and adapt from there.</p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 32px 0;">
            <p style="color: #666; font-size: 14px;">
                —
                <br>Part of an ongoing research study.
            </p>
        </div>
        """
        
        try:
            resend.Emails.send({
                "from": EMAIL_FROM,
                "to": req.user_email,
                "subject": f"Your 7-Day Habit Experiment: {req.goal}",
                "html": email_html
            })
            return {"status": "sent", "user_email": req.user_email, "experiment_id": req.experiment_id}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
