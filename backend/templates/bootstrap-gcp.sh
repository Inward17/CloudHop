#!/usr/bin/env bash
# Cloudhop access for one GitHub repo. Run in Google Cloud Shell, after reading it.
# Grants: GitHub Actions in that repo can act as cloudhop-deploy. No long-lived keys are created.
set -euo pipefail
PROJECT=__PROJECT__
REPO=__REPO__
REGION=__REGION__
SA="cloudhop-deploy@$PROJECT.iam.gserviceaccount.com"
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

gcloud services enable compute.googleapis.com iap.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project "$PROJECT"
gcloud iam service-accounts create cloudhop-deploy --project "$PROJECT"
for role in roles/compute.admin roles/storage.admin roles/iam.serviceAccountAdmin roles/iam.serviceAccountUser roles/iap.tunnelResourceAccessor roles/compute.osAdminLogin; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" --condition=None --quiet >/dev/null
done
gcloud iam workload-identity-pools create cloudhop --project "$PROJECT" --location global
gcloud iam workload-identity-pools providers create-oidc github --project "$PROJECT" --location global \
  --workload-identity-pool cloudhop --issuer-uri https://token.actions.githubusercontent.com \
  --attribute-mapping 'google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.environment=assertion.environment' \
  --attribute-condition "assertion.repository == '$REPO' && assertion.environment == 'cloudhop'"
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/cloudhop/attribute.repository/$REPO"
gcloud storage buckets create "gs://$PROJECT-cloudhop-state" --project "$PROJECT" --location "$REGION" --uniform-bucket-level-access --public-access-prevention

echo "Done. Enter project number $NUMBER in Cloudhop."
