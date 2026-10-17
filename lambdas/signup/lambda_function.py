import boto3, time, os

REGION = "ap-south-1"
STUDENT_GROUP = "students"
PROCTOR_GROUP = "proctors"

# Anyone signing up with an email on this list becomes a proctor.
# Everything else becomes a student.
PROCTOR_EMAILS = {
    "kumbhakartamal@gmail.com",
}
PROCTOR_DOMAINS = set()   # e.g. {"staff.vit.ac.in"}

idp = boto3.client("cognito-idp", region_name=REGION)
students = boto3.resource("dynamodb", region_name=REGION).Table("Students")


def lambda_handler(event, context):
    """Cognito post-confirmation trigger: assign a group and create the student record."""
    attrs = event.get("request", {}).get("userAttributes", {})
    email = (attrs.get("email") or "").strip().lower()
    sub = attrs.get("sub")
    name = attrs.get("name") or email.split("@")[0]
    pool = event["userPoolId"]
    username = event["userName"]

    domain = email.split("@")[-1] if "@" in email else ""
    is_proctor = email in PROCTOR_EMAILS or domain in PROCTOR_DOMAINS
    group = PROCTOR_GROUP if is_proctor else STUDENT_GROUP

    try:
        idp.admin_add_user_to_group(
            UserPoolId=pool, Username=username, GroupName=group
        )
    except Exception as e:
        print(f"Group assignment failed for {email}: {e}")

    if not is_proctor and sub:
        try:
            students.put_item(
                Item={
                    "student_id": sub,
                    "name": name,
                    "email": email,
                    "enrolled": False,
                    "registered_face_id": None,
                    "keystroke_baseline_ref": None,
                    "created_at": int(time.time()),
                },
                ConditionExpression="attribute_not_exists(student_id)",
            )
        except idp.exceptions.ClientError:
            pass
        except Exception as e:
            print(f"Student record failed for {email}: {e}")

    return event
