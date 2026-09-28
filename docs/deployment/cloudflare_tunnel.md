# Cloudflare Tunnel Setup (any OS)

The Prawn Hunter can expose its **honeypot receiver** (the `/honeypot/{path}`
router inside the `api` container) on a public HTTPS hostname **without opening
any inbound port and without revealing the host IP**. Attackers hit
Cloudflare's edge; Cloudflare forwards traffic down an outbound-only tunnel to
`api:8001`.

This is **optional** — it is only needed when `HONEYPOT_MODE=True`. The core
scanning / validation / broadcast pipeline does not require a tunnel.

Two ways to run it. **Use Option A** (Docker service) for a portable,
one-command setup that works identically on macOS, Linux, and Windows.

---

## Option A — `cloudflared` as a Docker service (recommended, any OS)

The stack ships a `cloudflared` service behind the `honeypot` profile. It uses a
**remotely-managed (token) tunnel**: all routing lives in the Cloudflare
dashboard and the container only needs a token — no interactive login, no
host-level service install.

### 1. Create the tunnel (one-time, in the dashboard)

1. Cloudflare **Zero Trust** dashboard → **Networks → Tunnels → Create a tunnel**.
2. Choose **Cloudflared**, name it (e.g. `prawnhunter`), **Save**.
3. On the "Install connector" screen choose **Docker** and copy the **token** —
   the long `eyJ...` string after `--token`. You do **not** run the command it
   shows; the compose service runs it for you.
4. Open the **Public Hostname** tab and add a hostname:
   - **Subdomain / domain**: e.g. `winnethepooh.hong-yi.me` (must be a domain on
     your Cloudflare account).
   - **Service**: **`http://api:8001`** ← the internal compose address, **not**
     `localhost`. `cloudflared` runs on the same Docker network and reaches the
     API by its service name.

### 2. Configure `.env`

```
CLOUDFLARE_TUNNEL_TOKEN=eyJ...            # the connector token from step 1.3
HONEYPOT_MODE=True
HONEYPOT_WEBHOOK_URL=https://winnethepooh.hong-yi.me
HONEYPOT_SECRET=<random 32+ char string>
```

### 3. Start it

```bash
docker compose --profile honeypot up -d --build
docker compose logs -f cloudflared        # look for "Registered tunnel connection"
```

Identical on macOS, Linux, and Windows. To stop just the tunnel:
`docker compose stop cloudflared`.

---

## Option B — `cloudflared` on the host (legacy / Windows)

[`scripts/setup_cloudflare_tunnel.ps1`](../../scripts/setup_cloudflare_tunnel.ps1)
sets up a **locally-managed** tunnel and installs `cloudflared` as a **Windows
service**. It is Windows-only and points the tunnel at `http://localhost:8011`
(the host-published API port). Use it only if you specifically want the tunnel
running outside Docker on a Windows host.

macOS / Linux host equivalent (if you do not want the Docker service):

```bash
# macOS:  brew install cloudflared
# Linux:  install from Cloudflare's apt/rpm repo (pkg.cloudflare.com)
cloudflared tunnel login
cloudflared tunnel create prawnhunter

# ~/.cloudflared/config.yml:
#   tunnel: <tunnel-id>
#   credentials-file: ~/.cloudflared/<tunnel-id>.json
#   ingress:
#     - hostname: winnethepooh.hong-yi.me
#       service: http://localhost:8011
#     - service: http_status:404

cloudflared tunnel route dns prawnhunter winnethepooh.hong-yi.me
cloudflared tunnel run prawnhunter        # or install as a launchd/systemd service
```

---

## Verify end-to-end

```bash
curl -sS https://<your-hostname>/health/          # reaches the API through the tunnel
docker compose logs cloudflared | grep -i connection
```

A `200` from the health endpoint over your public hostname confirms the tunnel
is live and routing to `api:8001`.

WAF hardening for the public hostname:
see [`cloudflare_waf_rules.md`](../cloudflare_waf_rules.md).

---

## Notes

- The tunnel is **outbound-only** — no ports are opened on the host, and the
  origin IP is never exposed. This is also the cleanest mitigation for the
  datacenter-IP concern on a cloud VPS: inbound reaches you via Cloudflare's
  edge regardless of the VPS IP reputation.
- `HONEYPOT_MODE=False` (the default) leaves the receiver disabled; the
  `cloudflared` service simply never starts unless you enable the `honeypot`
  (or `full`) profile.
- Pin the image (`cloudflare/cloudflared:<dated-tag>`) for production instead of
  `:latest`.
