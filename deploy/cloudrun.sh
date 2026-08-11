#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${1:?usage: ./deploy/cloudrun.sh PROJECT_ID [REGION]}"
REGION="${2:-asia-east1}"
MODEL_ARMOR_LOCATION="${SHIFTZERO_MODEL_ARMOR_LOCATION:-asia-southeast1}"
SERVICE="${SHIFTZERO_SERVICE:-shiftzero-api}"
DASHBOARD_ORIGIN="${SHIFTZERO_DASHBOARD_ORIGIN:-https://shiftzero-command-center.mingjen.chatgpt.site}"
DEMO_TOKEN_SECRET="${SHIFTZERO_DEMO_TOKEN_SECRET:-shiftzero-demo-token}"
RUNTIME_SA_NAME="${SHIFTZERO_RUNTIME_SA_NAME:-shiftzero-runtime}"

gcloud config set project "$PROJECT_ID"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  pubsub.googleapis.com \
  cloudtrace.googleapis.com \
  modelarmor.googleapis.com \
  --project "$PROJECT_ID"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name="ShiftZero Cloud Run runtime" \
    --project "$PROJECT_ID"
fi

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/aiplatform.user" \
  --condition=None \
  --quiet >/dev/null

for role in roles/datastore.user roles/pubsub.publisher roles/cloudtrace.agent roles/modelarmor.user roles/modelarmor.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
done

if ! gcloud pubsub topics describe shiftzero-events --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud pubsub topics create shiftzero-events --project "$PROJECT_ID" >/dev/null
fi

if ! gcloud firestore databases describe --database='(default)' --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --database='(default)' \
    --location="$REGION" \
    --type=firestore-native \
    --project "$PROJECT_ID" \
    --quiet >/dev/null
fi

gcloud config set api_endpoint_overrides/modelarmor "https://modelarmor.${MODEL_ARMOR_LOCATION}.rep.googleapis.com/" >/dev/null
if ! gcloud model-armor templates describe shiftzero-ingress --location="$MODEL_ARMOR_LOCATION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud model-armor templates create shiftzero-ingress \
    --location="$MODEL_ARMOR_LOCATION" \
    --project="$PROJECT_ID" \
    --pi-and-jailbreak-filter-settings-enforcement=enabled \
    --pi-and-jailbreak-filter-settings-confidence-level=medium-and-above \
    --template-metadata-log-operations \
    --template-metadata-log-sanitize-operations \
    --quiet >/dev/null
fi

if ! gcloud secrets describe "$DEMO_TOKEN_SECRET" --project "$PROJECT_ID" >/dev/null 2>&1; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' | \
    gcloud secrets create "$DEMO_TOKEN_SECRET" \
      --replication-policy="automatic" \
      --data-file=- \
      --project "$PROJECT_ID"
fi

gcloud secrets add-iam-policy-binding "$DEMO_TOKEN_SECRET" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "$PROJECT_ID" \
  --quiet >/dev/null

gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --platform managed \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 3 \
  --memory 1Gi \
  --cpu 1 \
  --concurrency 20 \
  --timeout 60 \
  --set-secrets="SHIFTZERO_DEMO_TOKEN=${DEMO_TOKEN_SECRET}:latest" \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_CLOUD_REGION=${REGION},SHIFTZERO_COMMANDER_MODE=adk,SHIFTZERO_COMMANDER_TIMEOUT_SECONDS=30,SHIFTZERO_GEMINI_MODEL=gemini-3.5-flash,SHIFTZERO_CLOUD_EVIDENCE_ENABLED=true,SHIFTZERO_PUBSUB_TOPIC=shiftzero-events,SHIFTZERO_FIRESTORE_DATABASE=(default),SHIFTZERO_CLOUD_TRACE_ENABLED=true,SHIFTZERO_CONTENT_GUARD_MODE=modelarmor,SHIFTZERO_MODEL_ARMOR_LOCATION=${MODEL_ARMOR_LOCATION},SHIFTZERO_MODEL_ARMOR_TEMPLATE=shiftzero-ingress,SHIFTZERO_CORS_ORIGINS=${DASHBOARD_ORIGIN}" \
  --quiet

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format='value(status.url)')"

printf 'SHIFTZERO_SERVICE_URL=%s\n' "$SERVICE_URL"
curl --fail --silent --show-error "$SERVICE_URL/health"
printf '\n'
