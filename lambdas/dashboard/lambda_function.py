import json, time, boto3
from decimal import Decimal
from boto3.dynamodb.conditions import Key

REGION = "ap-south-1"
BUCKET = "proctorai-media-tamal"

s3 = boto3.client("s3", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
sessions = ddb.Table("ExamSessions")
risk = ddb.Table("RiskScores")
flagged = ddb.Table("FlaggedEvents")
students = ddb.Table("Students")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,GET,POST",
}


class DecEnc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def respond(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body, cls=DecEnc)}


def require_proctor(event):
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    if "proctors" not in claims.get("cognito:groups", ""):
        return None
    return claims["sub"]


def lambda_handler(event, context):
    try:
        proctor_id = require_proctor(event)
    except KeyError:
        return respond(401, {"error": "Invalid token"})
    if not proctor_id:
        return respond(403, {"error": "Proctor accounts only"})

    path = event.get("rawPath", "")
    params = event.get("queryStringParameters") or {}

    # ---- live roster ----
    if path.endswith("/roster"):
        exam_id = params.get("exam_id", "exam-001")
        items = sessions.scan().get("Items", [])
        rows = []
        for s in items:
            if s.get("exam_id") != exam_id:
                continue
            stu = students.get_item(
                Key={"student_id": s["student_id"]}).get("Item") or {}
            rows.append({
                "session_id": s["session_id"],
                "student_id": s["student_id"],
                "name": stu.get("name", "Unknown"),
                "status": s.get("status"),
                "risk": s.get("overall_risk_score", 0),
                "frames_checked": s.get("frames_checked", 0),
                "rekognition_calls": s.get("rekognition_calls", 0),
                "flag_count": s.get("flag_count", 0),
                "started_at": s.get("started_at"),
            })
        rows.sort(key=lambda r: float(r["risk"] or 0), reverse=True)
        total_frames = sum(int(r["frames_checked"] or 0) for r in rows)
        total_rekog = sum(int(r["rekognition_calls"] or 0) for r in rows)
        return respond(200, {
            "sessions": rows,
            "totals": {
                "frames_checked": total_frames,
                "rekognition_calls": total_rekog,
                "savings_pct": round(
                    (1 - total_rekog / total_frames) * 100, 1) if total_frames else 100.0,
            },
        })

    # ---- risk timeline ----
    if path.endswith("/timeline"):
        sid = params.get("session_id")
        if not sid:
            return respond(400, {"error": "session_id required"})
        res = risk.query(
            KeyConditionExpression=Key("session_id").eq(sid),
            ScanIndexForward=True, Limit=500)
        return respond(200, {"points": res.get("Items", [])})

    # ---- flagged events ----
    if path.endswith("/flags"):
        sid = params.get("session_id")
        if sid:
            res = flagged.query(
                KeyConditionExpression=Key("session_id").eq(sid),
                ScanIndexForward=False, Limit=100)
            items = res.get("Items", [])
        else:
            items = flagged.scan(Limit=100).get("Items", [])
            items.sort(key=lambda i: int(i.get("timestamp", 0)), reverse=True)
        for it in items:
            key = it.get("s3_clip_url")
            if key:
                it["clip_signed_url"] = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": BUCKET, "Key": key},
                    ExpiresIn=900)
        return respond(200, {"flags": items})

    # ---- review a flag ----
    if path.endswith("/review"):
        body = json.loads(event.get("body") or "{}")
        sid = body.get("session_id")
        ts = body.get("timestamp")
        if not sid or ts is None:
            return respond(400, {"error": "session_id and timestamp required"})
        flagged.update_item(
            Key={"session_id": sid, "timestamp": int(ts)},
            UpdateExpression="SET reviewed = :r, reviewer_notes = :n, reviewed_by = :p",
            ExpressionAttributeValues={
                ":r": bool(body.get("reviewed", True)),
                ":n": body.get("notes", ""),
                ":p": proctor_id,
            },
        )
        return respond(200, {"updated": True})

    # ---- end a session ----
    if path.endswith("/end"):
        body = json.loads(event.get("body") or "{}")
        sid = body.get("session_id")
        if not sid:
            return respond(400, {"error": "session_id required"})
        sessions.update_item(
            Key={"session_id": sid},
            UpdateExpression=("SET #s = :st, ended_at = :e, closed_reason = :r, "
                              "closed_by = :p"),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":st": "submitted",
                ":e": int(time.time()),
                ":r": str(body.get("reason") or "Ended by proctor"),
                ":p": proctor_id,
            },
        )
        return respond(200, {"ended": True})

    # ---- post-exam summary ----
    if path.endswith("/summary"):
        exam_id = params.get("exam_id", "exam-001")
        items = [s for s in sessions.scan().get("Items", [])
                 if s.get("exam_id") == exam_id]
        rows, tf, tr, tflags = [], 0, 0, 0
        for s in items:
            stu = students.get_item(
                Key={"student_id": s["student_id"]}).get("Item") or {}
            fl = flagged.query(
                KeyConditionExpression=Key("session_id").eq(s["session_id"])
            ).get("Items", [])
            confirmed = sum(1 for f in fl if f.get("reviewed") and
                            "Dismissed" not in str(f.get("reviewer_notes", "")))
            f_checked = int(s.get("frames_checked", 0) or 0)
            r_calls = int(s.get("rekognition_calls", 0) or 0)
            tf += f_checked
            tr += r_calls
            tflags += len(fl)
            rows.append({
                "student": stu.get("name", "Unknown"),
                "session_id": s["session_id"],
                "status": s.get("status"),
                "peak_risk": s.get("overall_risk_score", 0),
                "frames_checked": f_checked,
                "rekognition_calls": r_calls,
                "flags_raised": len(fl),
                "flags_confirmed": confirmed,
                "duration_sec": (int(s.get("ended_at", time.time()))
                                 - int(s.get("started_at", 0))),
            })
        rows.sort(key=lambda r: float(r["peak_risk"] or 0), reverse=True)
        return respond(200, {
            "exam_id": exam_id,
            "sessions": rows,
            "aggregate": {
                "session_count": len(rows),
                "frames_checked": tf,
                "rekognition_calls": tr,
                "calls_avoided": tf - tr,
                "savings_pct": round((1 - tr / tf) * 100, 1) if tf else 100.0,
                "estimated_cost_usd": round(tr * 0.001, 4),
                "cost_without_prefilter_usd": round(tf * 0.001, 4),
                "flags_raised": tflags,
            },
        })
    # ---- submissions for an exam ----
    if path.endswith("/submissions"):
        exam_id = params.get("exam_id")
        if not exam_id:
            return respond(400, {"error": "exam_id required"})

        exam = ddb.Table("Exams").get_item(Key={"exam_id": exam_id}).get("Item")
        if not exam:
            return respond(404, {"error": "Exam not found"})

        qs = exam.get("questions", [])
        by_id = {q["id"]: q for q in qs}

        rows = []
        for s in sessions.scan().get("Items", []):
            if s.get("exam_id") != exam_id:
                continue
            if s.get("status") not in ("submitted", "completed", "expired"):
                continue
            stu = students.get_item(Key={"student_id": s["student_id"]}).get("Item") or {}
            auto = {k: int(v) for k, v in (s.get("auto_marks") or {}).items()}
            manual = {k: int(v) for k, v in (s.get("manual_marks") or {}).items()}

            items = []
            for qid in s.get("question_order", list(by_id)):
                q = by_id.get(qid)
                if not q:
                    continue
                items.append({
                    "id": qid,
                    "type": q.get("type"),
                    "text": q.get("text"),
                    "max_marks": int(q.get("marks", 1)),
                    "options": q.get("options", []),
                    "correct_option_id": q.get("correct_option_id"),
                    "answer": (s.get("answers") or {}).get(qid),
                    "auto": auto.get(qid),
                    "manual": manual.get(qid),
                    "language": q.get("language"),
                    "test_count": len(q.get("tests", []) or []),
                })

            flags = flagged.query(
                KeyConditionExpression=Key("session_id").eq(s["session_id"]),
                ScanIndexForward=False, Limit=12
            ).get("Items", [])
            flag_items = []
            for f in flags:
                key = f.get("s3_clip_url")
                flag_items.append({
                    "timestamp": int(f.get("timestamp", 0)),
                    "event_type": f.get("event_type"),
                    "score": f.get("score"),
                    "reviewed": bool(f.get("reviewed")),
                    "clip_signed_url": s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": BUCKET, "Key": key},
                        ExpiresIn=900) if key else None,
                })

            awarded = sum(auto.values()) + sum(manual.values())
            pending = sum(1 for i in items if i["type"] == "text" and i["manual"] is None)

            rows.append({
                "session_id": s["session_id"],
                "student_id": s["student_id"],
                "name": stu.get("name", "Unknown"),
                "email": s.get("student_email", ""),
                "status": s.get("status"),
                "submitted_at": int(s.get("submitted_at", 0) or 0),
                "peak_risk": s.get("overall_risk_score", 0),
                "flag_count": int(s.get("flag_count", 0) or 0),
                "graded": bool(s.get("graded")),
                "awarded": awarded,
                "pending_count": pending,
                "code_detail": s.get("code_detail") or {},
                "code_events": s.get("code_events") or [],
                "feedback": s.get("feedback", ""),
                "flags": flag_items,
                "closed_reason": s.get("closed_reason"),
                "questions": items,
            })

        rows.sort(key=lambda r: (r["graded"], -float(r["peak_risk"] or 0)))
        return respond(200, {
            "exam_title": exam.get("title"),
            "total_marks": int(exam.get("total_marks", 0)),
            "results_published": bool(exam.get("results_published")),
            "submissions": rows,
        })

    # ---- save marks for one submission ----
    if path.endswith("/grade"):
        body = json.loads(event.get("body") or "{}")
        sid = body.get("session_id")
        if not sid:
            return respond(400, {"error": "session_id required"})

        sess = sessions.get_item(Key={"session_id": sid}).get("Item")
        if not sess:
            return respond(404, {"error": "Session not found"})

        exam = ddb.Table("Exams").get_item(Key={"exam_id": sess["exam_id"]}).get("Item") or {}
        by_id = {q["id"]: q for q in exam.get("questions", [])}

        manual = {}
        for qid, val in (body.get("manual_marks") or {}).items():
            q = by_id.get(qid)
            if not q or q.get("type") != "text":
                continue
            try:
                n = int(val)
            except (TypeError, ValueError):
                continue
            manual[qid] = max(0, min(n, int(q.get("marks", 1))))

        auto = {k: int(v) for k, v in (sess.get("auto_marks") or {}).items()}
        total = sum(auto.values()) + sum(manual.values())
        text_qs = [q["id"] for q in exam.get("questions", []) if q.get("type") == "text"]
        fully = all(qid in manual for qid in text_qs)

        sessions.update_item(
            Key={"session_id": sid},
            UpdateExpression=(
                "SET manual_marks = :m, total_awarded = :t, feedback = :f, "
                "graded = :g, graded_by = :p, graded_at = :ts, #s = :st"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":m": manual,
                ":t": total,
                ":f": str(body.get("feedback") or ""),
                ":g": bool(fully),
                ":p": proctor_id,
                ":ts": int(time.time()),
                ":st": "completed" if fully else sess.get("status", "submitted"),
            },
        )
        return respond(200, {"session_id": sid, "total_awarded": total, "graded": fully})
    # ---- analytics for one exam ----
    if path.endswith("/analytics"):
        exam_id = params.get("exam_id")
        if not exam_id:
            return respond(400, {"error": "exam_id required"})

        exam = ddb.Table("Exams").get_item(Key={"exam_id": exam_id}).get("Item")
        if not exam:
            return respond(404, {"error": "Exam not found"})

        qs = exam.get("questions", [])
        by_id = {q["id"]: q for q in qs}
        total_marks = int(exam.get("total_marks", 0) or 0)

        sess_rows = [s for s in sessions.scan().get("Items", [])
                     if s.get("exam_id") == exam_id
                     and s.get("status") in ("submitted", "completed", "expired")]

        # per-question aggregation
        qstats = {qid: {"attempted": 0, "awarded": 0, "possible": 0,
                        "full": 0, "zero": 0, "picks": {}} for qid in by_id}

        scores, durations = [], []
        frames_total = rekog_total = flags_total = 0
        integrity = []

        for s in sess_rows:
            auto = {k: int(v) for k, v in (s.get("auto_marks") or {}).items()}
            manual = {k: int(v) for k, v in (s.get("manual_marks") or {}).items()}
            answers = s.get("answers") or {}
            awarded_total = 0

            for qid, q in by_id.items():
                st = qstats[qid]
                given = answers.get(qid)
                has = bool(str(given).strip()) if given is not None else False
                mark = auto.get(qid, manual.get(qid))
                if has:
                    st["attempted"] += 1
                if q.get("type") == "mcq" and given:
                    st["picks"][given] = st["picks"].get(given, 0) + 1
                if mark is not None:
                    st["awarded"] += int(mark)
                    st["possible"] += int(q.get("marks", 1))
                    awarded_total += int(mark)
                    if int(mark) == int(q.get("marks", 1)):
                        st["full"] += 1
                    elif int(mark) == 0:
                        st["zero"] += 1

            scores.append(awarded_total)
            dur = int(s.get("ended_at", 0) or 0) - int(s.get("started_at", 0) or 0)
            if dur > 0:
                durations.append(dur)

            fc = int(s.get("frames_checked", 0) or 0)
            rc = int(s.get("rekognition_calls", 0) or 0)
            fl = int(s.get("flag_count", 0) or 0)
            frames_total += fc
            rekog_total += rc
            flags_total += fl
            integrity.append({
                "flags": fl,
                "score_pct": round(awarded_total / total_marks * 100, 1) if total_marks else 0,
                "peak_risk": float(s.get("overall_risk_score", 0) or 0),
            })

        questions = []
        for i, q in enumerate(qs):
            st = qstats.get(q["id"], {})
            poss = st.get("possible", 0)
            facility = round(st.get("awarded", 0) / poss * 100, 1) if poss else None
            picks = st.get("picks", {})
            questions.append({
                "id": q["id"],
                "n": i + 1,
                "type": q.get("type"),
                "text": q.get("text"),
                "max_marks": int(q.get("marks", 1)),
                "attempted": st.get("attempted", 0),
                "facility": facility,
                "full_marks": st.get("full", 0),
                "zero_marks": st.get("zero", 0),
                "options": [
                    {"id": o["id"], "text": o.get("text"),
                     "count": picks.get(o["id"], 0),
                     "correct": o["id"] == q.get("correct_option_id")}
                    for o in (q.get("options") or [])
                ] if q.get("type") == "mcq" else [],
            })

        # score distribution in 5 bands
        bands = [0, 0, 0, 0, 0]
        for sc in scores:
            pct = (sc / total_marks * 100) if total_marks else 0
            bands[min(int(pct // 20), 4)] += 1

        n = len(scores)
        ordered = sorted(scores)
        median = 0
        if n:
            median = ordered[n // 2] if n % 2 else (ordered[n//2 - 1] + ordered[n//2]) / 2

        # flagged vs clean cohort comparison
        flagged_set = [x["score_pct"] for x in integrity if x["flags"] > 0]
        clean_set = [x["score_pct"] for x in integrity if x["flags"] == 0]

        return respond(200, {
            "exam_title": exam.get("title"),
            "total_marks": total_marks,
            "cohort": {
                "submissions": n,
                "roster_size": len(exam.get("allowed_emails", [])),
                "mean": round(sum(scores) / n, 1) if n else 0,
                "median": median,
                "high": max(scores) if scores else 0,
                "low": min(scores) if scores else 0,
                "pass_count": sum(1 for sc in scores if total_marks and sc / total_marks >= 0.4),
                "avg_minutes": round(sum(durations) / len(durations) / 60, 1) if durations else 0,
                "bands": bands,
            },
            "integrity": {
                "frames_checked": frames_total,
                "rekognition_calls": rekog_total,
                "calls_avoided": frames_total - rekog_total,
                "savings_pct": round((1 - rekog_total / frames_total) * 100, 1) if frames_total else 100.0,
                "cost_actual": round(rekog_total * 0.001, 4),
                "cost_naive": round(frames_total * 0.001, 4),
                "flags_raised": flags_total,
                "sessions_flagged": sum(1 for x in integrity if x["flags"] > 0),
                "flagged_mean_pct": round(sum(flagged_set) / len(flagged_set), 1) if flagged_set else None,
                "clean_mean_pct": round(sum(clean_set) / len(clean_set), 1) if clean_set else None,
            },
            "questions": questions,
        })

    return respond(404, {"error": f"Unknown path: {path}"})
