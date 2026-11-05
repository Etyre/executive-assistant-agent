#!/usr/bin/env bash
# Creates a small Compute Engine VM for the agent. Run from your laptop with gcloud authenticated.
#   PROJECT=my-gcp-project ZONE=us-west1-b ./deploy/gcp/create_vm.sh
set -euo pipefail
PROJECT="${PROJECT:?set PROJECT}"; ZONE="${ZONE:-us-west1-b}"; NAME="${NAME:-ea-agent}"
gcloud compute instances create "$NAME" --project "$PROJECT" --zone "$ZONE" \
  --machine-type e2-small --image-family debian-12 --image-project debian-cloud \
  --boot-disk-size 20GB --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
  --tags ea-agent --metadata enable-oslogin=TRUE
# Only Tailscale traffic and nothing else from the internet; Twilio/Shortcut reach us through a Tailscale Funnel
# (or Cloudflare Tunnel), so no inbound port is opened here. Remove the default-allow rules if your project has them.
gcloud compute firewall-rules create ea-agent-deny-ingress --project "$PROJECT" --direction INGRESS --action DENY \
  --rules all --target-tags ea-agent --priority 1000 --source-ranges 0.0.0.0/0 2>/dev/null || true
gcloud compute firewall-rules create ea-agent-allow-iap-ssh --project "$PROJECT" --direction INGRESS --action ALLOW \
  --rules tcp:22 --target-tags ea-agent --priority 900 --source-ranges 35.235.240.0/20 2>/dev/null || true
echo "ssh with: gcloud compute ssh $NAME --project $PROJECT --zone $ZONE --tunnel-through-iap"
echo "then run deploy/gcp/bootstrap.sh on the VM"
