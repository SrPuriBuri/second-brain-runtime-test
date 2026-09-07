# Second Brain Public Runtime

Ephemeral GitHub Actions compute layer for the private `SrPuriBuri/second-brain` repository.

## Contract

- The private repository is the only authoritative memory/vault.
- The private repository must not be relied on for GitHub Actions compute.
- This public repository stores runtime code, not personal source queues or durable evidence.
- The scheduled bridge checks the private queue, exits cheaply when no work is eligible, and processes eligible sources on GitHub-hosted runners.
- Acquisition is adaptive and Gemini-first; raw media is temporary and is not persisted.
- Successful evidence is written back to the private repository and materialized into its vault.
- Failed sources remain private and use bounded timeout plus retry backoff.
- Submitted/saved sources are acquisition signals only; they do not imply preference, endorsement, intent, belief, or future action.
- Private bridge/request/evidence commits should use `[skip ci]` so they do not consume private Actions quota or generate avoidable failed-run emails.

## YouTube acquisition

Normal order:

```text
YouTube
  → metadata/oEmbed
  → youtube-transcript-api
  → Gemini technical video analysis
  → private evidence
```

Cloud IP blocking of `youtube-transcript-api` / `yt-dlp` is expected.

The runtime therefore also supports:

```text
youtube-transcript-api fails
  → youtube-transcript.ai HTTP fallback
  → Gemini visual-only pass
  → merge transcript + visual evidence
```

For unusually difficult videos, temporarily reduce concurrency and increase the per-source timeout, process small batches, then restore normal throughput. See the private project's `docs/RUNTIME_OPERATING_MODEL.md` for the durable operating rules.

The active workflow is `.github/workflows/private-evidence-bridge.yml`.
