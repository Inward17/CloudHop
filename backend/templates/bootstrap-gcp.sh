#!/usr/bin/env bash
# Cloudhop access for __REPO__. Run in Google Cloud Shell, after reading it.
# Grants: CI jobs for that repo (and only those) can act as ch-__NAME__. No long-lived keys are created.
set -euo pipefail
PROJECT=__PROJECT__
REGION=__REGION__
POOL=ch-__NAME__
SA="ch-__NAME__@$PROJECT.iam.gserviceaccount.com"
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

gcloud services enable compute.googleapis.com iap.googleapis.com iamcredentials.googleapis.com sts.googleapis.com cloudresourcemanager.googleapis.com --project "$PROJECT"
gcloud iam service-accounts create "ch-__NAME__" --project "$PROJECT" --display-name "Cloudhop __REPO__"
# Project-wide: create and manage VMs, firewalls and addresses, and open IAP tunnels. Compute Admin still
# reaches every VM in the project, so use a project that holds only Cloudhop apps.
for role in roles/compute.admin roles/iap.tunnelResourceAccessor; do
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

bucket() {
  if ! gcloud storage buckets describe "gs://$1" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://$1" --project "$PROJECT" --location "$REGION" --uniform-bucket-level-access --public-access-prevention
  fi
}
# State bucket (shared by this project's Cloudhop apps) and this app's data bucket. Access is per bucket.
bucket "$PROJECT-cloudhop-state"
DATA="cloudhop-__NAME__-$(printf %s "$PROJECT" | md5sum | cut -c1-8)"
bucket "$DATA"
gcloud storage buckets add-iam-policy-binding "gs://$PROJECT-cloudhop-state" --member "serviceAccount:$SA" --role roles/storage.objectAdmin --quiet >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$DATA" --member "serviceAccount:$SA" --role roles/storage.admin --quiet >/dev/null

# The server's own account: Cloudhop may run VMs as this account only, and it can use only the data bucket.
VM_SA="ch-__NAME__-vm@$PROJECT.iam.gserviceaccount.com"
gcloud iam service-accounts create "ch-__NAME__-vm" --project "$PROJECT" --display-name "Cloudhop __REPO__ server"
gcloud iam service-accounts add-iam-policy-binding "$VM_SA" --project "$PROJECT" --member "serviceAccount:$SA" --role roles/iam.serviceAccountUser --quiet >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$DATA" --member "serviceAccount:$VM_SA" --role roles/storage.objectAdmin --quiet >/dev/null

echo "Done. Enter project number $NUMBER in Cloudhop."
