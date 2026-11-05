# Running the agent on Google Cloud

The agent's data lives on your Mac (Project Tracker, Focus Monitor, Sleep Bandit all bind to localhost
there), so the cloud agent reaches the Mac through a small **bridge** over **Tailscale**: a private
WireGuard network between the VM and the Mac; nothing on the Mac is exposed to the internet.

```
 iPhone / laptop ──Telegram──▶ Telegram API ◀──long-poll── ┐
 iPhone Shortcut ──HTTPS────▶ Tailscale Funnel ──────────▶ │ GCP VM: docker `ea` (python -m ea serve)
 phone call ───── Twilio ────▶ Tailscale Funnel ──────────▶ │   memory + transcripts + state in ./data
                                                           └──Tailscale──▶ Mac: bridge :8799 ──▶ :8791 / :8790 / :8777
```

## One-time setup

1. **Tailscale** on the Mac (App Store) and sign in. Enable **Funnel** for your tailnet in the admin
   console (Settings → Funnel) and **MagicDNS/HTTPS**.
2. **Bridge on the Mac** (from this repo, on the Mac):
   ```bash
   ./bridge/install_bridge.sh          # prints the bridge token; keep it
   tailscale ip -4                     # the Mac's Tailscale IP, e.g. 100.101.102.103
   ```
3. **VM**: `PROJECT=<gcp-project> ./deploy/gcp/create_vm.sh`, then
   `gcloud compute ssh ea-agent --tunnel-through-iap`, copy the repo to `~/ea/executive-assistant-agent`
   (`git clone` from your private remote, or `gcloud compute scp --recurse`), and run
   `./deploy/gcp/bootstrap.sh`. It stops twice to let you fill in `data/.env` and `config.toml`; set
   `sources.bridge_url = "http://100.101.102.103:8799"` and `EA_BRIDGE_TOKEN` to the printed token.
4. **Public URL**: `tailscale funnel status` on the VM prints `https://ea-agent.<tailnet>.ts.net`. Put it in
   `channels.public_base_url`, use it in the iPhone Shortcut, and as Twilio's voice webhook
   (`https://…/twilio/voice`, HTTP POST).
5. `sudo docker compose -f deploy/docker-compose.yml restart ea` after config changes;
   `sudo docker logs -f ea` to watch.

Why this shape: the VM accepts no inbound traffic from the internet (firewall denies all, SSH via IAP
only); the only public entry is Tailscale's Funnel, which terminates TLS and forwards to the agent;
the agent checks a bearer token (Shortcut) or Twilio's request signature (calls); the Mac accepts only
tailnet traffic and only through the bridge's allow-list. Secrets live in `data/.env` (mode 600) on the
VM. If you prefer Google Secret Manager, store the same lines there and write them into `data/.env` at
boot in `bootstrap.sh`.

Cost: an e2-small is ≈ $13/month; the model is the real cost (Opus 5.5: $4 / $20 per million tokens,
cache reads $0.20). A day of chatting plus three scheduled checks typically runs a few dollars.

## Moving to the Mac mini later

Same repo, same `data/` folder (copy it), `sources.bridge_url = ""` so the connectors talk to localhost,
and either `deploy/macmini/install.sh` (launchd, no Docker) or the same docker-compose with
`tracker_url = "http://host.docker.internal:8791"` etc. The Funnel/Twilio/Shortcut part moves with
Tailscale: run `tailscale funnel --bg 8800` on the Mac mini instead and update `public_base_url`.

## Swapping the model for a local open-weight one

On the Mac mini with Ollama: `ollama pull qwen3:32b` (any model with tool calling), then in `config.toml`
`provider = "openai_compat"`, `model = "qwen3:32b"`, `openai_base_url = "http://127.0.0.1:11434/v1"`.
Nothing else changes: memory, tools, channels and transcripts are provider-neutral. Expect weaker tool
use than Opus; keep `max_tool_rounds` modest and watch the logs the first day.
