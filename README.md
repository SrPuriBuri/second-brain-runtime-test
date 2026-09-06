# Second Brain Public Runtime

Ephemeral GitHub Actions compute layer for the private `SrPuriBuri/second-brain` repository.

## Contract

- The private repository is the only authoritative memory/vault.
- This public repository stores runtime code, not personal source queues or durable evidence.
- The scheduled bridge checks the private queue, exits cheaply when no work is eligible, and processes eligible sources on GitHub-hosted runners.
- Acquisition is adaptive and Gemini-first; raw media is temporary and is not persisted.
- Successful evidence is written back to the private repository and materialized into its vault.
- Failed sources remain private and use bounded timeout plus retry backoff.
- Submitted/saved sources are acquisition signals only; they do not imply preference, endorsement, intent, belief, or future action.

The active workflow is `.github/workflows/private-evidence-bridge.yml`.
