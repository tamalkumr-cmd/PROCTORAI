import json, time, uuid, traceback, boto3
from decimal import Decimal

REGION = "ap-south-1"
ddb = boto3.resource("dynamodb", region_name=REGION)
exams = ddb.Table("Exams")
sessions = ddb.Table("ExamSessions")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,GET,POST",
}


def jsonable(o):
    """Recursively convert DynamoDB types into plain JSON types."""
    if isinstance(o, Decimal):
        f = float(o)
        return int(f) if f.is_integer() else f
    if isinstance(o, (list, tuple, set)):
        return [jsonable(x) for x in o]
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    return o


def respond(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(jsonable(body))}


def as_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def as_list(v):
    return list(v) if isinstance(v, (list, tuple, set)) else []


def emails_of(exam):
    return [str(e).strip().lower() for e in as_list(exam.get("allowed_emails")) if e]


def identity(event):
    c = event["requestContext"]["authorizer"]["jwt"]["claims"]
    groups = c.get("cognito:groups", "")
    if isinstance(groups, list):
        groups = ",".join(groups)
    return {
        "sub": c["sub"],
        "email": str(c.get("email") or "").strip().lower(),
        "is_proctor": "proctors" in groups,
        "is_student": "students" in groups,
    }


def is_real_exam(item):
    return not str(item.get("exam_id", "")).startswith("__")


def clean_questions(raw):
    out, total = [], 0
    for i, q in enumerate(as_list(raw)):
        if not isinstance(q, dict):
            continue
        qtype = q.get("type", "text")
        if qtype not in ("text", "mcq"):
            raise ValueError(f"Question {i+1}: type must be text or mcq")
        text = str(q.get("text") or "").strip()
        if not text:
            raise ValueError(f"Question {i+1}: text is required")
        marks = as_int(q.get("marks", 1), 0)
        if marks < 1:
            raise ValueError(f"Question {i+1}: marks must be at least 1")

        item = {
            "id": q.get("id") or f"q{uuid.uuid4().hex[:6]}",
            "type": qtype,
            "text": text,
            "marks": marks,
        }

        if qtype == "mcq":
            opts = []
            for j, o in enumerate(as_list(q.get("options"))):
                otext = str((o or {}).get("text") or "").strip()
                if otext:
                    opts.append({"id": (o or {}).get("id") or chr(97 + j), "text": otext})
            if len(opts) < 2:
                raise ValueError(f"Question {i+1}: an MCQ needs at least 2 options")
            correct = q.get("correct_option_id")
            if correct not in [o["id"] for o in opts]:
                raise ValueError(f"Question {i+1}: mark one option as correct")
            item["options"] = opts
            item["correct_option_id"] = correct

        out.append(item)
        total += marks
    return out, total


def strip_answers(exam):
    safe = dict(exam)
    safe["questions"] = [
        {k: v for k, v in q.items() if k != "correct_option_id"}
        for q in as_list(exam.get("questions")) if isinstance(q, dict)
    ]
    safe.pop("allowed_emails", None)
    return safe


