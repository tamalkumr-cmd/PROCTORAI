import json, uuid, time, random, boto3
from decimal import Decimal

REGION = "ap-south-1"
ddb = boto3.resource("dynamodb", region_name=REGION)
sessions = ddb.Table("ExamSessions")
students = ddb.Table("Students")
exams = ddb.Table("Exams")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,GET,POST",
}


class DecEnc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o) if o % 1 else int(o)
        return super().default(o)


def respond(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body, cls=DecEnc)}


def identity(event):
    c = event["requestContext"]["authorizer"]["jwt"]["claims"]
    return {
        "sub": c["sub"],
        "email": (c.get("email") or "").strip().lower(),
        "is_student": "students" in c.get("cognito:groups", ""),
    }


def public_questions(exam, order):
    """Questions in this student's order, with answer keys removed."""
    by_id = {q["id"]: q for q in exam.get("questions", [])}
    out = []
    for qid in order:
        q = by_id.get(qid)
        if not q:
            continue
        out.append({k: v for k, v in q.items() if k != "correct_option_id"})
    return out


def auto_grade(exam, answers):
    """Grade MCQs immediately. Returns (marks_by_qid, auto_total)."""
    marks, total = {}, 0
    for q in exam.get("questions", []):
        if q.get("type") != "mcq":
            continue
        given = answers.get(q["id"])
        awarded = int(q.get("marks", 1)) if given == q.get("correct_option_id") else 0
        marks[q["id"]] = awarded
        total += awarded
    return marks, total


