# Brazil deployment — prepared, not yet provisioned

The current Render engine runs in Virginia. Video `dABOAonHlR8` was rejected
with YouTube's country restriction: available in Brazil. Render has no Brazil
region. A client-side VPN does not relocate the server making the requests.

The root `fly.toml` reuses the existing Docker engine in Fly.io's São Paulo
region (`gru`). A Fly.io account and authorized compute/bandwidth billing are
required before provisioning; none has been created or charged by this change.

Deployment handoff for the operator:

1. Create a uniquely named Fly app using the checked-in configuration, with
   primary region `gru`. Deploy exactly one Machine; jobs/files are process-local.
2. Configure `REEL_INTERNAL_KEY` as a runtime secret, never a build argument,
   repository variable, command-line literal or committed file. Use the same
   secret in the private Site; if rotating it, update both runtimes together.
3. Build from repository root with `cloud/Dockerfile`. Keep automatic stopping
   disabled so background downloads and temporary files survive between polls.
4. Verify the Machine's actual region and outbound IP country. `primary_region`
   alone does not prove YouTube classifies the egress IP as Brazilian.
5. Test the reported YouTube URL through analysis, full file transfer, quality
   selection and cancellation. A Brazil location addresses the country check;
   other platform restrictions, such as login or anti-bot checks, can remain.
6. Only after those checks, set the private Site's `REEL_BACKEND_URL` to the
   validated HTTPS endpoint. Keep the current Render endpoint as rollback.

Do not mark the reported download fixed until the actual link succeeds from
the deployed engine. No local Python or client-side API setup is needed.

References:
- https://render.com/docs/regions
- https://fly.io/docs/reference/regions/
- https://fly.io/docs/reference/configuration/
- https://fly.io/docs/apps/secrets/
