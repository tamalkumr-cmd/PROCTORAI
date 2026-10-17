#!/bin/bash
set -e
REGION=ap-south-1
BUCKET=proctorai-media-tamal

echo "Creating DynamoDB tables..."

aws dynamodb create-table --table-name Students \
  --attribute-definitions AttributeName=student_id,AttributeType=S \
  --key-schema AttributeName=student_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region $REGION

aws dynamodb create-table --table-name Exams \
  --attribute-definitions AttributeName=exam_id,AttributeType=S \
  --key-schema AttributeName=exam_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region $REGION

aws dynamodb create-table --table-name ExamSessions \
  --attribute-definitions AttributeName=session_id,AttributeType=S \
  --key-schema AttributeName=session_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region $REGION

aws dynamodb create-table --table-name FlaggedEvents \
  --attribute-definitions AttributeName=session_id,AttributeType=S \
                          AttributeName=timestamp,AttributeType=N \
  --key-schema AttributeName=session_id,KeyType=HASH \
               AttributeName=timestamp,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST --region $REGION

aws dynamodb create-table --table-name RiskScores \
  --attribute-definitions AttributeName=session_id,AttributeType=S \
                          AttributeName=timestamp,AttributeType=N \
  --key-schema AttributeName=session_id,KeyType=HASH \
               AttributeName=timestamp,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST --region $REGION

echo "Creating media bucket..."

aws s3api create-bucket --bucket $BUCKET --region $REGION \
  --create-bucket-configuration LocationConstraint=$REGION

aws s3api put-public-access-block --bucket $BUCKET \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

echo "Creating prefixes..."
aws s3api put-object --bucket $BUCKET --key staging/
aws s3api put-object --bucket $BUCKET --key flagged/
aws s3api put-object --bucket $BUCKET --key baselines/

echo "Applying lifecycle rule..."
cat > /tmp/lifecycle.json <<'EOF'
{
  "Rules": [
    {
      "ID": "expire-staging-1day",
      "Status": "Enabled",
      "Filter": { "Prefix": "staging/" },
      "Expiration": { "Days": 1 }
    }
  ]
}
EOF
aws s3api put-bucket-lifecycle-configuration --bucket $BUCKET \
  --lifecycle-configuration file:///tmp/lifecycle.json

echo "Applying CORS..."
cat > /tmp/cors.json <<'EOF'
{
  "CORSRules": [
    {
      "AllowedHeaders": ["*"],
      "AllowedMethods": ["GET", "PUT", "POST"],
      "AllowedOrigins": ["http://localhost:3000"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    }
  ]
}
EOF
aws s3api put-bucket-cors --bucket $BUCKET --cors-configuration file:///tmp/cors.json

echo "Done."
