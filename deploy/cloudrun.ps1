param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,
    [string]$Region = "asia-east1",
    [string]$ModelArmorLocation = "asia-southeast1",
    [string]$Service = "shiftzero-api",
    [string]$DashboardOrigin = "https://shiftzero-command-center.mingjen.chatgpt.site",
    [string]$DemoTokenSecret = "shiftzero-demo-token"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "Google Cloud CLI is required: https://cloud.google.com/sdk/docs/install"
}

gcloud config set project $ProjectId
gcloud services enable run.googleapis.com cloudbuild.googleapis.com aiplatform.googleapis.com secretmanager.googleapis.com firestore.googleapis.com pubsub.googleapis.com cloudtrace.googleapis.com modelarmor.googleapis.com

$runtimeSaName = "shiftzero-runtime"
$runtimeSa = "${runtimeSaName}@${ProjectId}.iam.gserviceaccount.com"
$runtimeExists = gcloud iam service-accounts describe $runtimeSa --project $ProjectId --format="value(email)" 2>$null
if (-not $runtimeExists) {
    gcloud iam service-accounts create $runtimeSaName --display-name="ShiftZero Cloud Run runtime" --project $ProjectId
}

$roles = @("roles/aiplatform.user", "roles/datastore.user", "roles/pubsub.publisher", "roles/cloudtrace.agent", "roles/modelarmor.user", "roles/modelarmor.viewer")
foreach ($role in $roles) {
    gcloud projects add-iam-policy-binding $ProjectId --member="serviceAccount:${runtimeSa}" --role=$role --condition=None --quiet | Out-Null
}

$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
$topicExists = gcloud pubsub topics describe shiftzero-events --project $ProjectId --format="value(name)" 2>$null
$topicProbeExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($topicProbeExit -ne 0) {
    gcloud pubsub topics create shiftzero-events --project $ProjectId | Out-Null
}

$ErrorActionPreference = "SilentlyContinue"
$databaseExists = gcloud firestore databases describe --database="(default)" --project $ProjectId --format="value(name)" 2>$null
$databaseProbeExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($databaseProbeExit -ne 0) {
    gcloud firestore databases create --database="(default)" --location=$Region --type=firestore-native --project $ProjectId --quiet | Out-Null
}

gcloud config set api_endpoint_overrides/modelarmor "https://modelarmor.${ModelArmorLocation}.rep.googleapis.com/" | Out-Null
$ErrorActionPreference = "SilentlyContinue"
$templateExists = gcloud model-armor templates describe shiftzero-ingress --location=$ModelArmorLocation --project $ProjectId --format="value(name)" 2>$null
$templateProbeExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($templateProbeExit -ne 0) {
    gcloud model-armor templates create shiftzero-ingress --location=$ModelArmorLocation --project=$ProjectId --pi-and-jailbreak-filter-settings-enforcement=enabled --pi-and-jailbreak-filter-settings-confidence-level=medium-and-above --template-metadata-log-operations --template-metadata-log-sanitize-operations --quiet | Out-Null
}

$ErrorActionPreference = "SilentlyContinue"
$secretExists = gcloud secrets describe $DemoTokenSecret --project $ProjectId --format="value(name)" 2>$null
$secretProbeExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($secretProbeExit -ne 0) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $token = [Convert]::ToHexString($bytes).ToLowerInvariant()
    $token | gcloud secrets create $DemoTokenSecret --replication-policy="automatic" --data-file=- --project $ProjectId
}

gcloud secrets add-iam-policy-binding $DemoTokenSecret --member="serviceAccount:${runtimeSa}" --role="roles/secretmanager.secretAccessor" --project $ProjectId --quiet | Out-Null

gcloud run deploy $Service `
    --source . `
    --project $ProjectId `
    --region $Region `
    --service-account $runtimeSa `
    --allow-unauthenticated `
    --min-instances 0 `
    --max-instances 3 `
    --memory 1Gi `
    --cpu 1 `
    --concurrency 20 `
    --timeout 60 `
    --set-secrets "SHIFTZERO_DEMO_TOKEN=${DemoTokenSecret}:latest" `
    --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${ProjectId},GOOGLE_CLOUD_LOCATION=global,GOOGLE_CLOUD_REGION=${Region},SHIFTZERO_COMMANDER_MODE=adk,SHIFTZERO_COMMANDER_TIMEOUT_SECONDS=30,SHIFTZERO_GEMINI_MODEL=gemini-3.5-flash,SHIFTZERO_CLOUD_EVIDENCE_ENABLED=true,SHIFTZERO_PUBSUB_TOPIC=shiftzero-events,SHIFTZERO_FIRESTORE_DATABASE=(default),SHIFTZERO_CLOUD_TRACE_ENABLED=true,SHIFTZERO_CONTENT_GUARD_MODE=modelarmor,SHIFTZERO_MODEL_ARMOR_LOCATION=${ModelArmorLocation},SHIFTZERO_MODEL_ARMOR_TEMPLATE=shiftzero-ingress,SHIFTZERO_CORS_ORIGINS=${DashboardOrigin}" `
    --quiet

gcloud run services describe $Service --project $ProjectId --region $Region --format="value(status.url)"