def scan_all(table):
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def handle(event):
    me = identity(event)
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    params = event.get("queryStringParameters") or {}
    body = json.loads(event.get("body") or "{}")

    # ---------- create or update ----------
    if path.endswith("/exams") and method == "POST":
        if not me["is_proctor"]:
            return respond(403, {"error": "Only proctors can create exams"})

        exam_id = body.get("exam_id") or f"exam-{uuid.uuid4().hex[:8]}"
        existing = exams.get_item(Key={"exam_id": exam_id}).get("Item") or {}

        if existing.get("status") == "published" and body.get("questions"):
            live = scan_all(sessions)
            if any(s.get("exam_id") == exam_id and s.get("status") == "active" for s in live):
                return respond(409, {"error": "Students are sitting this exam. Close it before editing questions."})

        title = str(body.get("title") or "").strip()
        if not title:
            return respond(400, {"error": "Give the exam a title"})

        try:
            questions, total = clean_questions(body.get("questions"))
        except ValueError as e:
            return respond(400, {"error": str(e)})

        allowed = sorted({
            str(e).strip().lower() for e in as_list(body.get("allowed_emails"))
            if e and "@" in str(e)
        })

        exams.put_item(Item={
            "exam_id": exam_id,
            "title": title,
            "description": str(body.get("description") or "").strip(),
            "status": existing.get("status", "draft"),
            "duration_min": as_int(body.get("duration_min"), 60),
            "opens_at": as_int(body.get("opens_at"), 0),
            "closes_at": as_int(body.get("closes_at"), 0),
            "shuffle_questions": bool(body.get("shuffle_questions", False)),
            "allow_resume": bool(body.get("allow_resume", True)),
            "auto_terminate": bool(body.get("auto_terminate", False)),
            "max_strikes": min(10, max(1, as_int(body.get("max_strikes"), 3))),
            "max_attempts": max(1, as_int(body.get("max_attempts"), 1)),
            "results_published": bool(existing.get("results_published", False)),
            "allowed_emails": allowed,
            "questions": questions,
            "total_marks": total,
            "created_by": existing.get("created_by", me["sub"]),
            "created_at": as_int(existing.get("created_at"), int(time.time())),
            "updated_at": int(time.time()),
            "composite_threshold": Decimal(str(as_float(body.get("composite_threshold"), 0.5))),
            "visual_weight": Decimal(str(as_float(body.get("visual_weight"), 0.6))),
            "keystroke_weight": Decimal(str(as_float(body.get("keystroke_weight"), 0.4))),
        })
        return respond(200, {"exam_id": exam_id, "total_marks": total,
                             "question_count": len(questions), "allowed_count": len(allowed)})

    # ---------- status ----------
    if path.endswith("/exams/status") and method == "POST":
        if not me["is_proctor"]:
            return respond(403, {"error": "Only proctors can change exam status"})
        exam_id = body.get("exam_id")
        new_status = body.get("status")
        if new_status not in ("draft", "published", "closed"):
            return respond(400, {"error": "status must be draft, published or closed"})
        exam = exams.get_item(Key={"exam_id": exam_id}).get("Item")
        if not exam:
            return respond(404, {"error": "Exam not found"})
        if new_status == "published":
            if not as_list(exam.get("questions")):
                return respond(400, {"error": "Add at least one question before publishing"})
            if not emails_of(exam):
                return respond(400, {"error": "Add at least one student email before publishing"})
        exams.update_item(
            Key={"exam_id": exam_id},
            UpdateExpression="SET #s = :s, updated_at = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": new_status, ":u": int(time.time())},
        )
        return respond(200, {"exam_id": exam_id, "status": new_status})

    # ---------- results toggle ----------
    if path.endswith("/exams/results") and method == "POST":
        if not me["is_proctor"]:
            return respond(403, {"error": "Only proctors can publish results"})
        exam_id = body.get("exam_id")
        published = bool(body.get("published", True))
        exams.update_item(
            Key={"exam_id": exam_id},
            UpdateExpression="SET results_published = :r, updated_at = :u",
            ExpressionAttributeValues={":r": published, ":u": int(time.time())},
        )
        return respond(200, {"exam_id": exam_id, "results_published": published})

    # ---------- roster ----------
    if path.endswith("/exams/roster") and method == "POST":
        if not me["is_proctor"]:
            return respond(403, {"error": "Only proctors can change the roster"})
        exam_id = body.get("exam_id")
        exam = exams.get_item(Key={"exam_id": exam_id}).get("Item")
        if not exam:
            return respond(404, {"error": "Exam not found"})
        current = set(emails_of(exam))
        add = {str(e).strip().lower() for e in as_list(body.get("add")) if e and "@" in str(e)}
        remove = {str(e).strip().lower() for e in as_list(body.get("remove"))}
        if body.get("replace") is not None:
            current = {str(e).strip().lower() for e in as_list(body["replace"]) if e and "@" in str(e)}
        else:
            current = (current | add) - remove
        exams.update_item(
            Key={"exam_id": exam_id},
            UpdateExpression="SET allowed_emails = :a, updated_at = :u",
            ExpressionAttributeValues={":a": sorted(current), ":u": int(time.time())},
        )
        return respond(200, {"exam_id": exam_id, "allowed_emails": sorted(current), "count": len(current)})

    # ---------- list ----------
    if path.endswith("/exams") and method == "GET":
        items = [e for e in scan_all(exams) if is_real_exam(e)]

        if me["is_proctor"]:
            rows = [{
                "exam_id": e.get("exam_id"),
                "title": e.get("title") or "Untitled",
                "status": e.get("status") or "draft",
                "question_count": len(as_list(e.get("questions"))),
                "total_marks": as_int(e.get("total_marks")),
                "allowed_count": len(emails_of(e)),
                "duration_min": as_int(e.get("duration_min"), 60),
                "opens_at": as_int(e.get("opens_at")),
                "closes_at": as_int(e.get("closes_at")),
                "results_published": bool(e.get("results_published")),
                "updated_at": as_int(e.get("updated_at")),
            } for e in items]
            rows.sort(key=lambda r: r["updated_at"], reverse=True)
            return respond(200, {"exams": rows, "role": "proctor"})

        my_sessions = [s for s in scan_all(sessions) if s.get("student_id") == me["sub"]]
        rows = []
        for e in items:
            if e.get("status") != "published":
                continue
            if me["email"] not in emails_of(e):
                continue
            mine = [s for s in my_sessions if s.get("exam_id") == e.get("exam_id")]
            done = [s for s in mine if s.get("status") in ("submitted", "completed")]
            active = next((s for s in mine if s.get("status") == "active"), None)
            rows.append({
                "exam_id": e.get("exam_id"),
                "title": e.get("title") or "Untitled",
                "description": e.get("description") or "",
                "duration_min": as_int(e.get("duration_min"), 60),
                "opens_at": as_int(e.get("opens_at")),
                "closes_at": as_int(e.get("closes_at")),
                "now": int(time.time()),
                "question_count": len(as_list(e.get("questions"))),
                "total_marks": as_int(e.get("total_marks")),
                "attempts_used": len(mine),
                "max_attempts": max(1, as_int(e.get("max_attempts"), 1)),
                "active_session": active.get("session_id") if active else None,
                "submitted": bool(done),
                "result_ready": bool(done) and bool(e.get("results_published")),
            })
        rows.sort(key=lambda r: r["title"])
        return respond(200, {"exams": rows, "role": "student"})

    # ---------- single exam ----------
    if path.endswith("/exam") and method == "GET":
        exam_id = params.get("exam_id")
        exam = exams.get_item(Key={"exam_id": exam_id}).get("Item") if exam_id else None
        if not exam or not is_real_exam(exam):
            return respond(404, {"error": "Exam not found"})
        if me["is_proctor"]:
            return respond(200, {"exam": exam})
        if exam.get("status") != "published":
            return respond(403, {"error": "This exam isn't open"})
        if me["email"] not in emails_of(exam):
            return respond(403, {"error": "You aren't on the roster for this exam"})
        return respond(200, {"exam": strip_answers(exam)})

    return respond(404, {"error": f"Unknown route: {method} {path}"})


def lambda_handler(event, context):
    try:
        return handle(event)
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        return respond(500, {"error": "Something went wrong. Try again."})
