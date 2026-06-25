# Upgrading

Précis-MCP ships as a rolling `main`: the public repository advances by sync
commits from the internal repository, each sync is tagged, and
`CHANGELOG.md` in the repository root answers the only question that matters
before you upgrade — **does this sync break my compose stack, my `instance/`
files, or my client integration?** Read it first; everything below assumes
you have.

## Multi-user bundle

1. **Back up first.**

    !!! warning "There are no reverse migrations"
        Rolling back an upgrade means restoring the pre-upgrade bundle. You
        have a one-command chain — [`backup run`](backups.md). An upgrade
        you can't roll back from is a bet, not a procedure.
2. **Take the new version.** Two paths, matching how you deploy:
   - *Pinned release (the default):* set `PRECIS_MCP_TAG` to the new version and
     re-pull (see [Pinned-release mode](#pinned-release-mode-pull-instead-of-build)
     below), or `scripts/deploy-mcp.sh --tag <version>` from your workstation —
     it pulls the published image, no rebuild.
   - *Rolling `main` / fork:* `git pull` on the deployment checkout, or
     `scripts/deploy-mcp.sh --build` from your workstation (it rsyncs the tree
     to the box and builds there).
3. **Bring the stack up** (the rolling-`main` / build path; the pinned-release
   path uses `up -d` without `--build`):

   ```bash
   docker compose -f deploy/docker-compose.yml up -d --build
   ```

   This does the upgrade in the right order on its own: the `migrate`
   service applies any new numbered Postgres migrations and must complete
   before the server (and the daemon/backup sidecars) start; services
   whose image changed are recreated, which is also how the
   scheduler/watcher/backup sidecars pick up new code.
4. **If the changelog entry touches `instance/` shapes** (new DDL, semantic
   conventions), re-run the idempotent provisioner:

   ```bash
   docker compose -f deploy/docker-compose.yml exec precis-mcp \
     python -m precis_mcp.clickhouse_init --scope open
   ```

5. **Verify:** `GET /health` returns ok; `python -m precis_mcp.admin_cli
   check-auth` passes; `clickhouse_init --scope open --check` passes; one
   known metric returns the expected number through a client.

## Single-user (quickstart) stack

```bash
git pull
MCP_DEV_KEY=$MCP_DEV_KEY \
  docker compose -f deploy/docker-compose.local.yml up -d --build
```

Re-run the provisioner (quickstart step 2) only if the changelog says the
sync touches `instance/` shapes.

## Pinned-release mode (pull instead of build)

The steps above build the app image from the synced source — the right choice
when you track rolling `main`. To run a **pinned, pre-built release** instead,
set `PRECIS_MCP_TAG` to a published image version and bring the stack up
*without* `--build`:

```bash
PRECIS_MCP_TAG=0.2.2 docker compose -f deploy/docker-compose.yml up -d
```

Compose pulls `ghcr.io/precis-finance/precis-mcp:<tag>` (falling back to a
source build only if the tag is absent). Upgrading is then bumping
`PRECIS_MCP_TAG` to a newer release and re-running `up -d` — no rebuild. The
bundled Postgres / ClickHouse / Keycloak images are unaffected either way; they
stay digest-pinned in the compose file. Still read `CHANGELOG.md` first, and
back up (step 1) — a tag bump is an upgrade like any other.

## Rolling back

There are no reverse migrations. Rolling back is a **restore**: check out
the previous tag, rebuild, then restore the pre-upgrade bundle —
[Backups & restore](backups.md). This is the reason step 1 isn't optional.

## Dependency and image pinning

What a sync ships is exactly what runs: Python dependencies install from a
hashed lockfile (`requirements.lock`), and the bundled Postgres / ClickHouse
/ Keycloak images are digest-pinned (`tag@sha256:…`) in the compose files,
so an upstream re-tag can't change your stack silently. The precis-mcp
application image follows the same principle — published to
`ghcr.io/precis-finance/precis-mcp` and selected by `PRECIS_MCP_TAG` (a release
version by default, pinnable to a `@sha256:` digest), or built from the synced
source when you run `up --build`. The flip side is
that base-image and dependency security fixes do **not** arrive by
re-pulling — they arrive as pin bumps in sync commits (CI gates the pin set
with `pip-audit` and a trivy image scan). If you maintain a fork, regenerate
the lockfile with `make lock` and refresh image digests with
`docker buildx imagetools inspect <tag>` on your own cadence.

## Not an upgrade

Editing your own `instance/` files isn't an upgrade and needs no rebuild:
catalogue/semantic changes need the provisioner + a server restart
([modelling contract](../configuration/adding-metrics-and-dimensions.md)),
and `instance/integrations/` changes need the daemons restarted
([ingestion](../configuration/ingestion.md#scheduling)).

## Related

- [Backups & restore](backups.md) — the rollback mechanism.
- [Troubleshooting](troubleshooting.md) — if the post-upgrade verify fails.
