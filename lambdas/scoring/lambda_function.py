import json, base64, time, boto3
from decimal import Decimal

REGION = "ap-south-1"
BUCKET = "proctorai-media-tamal"
COLLECTION = "proctorai-faces"
MONTHLY_REKOG_CAP = 800
TOPIC_ARN = "arn:aws:sns:ap-south-1:582093723182:proctorai-alerts"

rekog = boto3.client("rekognition", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
sessions = ddb.Table("ExamSessions")
risk = ddb.Table("RiskScores")
exams = ddb.Table("Exams")
flagged = ddb.Table("FlaggedEvents")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}

_baseline_cache = {}


def respond(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body, default=str)}


def usage_key():
    return "__rekog_usage_" + time.strftime("%Y-%m")


def get_usage():
    item = exams.get_item(Key={"exam_id": usage_key()}).get("Item")
    return int(item.get("count", 0)) if item else 0


def bump_usage():
    exams.update_item(
        Key={"exam_id": usage_key()},
        UpdateExpression="SET #c = if_not_exists(#c, :z) + :one",
        ExpressionAttributeNames={"#c": "count"},
        ExpressionAttributeValues={":z": 0, ":one": 1},
    )


def load_baseline(ref):
    if not ref:
        return None
    if ref in _baseline_cache:
        return _baseline_cache[ref]
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=ref)
        data = json.loads(obj["Body"].read())
        _baseline_cache[ref] = data
        return data
    except Exception:
        return None


def keystroke_score(events, baseline):
    """Return 0.0 (normal) to 1.0 (very anomalous)."""
    if not events or not baseline:
        return 0.0
    dwells, flights = [], []
    for i, e in enumerate(events):
        dwells.append(e["up"] - e["down"])
        if i > 0:
            flights.append(e["down"] - events[i - 1]["up"])
    if not dwells:
        return 0.0
    dm = sum(dwells) / len(dwells)
    fm = sum(flights) / len(flights) if flights else 0.0

    def z(val, mean, std):
        std = max(float(std), 1.0)
        return abs(val - float(mean)) / std

    zd = z(dm, baseline["dwell_mean"], baseline["dwell_std"])
    zf = z(fm, baseline["flight_mean"], baseline["flight_std"]) if flights else 0.0
    combined = (zd + zf) / 2.0
    return min(combined / 3.0, 1.0)


def visual_score(flags, image_bytes, expected_face_id, allow_rekog, expected_student_id=None):
    detail = {"rekognition_used": False, "reason": None}

    if flags.get("no_face"):
        return 0.85, {**detail, "reason": "no_face_local"}
    if flags.get("multiple_faces"):
        return 0.95, {**detail, "reason": "multiple_faces_local"}
    base = 0.45 if flags.get("looking_away") else 0.0

    if not flags.get("needs_verify") or not image_bytes:
        return base, {**detail, "reason": "local_only"}

    if not allow_rekog:
        return base, {**detail, "reason": "rekognition_capped"}

    try:
        res = rekog.search_faces_by_image(
            CollectionId=COLLECTION,
            Image={"Bytes": image_bytes},
            MaxFaces=3,
            FaceMatchThreshold=80,
        )
    except rekog.exceptions.InvalidParameterException:
        return 0.85, {**detail, "rekognition_used": True, "reason": "no_face_rekognition"}
    except Exception as e:
        return base, {**detail, "reason": f"rekognition_error: {e}"}

    detail["rekognition_used"] = True
    matches = res.get("FaceMatches", [])
    if not matches:
        return 0.95, {**detail, "reason": "identity_mismatch"}

    mine = [m for m in matches
            if m["Face"].get("ExternalImageId") == expected_student_id
            or m["Face"]["FaceId"] == expected_face_id]
    if not mine:
        return 0.95, {**detail, "reason": "different_person"}
    m = max(mine, key=lambda x: x["Similarity"])

    sim = m["Similarity"]
    detail["similarity"] = round(sim, 2)
    return max(base, (100.0 - sim) / 100.0), {**detail, "reason": "identity_ok"}


