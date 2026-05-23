#!/usr/bin/env bash
# SpaceX Calendar Sync — Cloud Run + Cloud Scheduler setup
# Run each section once during initial setup, then use the last block to redeploy.
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
PROJECT_ID="$(gcloud config get-value project)"
REGION="us-central1"
SERVICE_NAME="spacex-calendar-sync"
SA_NAME="spacex-calendar-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_JOB="spacex-sync-daily"
SCHEDULE="0 6 * * *"   # 06:00 UTC every day — adjust to taste

# Set GOOGLE_CALENDAR_ID to the target calendar's ID before running.
# You can find it in Google Calendar > Settings > your calendar > Integrate Calendar.
CALENDAR_ID="${GOOGLE_CALENDAR_ID:?Set GOOGLE_CALENDAR_ID before running this script}"

echo "Project : $PROJECT_ID"
echo "Region  : $REGION"
echo "Calendar: $CALENDAR_ID"
echo ""

# ── 1. Enable required APIs ───────────────────────────────────────────────────
echo "==> Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  calendar-json.googleapis.com \
  --project "$PROJECT_ID"

# ── 2. Create a service account for the Cloud Run service ─────────────────────
echo "==> Creating service account..."
gcloud iam service-accounts create "$SA_NAME" \
  --display-name "SpaceX Calendar Sync" \
  --project "$PROJECT_ID" || echo "Service account already exists, skipping."

# ── 3. Grant the service account permission to invoke Cloud Run ───────────────
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role "roles/run.invoker"

# ── 4. Share the Google Calendar with the service account ─────────────────────
# You MUST do this step manually in the Google Calendar UI:
#
#   a) Open Google Calendar → find your SpaceX calendar → Settings (gear icon)
#   b) "Share with specific people or groups"
#   c) Add: $SA_EMAIL  with role "Make changes to events"
#
# Or use the Calendar API (requires OAuth, so easier via the UI):
echo ""
echo "ACTION REQUIRED: Share the calendar with the service account."
echo "  Calendar ID : $CALENDAR_ID"
echo "  Share with  : $SA_EMAIL"
echo "  Permission  : Make changes to events"
echo ""
read -r -p "Press Enter once you have shared the calendar..."

# ── 5. Deploy the Cloud Run service ───────────────────────────────────────────
echo "==> Deploying Cloud Run service..."
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --set-env-vars "GOOGLE_CALENDAR_ID=${CALENDAR_ID}" \
  --no-allow-unauthenticated \
  --project "$PROJECT_ID"

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" --project "$PROJECT_ID" \
  --format 'value(status.url)')"
echo "Service URL: $SERVICE_URL"

# ── 6. Create a Cloud Scheduler job ───────────────────────────────────────────
echo "==> Creating Cloud Scheduler job (${SCHEDULE})..."
gcloud scheduler jobs create http "$SCHEDULER_JOB" \
  --location "$REGION" \
  --schedule "$SCHEDULE" \
  --uri "${SERVICE_URL}/sync_launches" \
  --http-method GET \
  --oidc-service-account-email "$SA_EMAIL" \
  --project "$PROJECT_ID" || \
gcloud scheduler jobs update http "$SCHEDULER_JOB" \
  --location "$REGION" \
  --schedule "$SCHEDULE" \
  --uri "${SERVICE_URL}/sync_launches" \
  --http-method GET \
  --oidc-service-account-email "$SA_EMAIL" \
  --project "$PROJECT_ID"

echo ""
echo "Done! To trigger a manual sync run:"
echo "  gcloud scheduler jobs run $SCHEDULER_JOB --location $REGION"
echo ""
echo "Or call the service directly (requires auth token):"
echo "  curl -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" $SERVICE_URL"
