# deploy/

The files that run lexchat-eval beside AILA on its EC2 instance. The install guide for the cloud
team is [`lex_eval/docs/deploy-ec2.md`](../lex_eval/docs/deploy-ec2.md); the design and its
decisions are in [`lex_eval/docs/decisions/aws-deployment.md`](../lex_eval/docs/decisions/aws-deployment.md).

| File | What it is for |
|---|---|
| `Dockerfile` | Builds the image: Python 3.12, the locked packages, the harness, a non-root user. |
| `requirements.lock` | Every Python package's exact version and hash, so a rebuild installs what was tested. |
| `compose.yml` | Runs the image on AILA's network with no published port, its data on named volumes. |
| `lexeval.sh` | The container's commands: `check`, `run`, `backup`, `dashboard`, `help`. |
| `settings.example` | Every setting, to copy to `/etc/lexeval/settings.env` (mode 0600) and fill in. |
| `lexeval-run.service` | systemd unit for one run; the timer starts it, or start it by hand. |
| `lexeval-run.timer` | Nightly at 02:00 London time. Installed switched off. |
| `ca/` | A CA bundle for a TLS-inspecting proxy, if there is one. Never committed. |
| `smoke/` | The smoke test: `deploy/smoke/smoke.sh` runs the image against a stub AILA, at no cost. |

To update `requirements.lock` after changing the dependencies in `pyproject.toml`, from the repo root
with [uv](https://docs.astral.sh/uv/) installed:

```bash
uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-unknown-linux-gnu \
  --generate-hashes --no-header -o deploy/requirements.lock
```

Then run the unit tests against the new set before committing it.
