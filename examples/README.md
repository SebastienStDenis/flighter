# Compose examples

Two ways to run the published image. Both keep all state in one `data` volume and are
configured the same way afterwards, on the settings page.

| | What it does | Use it when |
|---|---|---|
| [`default/`](default/) | One container, publishing port 8000 on the host. | The normal install. The host is somewhere you can already reach. |
| [`tailscale/`](tailscale/) | Adds a Tailscale sidecar that terminates TLS; nothing is published on the host. | You want HTTPS - which the phone's offline shell needs - and a device of its own on your tailnet. |

Run `docker compose` from inside whichever directory you pick, so it reads that stack's
`.env`. Each has a `.env.example` to copy.
