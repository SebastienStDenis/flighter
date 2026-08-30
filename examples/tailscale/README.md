# Flighter behind a Tailscale sidecar

The root `docker-compose.yml` publishes port 8000 on the host and leaves reaching it to
you. This is the variant that ends up with a real certificate and no published port at
all: a `tailscale` sidecar joins the tailnet as its own device named `flighter`, the app
shares that network namespace, and `tailscale serve` terminates TLS and proxies to the
app on `127.0.0.1:8000`.

The main README describes the simpler version of the same idea - `tailscale serve --bg
8000` run on the host itself. Prefer that one if the host is already on your tailnet and
you do not mind the app answering on the host's own name. Use this one when you want
Flighter to be a separate device with a name and certificate of its own, or when the host
is not on the tailnet.

HTTPS is what makes the phone work: the service worker that paints the offline shell only
runs on a secure origin, so an `http://<host>:8000` icon on the home screen caches
nothing.

## Setup

1. Enable MagicDNS and **HTTPS Certificates** for the tailnet, both on the
   [DNS page of the admin console](https://login.tailscale.com/admin/dns).
2. Create an [auth key](https://login.tailscale.com/admin/settings/keys), then
   `cp .env.example .env` and fill in both values.
3. `docker compose up -d`, from this directory.
4. In the admin console, open the new `flighter` machine and disable key expiry.
   Otherwise the device is logged out in a few months and the stack goes dark.

The app is then at `https://flighter.<your-tailnet>.ts.net`. Set that as the **public base
URL** on the Preferences tab - calendar links, pushes and the widget are all read on the
phone rather than on the machine serving them - and carry on with the settings walkthrough
in the main README.

Nothing is published to the host, so this is reachable from the tailnet and nowhere else.
To also serve it on the LAN, add a `ports:` entry to the `tailscale` service.

## Updating

Watchtower is scoped to the app container, so the Update button under **Settings →
Preferences → Advanced** works exactly as `docs/updates.md` describes. Enter
`http://watchtower:8080` and your `WATCHTOWER_HTTP_API_TOKEN` under **Settings →
Connections → Advanced**. The app resolves that name through the sidecar's namespace,
which is on the same compose network.

The sidecar is deliberately out of scope; update it by hand:

```sh
docker compose pull tailscale && docker compose up -d
```

## Backups

Same as the main README, with the service named `flighter` rather than `app`:

```
0 4 * * * docker compose -f /path/to/examples/tailscale/docker-compose.yml exec -T flighter /app/scripts/backup.sh
```
