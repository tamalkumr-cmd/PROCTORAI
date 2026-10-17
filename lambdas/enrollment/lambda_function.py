import json, base64, time, boto3

REGION = "ap-south-1"
BUCKET = "proctorai-media-tamal"
COLLECTION = "proctorai-faces"
ANGLES = ["front", "left", "down"]

rekognition = boto3.client("rekognition", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
students = boto3.resource("dynamodb", region_name=REGION).Table("Students")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}


def respond(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body)}


def keystroke_baseline(events):
    dwells, flights = [], []
    for i, e in enumerate(events):
        dwells.append(e["up"] - e["down"])
        if i > 0:
            flights.append(e["down"] - events[i - 1]["up"])
    if not dwells:
        return None
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    std = lambda xs, m: (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5 if xs else 0.0
    dm, fm = avg(dwells), avg(flights)
    return {"dwell_mean": dm, "dwell_std": std(dwells, dm),
            "flight_mean": fm, "flight_std": std(flights, fm),
            "sample_count": len(dwells)}


def decode(img):
    return base64.b64decode(str(img).split(",")[-1])


def lambda_handler(event, context):
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        student_id = claims["sub"]
        if "students" not in str(claims.get("cognito:groups", "")):
            return respond(403, {"error": "Not a student account"})
    except KeyError:
        return respond(401, {"error": "Missing or invalid token"})

    try:
        body = json.loads(event["body"])
        images = body.get("images") or ([body["image"]] if body.get("image") else [])
        keystrokes = body.get("keystrokes", [])
    except Exception as e:
        return respond(400, {"error": f"Bad request body: {e}"})

    if not images:
        return respond(400, {"error": "Send at least one photo"})
    images = images[:len(ANGLES)]

    # remove faces from any earlier enrolment so matches stay clean
    old = students.get_item(Key={"student_id": student_id}).get("Item") or {}
    old_ids = list(old.get("registered_face_ids") or [])
    if old.get("registered_face_id") and old["registered_face_id"] not in old_ids:
        old_ids.append(old["registered_face_id"])
    if old_ids:
        try:
            rekognition.delete_faces(CollectionId=COLLECTION, FaceIds=old_ids)
        except Exception as e:
            print(f"delete_faces failed: {e}")

    results, face_ids, image_keys = [], [], []
    for i, img in enumerate(images):
        angle = ANGLES[i]
        raw = decode(img)
        try:
            res = rekognition.index_faces(
                CollectionId=COLLECTION,
                Image={"Bytes": raw},
                ExternalImageId=student_id,
                MaxFaces=1,
                QualityFilter="AUTO",
            )
        except Exception as e:
            results.append({"angle": angle, "ok": False, "reason": str(e)})
            continue

        recs = res.get("FaceRecords", [])
        if not recs:
            results.append({"angle": angle, "ok": False,
                            "reason": "Face not clear enough — try better light"})
            continue

        face = recs[0]["Face"]
        face_ids.append(face["FaceId"])
        key = f"baselines/{student_id}/reference_{angle}.jpg"
        s3.put_object(Bucket=BUCKET, Key=key, Body=raw, ContentType="image/jpeg")
        image_keys.append(key)
        results.append({"angle": angle, "ok": True,
                        "confidence": round(face["Confidence"], 2)})

    if not face_ids:
        return respond(422, {"error": "None of the photos had a usable face. Try again facing a light source.",
                             "angles": results})
    if not results[0]["ok"]:
        # the straight-on photo is the one that matters most
        rekognition.delete_faces(CollectionId=COLLECTION, FaceIds=face_ids)
        return respond(422, {"error": "The straight-on photo wasn't usable. Retake it facing the camera.",
                             "angles": results})

    baseline = keystroke_baseline(keystrokes)
    baseline_key = None
    if baseline:
        baseline_key = f"baselines/{student_id}/keystroke.json"
        s3.put_object(Bucket=BUCKET, Key=baseline_key, Body=json.dumps(baseline),
                      ContentType="application/json")

    students.update_item(
        Key={"student_id": student_id},
        UpdateExpression=("SET enrolled = :e, registered_face_id = :f, registered_face_ids = :fs, "
                          "reference_image_refs = :i, keystroke_baseline_ref = :k, enrolled_at = :t"),
        ExpressionAttributeValues={
            ":e": True, ":f": face_ids[0], ":fs": face_ids, ":i": image_keys,
            ":k": baseline_key, ":t": int(time.time()),
        },
    )

    return respond(200, {
        "enrolled": True,
        "face_id": face_ids[0],
        "references": len(face_ids),
        "angles": results,
        "confidence": results[0].get("confidence"),
        "keystroke_samples": baseline["sample_count"] if baseline else 0,
    })
