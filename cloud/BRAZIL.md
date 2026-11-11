# Brazil deployment — migration not yet validated

On 2026-11-11, the owner created Fly app `download-arquivos`. Logs showed Machine
`d895275b30d558` in `ams`, a proxy expecting port 8080, and repeated startup exits
because `REEL_INTERNAL_KEY` was missing. The Docker and Fly defaults now use 8080.
The private Site's temporary owner-only `/configurar-fly` page provides the new
Fly key for secure manual registration and an authenticated server health check.
The secret is runtime-only and is not included in source or deployment bundles.
Normal downloads remain on Render until the Fly backend is explicitly selected.

The current Render engine runs in Virginia. Video `dABOAonHlR8` was rejected
with YouTube's country restriction: available in Brazil. Render has no Brazil
region. A client-side VPN does not relocate the server making the requests.

The root `fly.toml` reuses the existing Docker engine in Fly.io's São Paulo
region (`gru`). A Fly.io account and authorized compute/bandwidth billing are
required for continuous operation. No Fly resources have been created by these
source changes. Selecting `gru` in a config does not move an existing `ams` VM.

Deployment handoff for the operator:

1. Create a uniquely named Fly app using the checked-in configuration, with
   primary region `gru`. Deploy exactly one Machine; jobs/files are process-local.
2. Configure `REEL_INTERNAL_KEY` as a runtime secret, never a build argument,
   repository variable, command-line literal or committed file. Use the same
   secret in the private Site; if rotating it, update both runtimes together.
3. Use port 8080 both for the app (`PORT`) and Fly proxy (`internal_port`).
   Build from repository root with `cloud/Dockerfile`. Keep automatic stopping
   disabled so background downloads and temporary files survive between polls.
4. Verify the Machine's actual region and outbound IP country. `primary_region`
   alone does not prove YouTube classifies the egress IP as Brazilian.
5. Test the reported YouTube URL through analysis, full file transfer, quality
   selection and cancellation. A Brazil location addresses the country check;
   other platform restrictions, such as login or anti-bot checks, can remain.
6. Only after those checks, set the private Site's `REEL_BACKEND_URL` to the
   validated HTTPS endpoint and `REEL_BACKEND_PROVIDER=fly`. This selects the
   already stored `REEL_FLY_INTERNAL_KEY`; preserve the Render URL/key for
   rollback. Remove `REEL_FLY_SETUP_OWNER_EMAIL` and `REEL_FLY_SETUP_EXPIRES`
   after setup. Setup key delivery also expires automatically.

Do not mark the reported download fixed until the actual link succeeds from
the deployed engine. No local Python or client-side API setup is needed.

References:
- https://render.com/docs/regions
- https://fly.io/docs/reference/regions/
- https://fly.io/docs/reference/configuration/
- https://fly.io/docs/apps/secrets/