def escalate(session_id, student_id, ts, composite, threshold, reason, image_bytes):
    """Write flagged event, persist clip, publish alert."""
    clip_key = None
    if image_bytes:
        clip_key = f"flagged/{session_id}/{ts}.jpg"
        s3.put_object(Bucket=BUCKET, Key=clip_key,
                      Body=image_bytes, ContentType="image/jpeg")

    flagged.put_item(Item={
        "session_id": session_id,
        "timestamp": ts,
        "event_type": reason,
        "score": Decimal(str(round(composite, 3))),
        "s3_clip_url": clip_key,
        "reviewed": False,
        "reviewer_notes": None,
    })

    try:
        sns.publish(
            TopicArn=TOPIC_ARN,
            Subject=f"ProctorAI alert: {reason}",
            Message=(
                f"Session: {session_id}\n"
                f"Student: {student_id}\n"
                f"Reason: {reason}\n"
                f"Composite score: {round(composite, 3)} (threshold {threshold})\n"
                f"Clip: {clip_key or 'none'}"
            ),
        )
    except Exception as e:
        print(f"SNS publish failed: {e}")

    return clip_key


STRIKE_COOLDOWN_SEC = 20


def terminate(sess, exam, strikes, reason):
    """Close the session, mark MCQs from saved answers, alert proctors."""
    answers = sess.get("answers") or {}
    qs = exam.get("questions", [])
    marks, total = {}, 0
    for q in qs:
        if q.get("type") != "mcq":
            continue
        m = int(q.get("marks", 1)) if answers.get(q["id"]) == q.get("correct_option_id") else 0
        marks[q["id"]] = m
        total += m
    needs_manual = any(q.get("type") == "text" for q in qs)
    now = int(time.time())
    why = f"Auto-ended after {strikes} integrity warnings (last: {reason})"
    try:
        sessions.update_item(
            Key={"session_id": sess["session_id"]},
            UpdateExpression=("SET #s = :st, submitted_at = :t, ended_at = :t, closed_reason = :r, "
                              "closed_by = :b, auto_terminated = :a, auto_marks = :m, "
                              "auto_total = :at, graded = :g"),
            ConditionExpression="#s = :active",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":st": "submitted", ":t": now, ":r": why, ":b": "system", ":a": True,
                ":m": marks, ":at": total, ":g": not needs_manual, ":active": "active",
            },
        )
    except Exception as e:
        print(f"terminate skipped: {e}")
        return why
    try:
        sns.publish(TopicArn=TOPIC_ARN,
                    Subject="ProctorAI: exam auto-ended",
                    Message=(f"Session {sess['session_id']} was ended automatically.\n"
                             f"Student: {sess.get('student_email') or sess['student_id']}\n"
                             f"Strikes: {strikes}\nLast violation: {reason}\n"
                             f"MCQ marks from saved answers: {total}"))
    except Exception as e:
        print(f"SNS failed: {e}")
    return why


