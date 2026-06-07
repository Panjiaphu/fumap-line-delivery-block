# Agent Access Key Setup

This repository is public, so real secrets, private keys, GitHub tokens, project API tokens, and deploy keys must not be committed here.

Use this file as the safe setup guide for AI agents that need to work with FUMAP GO and Timeblock.

## Recommended Secrets

Configure these outside the repository, for example in the hosting provider secret manager, GitHub Actions secrets, or the agent runtime secret store.

```text
TIMEBLOCK_BASE_URL=https://fumap-bot-life.onrender.com
TIMEBLOCK_PROJECT_CODE=fumapgo
TIMEBLOCK_PROJECT_TOKEN=<set-in-secret-manager>
GITHUB_AGENT_TOKEN=<set-in-secret-manager>
```

## GitHub Agent Token

Use a fine-grained GitHub token or GitHub App installation token instead of a broad personal token.

Minimum recommended repository permissions for coding agents:

```text
Contents: Read and write
Pull requests: Read and write, if the agent opens PRs
Metadata: Read
Actions: Read, only if the agent needs CI status
```

Do not grant admin or organization-wide permissions unless a specific workflow requires them.

## SSH Deploy Key Option

If using SSH instead of HTTPS tokens:

1. Generate the private key outside this repository.
2. Add only the public key to GitHub as a deploy key.
3. Store the private key in the agent runtime secret store.
4. Do not commit the private key, `.pem`, `.key`, or raw token file.

Suggested secret names:

```text
FUMAPGO_GITHUB_SSH_PRIVATE_KEY
FUMAPGO_GITHUB_KNOWN_HOSTS
```

## Timeblock Project Token

When the Timeblock project-aware gateway is implemented, FUMAP GO should call Timeblock with a project token or signed request.

Recommended runtime variables:

```text
TIMEBLOCK_BASE_URL=https://fumap-bot-life.onrender.com
TIMEBLOCK_PROJECT_CODE=fumapgo
TIMEBLOCK_PROJECT_TOKEN=<secret>
TIMEBLOCK_REQUEST_TIMEOUT_SECONDS=3
```

## Safety Rules

- Never commit real tokens or private keys.
- Rotate a token immediately if it is pasted into chat, committed, logged, or exposed in a public file.
- Keep reward-affecting writes behind service-layer APIs.
- Require `source_project` and idempotency for Timeblock reward ingestion.
- Use the least permission needed for each agent workflow.
