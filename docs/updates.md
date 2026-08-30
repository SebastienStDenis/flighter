# Updating from the settings page

The Advanced section at the foot of **Settings → Preferences** holds the Version row:
the version the running container was built as, with a refresh icon that asks the
registry whether it holds a newer one. Nothing newer puts a checkmark next to the
version; something newer shows an up arrow with the newer build's number - a button,
once Watchtower is connected under **Settings → Connections → Advanced**, or plain
text without one. The check works with no Watchtower at all; only installing needs it.

The version number is nobody's to bump: the release workflow counts the commits
behind each build (`git rev-list --count`), so `v348` simply means the 348th commit
on `main` and the next push mints the next number on its own. The commit itself is
still the exact identity of a build - it sits under the version's hover, and it is
what the update check actually compares; the number is the name a person reads.

## How it works

Flighter never touches the Docker socket. Three small parts add up to the button:

1. **The image knows its commit and its number.** The release workflow passes
   `GIT_SHA` and the counted `GIT_VERSION` as build arguments, and the Dockerfile
   stamps them into the image as `FLIGHTER_BUILD_SHA` and `FLIGHTER_BUILD_VERSION`.
2. **The registry says which commit `:latest` is.** The app reads the standard
   `org.opencontainers.image.revision` label off the published image (an anonymous
   pull-scope token is enough for a public image), caches the answer for an hour, and
   compares; the refresh icon asks afresh regardless of the cache. The
   `org.opencontainers.image.version` label rides along so a newer build is offered
   under its number rather than its commit. A locally built image has no stamp; the
   row says so instead of guessing.
3. **Watchtower does the swap.** The Update button POSTs to Watchtower's HTTP API
   (`/v1/update`, scoped to the Flighter image), and Watchtower pulls the image and
   recreates the container. Because Watchtower answers only after the work is done,
   the request dying without an answer is the update *succeeding* - the settings page
   treats it that way and polls until the new process is up, then reloads itself.

## Setting it up

The app has to be running a published image for any of this to apply: a stack that builds
its own, as the root `docker-compose.yml` does, has nothing to compare against and says so
instead. `examples/tailscale/` runs the published image with Watchtower already wired.

Run Watchtower in the same compose stack with its HTTP API on - the commented block in
the root `docker-compose.yml` is a working example:

```yaml
watchtower:
  # The maintained fork (nicholas-fedor/watchtower); the archived
  # containrrr/watchtower speaks the same API and works too.
  image: nickfedor/watchtower
  restart: unless-stopped
  environment:
    # The fork's own spelling is WATCHTOWER_HTTP_API_ENDPOINTS=update; this older
    # one is understood by both it and the original.
    - WATCHTOWER_HTTP_API_UPDATE=true
    - WATCHTOWER_HTTP_API_TOKEN=choose-a-long-random-string
    # Keep the scheduled sweep as well as the button.
    - WATCHTOWER_HTTP_API_PERIODIC_POLLS=true
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
```

Both implementations are spoken to the same way: `POST /v1/update` with the bearer
token, scoped to the Flighter image. The fork routes that endpoint for POST alone, so
the connection check knocks with POST too (and a deliberately wrong token, which
either implementation turns away at the door without updating anything).

Then, under **Settings → Connections → Advanced → Watchtower**, enter:

- **API address**: `http://watchtower:8080` (the compose service name; the port never
  needs publishing on the host, the app reaches it over the compose network).
- **API token**: the value of `WATCHTOWER_HTTP_API_TOKEN`.

Saving probes the address before keeping it. The token itself can only be fully proven
by running an update, unless `WATCHTOWER_HTTP_API_METRICS=true` is also set - the
metrics endpoint shares the API's token check, so with it on, saving verifies the token
outright.

If Watchtower minds other containers on the machine, the button still only updates
Flighter: the request names the image. The scheduled sweep keeps whatever scope you
gave it.
