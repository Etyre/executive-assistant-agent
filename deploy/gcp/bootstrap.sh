#!/usr/bin/env bash
# On the VM: install Docker + Tailscale, clone/copy the repo, start the agent. Idempotent.
set -euo pipefail
sudo apt-get update -y && sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" || true
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh            # prints a login URL; approve the machine in your tailnet
mkdir -p ~/ea && cd ~/ea
if [ ! -d executive-assistant-agent ]; then
  echo "copy the repo here (git clone <your-remote> executive-assistant-agent, or scp/rsync the folder), then re-run"; exit 1
fi
cd executive-assistant-agent
mkdir -p data && chmod 700 data
[ -f data/.env ] || { cp .env.example data/.env; echo "fill in data/.env then re-run"; exit 1; }
[ -f config.toml ] || { cp config.example.toml config.toml; echo "edit config.toml (sources.bridge_url = Tailscale IP of the Mac + :8799) then re-run"; exit 1; }
chmod 600 data/.env
sudo docker compose -f deploy/docker-compose.yml up -d --build
# Expose only the HTTPS webhook paths to the internet through Tailscale Funnel (Twilio + iPhone Shortcut).
sudo tailscale funnel --bg 8800 || echo "enable Funnel for this tailnet in the Tailscale admin console, then: sudo tailscale funnel --bg 8800"
echo "public URL (put in config.toml channels.public_base_url):"; tailscale funnel status 2>/dev/null | head -5