def lambda_handler(event, context):
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        student_id = claims["sub"]
    except KeyError:
        return respond(401, {"error": "Invalid token"})

    try:
        body = json.loads(event["body"])
        session_id = body["session_id"]
        flags = body.get("flags", {})
        keystrokes = body.get("keystrokes", [])
        img_b64 = body.get("image")
        image_bytes = base64.b64decode(img_b64.split(",")[-1]) if img_b64 else None
    except Exception as e:
        return respond(400, {"error": f"Bad body: {e}"})

    sess = sessions.get_item(Key={"session_id": session_id}).get("Item")
    if not sess:
        return respond(404, {"error": "Session not found"})
    if sess["student_id"] != student_id:
        return respond(403, {"error": "Session belongs to another student"})
    if sess.get("status") != "active":
        return respond(409, {"error": "This session has been closed",
                             "session_closed": True,
                             "closed_reason": sess.get("closed_reason", "ended")})

    exam = exams.get_item(Key={"exam_id": sess["exam_id"]}).get("Item") or {}
    vw = float(exam.get("visual_weight", 0.6))
    kw = float(exam.get("keystroke_weight", 0.4))
    threshold = float(exam.get("composite_threshold", 0.65))

    used = get_usage()
    allow_rekog = used < MONTHLY_REKOG_CAP

    vs, detail = visual_score(flags, image_bytes, sess.get("face_id"), allow_rekog, student_id)
    if detail["rekognition_used"]:
        bump_usage()

    baseline = load_baseline(body.get("baseline_ref"))
    ks = keystroke_score(keystrokes, baseline)

    if flags.get("tab_switch"):
        vs = max(vs, 0.6)

    if flags.get("code_paste"):
        vs = max(vs, 0.7)
        detail["reason"] = "code_pasted"
    elif flags.get("typing_spike"):
        vs = max(vs, 0.5)
        detail["reason"] = "typing_spike"
    elif flags.get("robotic_timing"):
        vs = max(vs, 0.5)
        detail["reason"] = "robotic_typing"

    composite = (vs * vw) + (ks * kw)
    ts = int(time.time() * 1000)

    risk.put_item(Item={
        "session_id": session_id,
        "timestamp": ts,
        "visual_score": Decimal(str(round(vs, 3))),
        "keystroke_score": Decimal(str(round(ks, 3))),
        "composite_score": Decimal(str(round(composite, 3))),
        "reason": detail["reason"],
        "rekognition_used": detail["rekognition_used"],
    })

    escalated = False
    clip_key = None
    if composite >= threshold:
        escalated = True
        clip_key = escalate(session_id, student_id, ts, composite,
                            threshold, detail["reason"], image_bytes)

    sessions.update_item(
        Key={"session_id": session_id},
        UpdateExpression=(
            "SET overall_risk_score = :c, frames_checked = frames_checked + :one, "
            "rekognition_calls = rekognition_calls + :r, "
            "flag_count = if_not_exists(flag_count, :z) + :f"
        ),
        ExpressionAttributeValues={
            ":c": Decimal(str(round(composite, 3))),
            ":one": 1,
            ":r": 1 if detail["rekognition_used"] else 0,
            ":f": 1 if escalated else 0,
            ":z": 0,
        },
    )

    strike = None
    is_violation = escalated or bool(flags.get("tab_switch")) or bool(flags.get("code_paste"))
    if is_violation and exam.get("auto_terminate"):
        max_strikes = max(1, int(exam.get("max_strikes", 3) or 3))
        now_s = ts // 1000
        last = int(sess.get("last_strike_at", 0) or 0)
        strikes = int(sess.get("strike_count", 0) or 0)
        counted = now_s - last >= STRIKE_COOLDOWN_SEC
        if counted:
            strikes += 1
            sessions.update_item(
                Key={"session_id": session_id},
                UpdateExpression="SET strike_count = :s, last_strike_at = :t",
                ExpressionAttributeValues={":s": strikes, ":t": now_s},
            )
        terminated = strikes >= max_strikes
        closed_reason = None
        if terminated:
            closed_reason = terminate(sess, exam, strikes,
                                      "tab switch" if flags.get("tab_switch") and not escalated
                                      else detail["reason"])
        strike = {"count": strikes, "max": max_strikes, "counted": counted,
                  "terminated": terminated, "closed_reason": closed_reason,
                  "reason": "tab_switch" if flags.get("tab_switch") and not escalated else detail["reason"]}

    return respond(200, {
        "strike": strike,
        "visual_score": round(vs, 3),
        "keystroke_score": round(ks, 3),
        "composite_score": round(composite, 3),
        "reason": detail["reason"],
        "rekognition_used": detail["rekognition_used"],
        "escalated": escalated,
        "clip": clip_key,
        "threshold": threshold,
        "rekognition_budget_left": max(0, MONTHLY_REKOG_CAP - used),
    })
