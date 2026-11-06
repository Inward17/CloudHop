#!/usr/bin/env bash
# Cloudhop access for __REPO__. Run in Google Cloud Shell, after reading it.
# Grants: CI jobs for that repo (and only those) can act as ch-__NAME__. No long-lived keys are created.
set -euo pipefail
PROJECT=__PROJECT__
REGION=__REGION__
POOL=ch-__NAME__
SA="ch-__NAME__@$PROJECT.iam.gserviceaccount.com"
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

gcloud services enable compute.googleapis.com iap.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project "$PROJECT"
gcloud iam service-accounts create "ch-__NAME__" --project "$PROJECT" --display-name "Cloudhop __REPO__"
for role in roles/compute.admin roles/storage.admin roles/iam.serviceAccountAdmin roles/iam.serviceAccountUser roles/iap.tunnelResourceAccessor roles/compute.osAdminLogin; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" --condition=None --quiet >/dev/null
done

# One pool per project. The condition admits only tokens for this repo's CI identity.
gcloud iam workload-identity-pools create "$POOL" --project "$PROJECT" --location global --display-name "Cloudhop __NAME__"
AUDIENCE="__GCP_AUDIENCE__"
gcloud iam workload-identity-pools providers create-oidc ci --project "$PROJECT" --location global \
  --workload-identity-pool "$POOL" --issuer-uri "__GCP_ISSUER__" \
  ${AUDIENCE:+--allowed-audiences "$AUDIENCE"} \
  --attribute-mapping 'google.subject=assertion.sub' \
  --attribute-condition "__GCP_CONDITION__"
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/*"

if ! gcloud storage buckets describe "gs://$PROJECT-cloudhop-state" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$PROJECT-cloudhop-state" --project "$PROJECT" --location "$REGION" --uniform-bucket-level-access --public-access-prevention
fi

echo "Done. Enter project number $NUMBER in Cloudhop."
