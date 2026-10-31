# Release deployment

A version tag triggers the release workflow. After tests and publication to PyPI and npm both succeed, a serialized deployment job sends the exact tag version to the VPS through a dedicated SSH key restricted to the server's `deploy-tools` command.

The server's `gemini-router` deployment config installs the requested PyPI version, restarts `gemini-router.service`, and checks `http://127.0.0.1:8787/healthz` for that exact version. If the health check fails, `deploy-tools` attempts to restore the previously installed version and reports the deployment job as failed. Temporary registry propagation delays are retried by the server deploy tool.

## GitHub Actions secrets

Configure these repository secrets without committing their values:

- `VPS_HOST` — server address.
- `VPS_DEPLOY_KEY` — private half of the dedicated deploy key. Its public half must be installed on the server with a forced command to `/usr/local/bin/deploy-tools` and forwarding/PTY disabled.
- `VPS_KNOWN_HOSTS` — pinned SSH host-key entry for `VPS_HOST`.
- `NPM_TOKEN` — npm publish token, used by the existing package publication job.

The PyPI job uses Trusted Publishing and the `pypi` GitHub environment.

The deployment job runs only for `v*` tags and waits for both registry publication jobs. A workflow dispatch from a branch does not deploy. The deploy job is serialized so two releases cannot update the service concurrently.