def lambda_handler(event, context):
    try:
        me = identity(event)
    except KeyError:
        return respond(401, {"error": "Invalid token"})
    if not me["is_student"]:
        return respond(403, {"error": "Student accounts only"})

    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    params = event.get("queryStringParameters") or {}
    body = json.loads(event.get("body") or "{}")

    # ---------------- start or resume ----------------
    if path.endswith("/session/start"):
        exam_id = body.get("exam_id")
        if not exam_id:
            return respond(400, {"error": "exam_id required"})

        exam = exams.get_item(Key={"exam_id": exam_id}).get("Item")
        if not exam or str(exam_id).startswith("__"):
            return respond(404, {"error": "Exam not found"})
        if exam.get("status") != "published":
            return respond(403, {"error": "This exam isn't open right now"})

        now_ts = int(time.time())
        opens = int(exam.get("opens_at", 0) or 0)
        closes = int(exam.get("closes_at", 0) or 0)
        if opens and now_ts < opens:
            return respond(403, {"error": "This exam hasn't opened yet",
                                 "opens_at": opens, "now": now_ts})
        if closes and now_ts > closes:
            return respond(403, {"error": "This exam has closed",
                                 "closes_at": closes, "now": now_ts})
        if me["email"] not in [e.lower() for e in exam.get("allowed_emails", [])]:
            return respond(403, {"error": "You aren't on the roster for this exam"})

        student = students.get_item(Key={"student_id": me["sub"]}).get("Item")
        if not student or not student.get("enrolled"):
            return respond(409, {"error": "Complete enrolment before starting an exam",
                                 "needs_enrolment": True})

        mine = [s for s in sessions.scan().get("Items", [])
                if s.get("student_id") == me["sub"] and s.get("exam_id") == exam_id]

        # resume an unfinished attempt
        if exam.get("allow_resume", True):
            active = next((s for s in mine if s.get("status") == "active"), None)
            if active:
                left = int(active.get("expires_at", 0)) - int(time.time())
                if left > 0:
                    return respond(200, {
                        "session_id": active["session_id"],
                        "resumed": True,
                        "exam_title": exam.get("title"),
                        "description": exam.get("description", ""),
                        "questions": public_questions(exam, active.get("question_order", [])),
                        "answers": active.get("answers", {}),
                        "seconds_left": left,
                        "total_marks": exam.get("total_marks", 0),
                        "face_id": student.get("registered_face_id"),
                        "keystroke_baseline_ref": student.get("keystroke_baseline_ref"),
                        "composite_threshold": float(exam.get("composite_threshold", 0.5)),
                    })
                sessions.update_item(
                    Key={"session_id": active["session_id"]},
                    UpdateExpression="SET #s = :s, ended_at = :e",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "expired", ":e": int(time.time())},
                )

        used = len([s for s in mine if s.get("status") in ("submitted", "completed", "expired")])
        if used >= int(exam.get("max_attempts", 1)):
            return respond(409, {"error": f"You have used all {used} attempt(s) for this exam"})

        order = [q["id"] for q in exam.get("questions", [])]
        if exam.get("shuffle_questions"):
            random.shuffle(order)

        now = int(time.time())
        duration = int(exam.get("duration_min", 60)) * 60
        session_id = str(uuid.uuid4())

        sessions.put_item(Item={
            "session_id": session_id,
            "student_id": me["sub"],
            "student_email": me["email"],
            "exam_id": exam_id,
            "status": "active",
            "started_at": now,
            "expires_at": now + duration,
            "attempt_no": used + 1,
            "question_order": order,
            "answers": {},
            "overall_risk_score": Decimal("0"),
            "frames_checked": 0,
            "rekognition_calls": 0,
            "flag_count": 0,
            "face_id": student.get("registered_face_id"),
        })

        return respond(200, {
            "session_id": session_id,
            "resumed": False,
            "exam_title": exam.get("title"),
            "description": exam.get("description", ""),
            "questions": public_questions(exam, order),
            "answers": {},
            "seconds_left": duration,
            "total_marks": exam.get("total_marks", 0),
            "attempt_no": used + 1,
            "face_id": student.get("registered_face_id"),
            "keystroke_baseline_ref": student.get("keystroke_baseline_ref"),
            "composite_threshold": float(exam.get("composite_threshold", 0.5)),
        })

    # ---------------- autosave ----------------
    if path.endswith("/answers"):
        session_id = body.get("session_id")
        answers = body.get("answers") or {}
        sess = sessions.get_item(Key={"session_id": session_id}).get("Item")
        if not sess:
            return respond(404, {"error": "Session not found"})
        if sess["student_id"] != me["sub"]:
            return respond(403, {"error": "Not your session"})
        if sess.get("status") != "active":
            return respond(409, {"error": "This session is already closed"})

        left = int(sess.get("expires_at", 0)) - int(time.time())
        sessions.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET answers = :a, last_saved_at = :t",
            ExpressionAttributeValues={":a": answers, ":t": int(time.time())},
        )
        return respond(200, {"saved": True, "seconds_left": max(left, 0)})

    # ---------------- submit ----------------
    if path.endswith("/submit"):
        session_id = body.get("session_id")
        answers = body.get("answers") or {}
        sess = sessions.get_item(Key={"session_id": session_id}).get("Item")
        if not sess:
            return respond(404, {"error": "Session not found"})
        if sess["student_id"] != me["sub"]:
            return respond(403, {"error": "Not your session"})
        if sess.get("status") != "active":
            return respond(200, {"submitted": True, "already": True})

        exam = exams.get_item(Key={"exam_id": sess["exam_id"]}).get("Item") or {}
        auto_marks, auto_total = auto_grade(exam, answers)
        needs_manual = any(q.get("type") == "text" for q in exam.get("questions", []))
        expired = int(time.time()) > int(sess.get("expires_at", 0))

        sessions.update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET answers = :a, #s = :st, submitted_at = :t, ended_at = :t, "
                "auto_marks = :am, auto_total = :at, graded = :g"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":a": answers,
                ":st": "submitted",
                ":t": int(time.time()),
                ":am": auto_marks,
                ":at": auto_total,
                ":g": not needs_manual,
            },
        )
        return respond(200, {
            "submitted": True,
            "auto_total": auto_total,
            "awaiting_manual_grading": needs_manual,
            "auto_submitted": expired,
        })

    # ---------------- my result ----------------
    if path.endswith("/result") and method == "GET":
        exam_id = params.get("exam_id")
        exam = exams.get_item(Key={"exam_id": exam_id}).get("Item")
        if not exam:
            return respond(404, {"error": "Exam not found"})
        if not exam.get("results_published"):
            return respond(403, {"error": "Results for this exam aren't out yet"})

        mine = [s for s in sessions.scan().get("Items", [])
                if s.get("student_id") == me["sub"] and s.get("exam_id") == exam_id
                and s.get("status") in ("submitted", "completed")]
        if not mine:
            return respond(404, {"error": "No submission found"})
        sess = sorted(mine, key=lambda s: int(s.get("submitted_at", 0)))[-1]

        auto = {k: int(v) for k, v in (sess.get("auto_marks") or {}).items()}
        manual = {k: int(v) for k, v in (sess.get("manual_marks") or {}).items()}
        by_id = {q["id"]: q for q in exam.get("questions", [])}
        breakdown = []
        for qid in sess.get("question_order", list(by_id)):
            q = by_id.get(qid)
            if not q:
                continue
            breakdown.append({
                "id": qid,
                "type": q.get("type"),
                "text": q.get("text"),
                "max_marks": int(q.get("marks", 1)),
                "awarded": auto.get(qid, manual.get(qid)),
                "your_answer": (sess.get("answers") or {}).get(qid),
                "correct_option_id": q.get("correct_option_id") if q.get("type") == "mcq" else None,
                "options": q.get("options", []) if q.get("type") == "mcq" else [],
            })

        return respond(200, {
            "exam_title": exam.get("title"),
            "total_marks": int(exam.get("total_marks", 0)),
            "awarded": int(sess.get("total_awarded", sess.get("auto_total", 0))),
            "graded": bool(sess.get("graded")),
            "feedback": sess.get("feedback", ""),
            "breakdown": breakdown,
        })

    return respond(404, {"error": f"Unknown route: {method} {path}"})
