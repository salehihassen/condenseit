# Pre-built Docker image

This fork builds the Python backend and Preact frontend in GitHub Actions and
publishes them to `ghcr.io/salehihassen/condenseit`. The container serves the web
UI on port **8899**. Digest runs and Ollama stay on the host, as described in
[deploy-local.md](deploy-local.md).

## Build and publish in GitHub Actions

The [publish workflow](../.github/workflows/docker-publish.yml) runs on every
push to `saleh-changes`, on published releases, and on manual dispatch. The
image namespace comes from the workflow repository, so a fork publishes under
its own owner rather than the upstream owner.

Each push to `saleh-changes` publishes:

- `ghcr.io/salehihassen/condenseit:latest`, the head of `saleh-changes`.
- `ghcr.io/salehihassen/condenseit:sha-<short-commit-sha>`, an immutable tag for
  that commit; pin it to hold a version or roll back.

Only `linux/amd64` is built. Releases tagged `vX.Y.Z` also publish `X.Y.Z`
and `X.Y` but never move `latest`.

The workflow logs into GHCR with its built-in `GITHUB_TOKEN` and
`packages: write`. No PAT or Docker Hub secrets are required in GitHub Actions.
Your machine only needs permission to pull images.

Before the first push, open the fork's **Actions** tab and enable workflows if
GitHub has disabled them for the fork. Then commit and push these changes:

```bash
git add .github/workflows/docker-publish.yml docker-compose.yml README.md docs/docker-image.md
git commit -m "Publish fork branch images to GHCR"
git push origin saleh-changes
```

Watch **Publish Docker image** finish before pulling. For
later manual builds, choose **Run workflow** and select the desired branch;
GitHub requires the workflow file to exist on the default branch before it
offers manual dispatch. A push to `saleh-changes` does not need that setup.

If a package with this name already exists from a previous manual push, grant
the fork write access under the package's **Settings → Manage Actions access**.
Packages created by this workflow are linked to the fork automatically.

## Pull and run with a read-only PAT

For a private image, use your existing GitHub personal access token **(classic)**
with `read:packages` and an account that can read the package. Keep the package
private if you want authenticated pulls. Public images support anonymous pulls.

```bash
# Skip this if Docker is already logged into GHCR with your read-only PAT.
# Paste the PAT at the password prompt.
docker login ghcr.io -u salehihassen

docker compose pull
docker compose up -d --no-build
```

Compose defaults to `ghcr.io/salehihassen/condenseit:latest`. Each time
you push code and the workflow finishes, repeat the two Compose commands to
update the running UI. `--no-build` keeps image builds in GitHub Actions.

For a fresh checkout, first copy `config.example.yaml` to `config.yaml` and
`.env.example` to `.env`, and edit those files for your environment. Compose
mounts `config.yaml` and `./data/` from the host.

Open [http://localhost:8899](http://localhost:8899). Stop with
`docker compose down`.

## Pin or override the image

[`docker-compose.yml`](../docker-compose.yml) accepts these environment
variables, which can also be saved in `.env`:

```bash
# Pin one workflow build; replace <short-commit-sha> with the actual commit.
export CONDENSEIT_IMAGE_TAG='sha-<short-commit-sha>'

# Or override the complete registry/name/tag, including upstream images.
export CONDENSEIT_IMAGE=ghcr.io/wildlifechorus/condenseit:2.7.5
```

`CONDENSEIT_IMAGE` takes precedence over `CONDENSEIT_IMAGE_TAG`. Remove an old
override to use this fork's `latest` image. Local builds remain available
with `docker compose up -d --build` when explicitly requested.

See [GitHub's Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
for token scopes and package access settings.
