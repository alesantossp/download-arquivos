# Reel — Online video download engine

Server component for the private Reel web application. Processes public video links with yt-dlp and FFmpeg, supports quality selection and cancellation, and transfers prepared files to the browser.

Deploy on Render with `cloud/Dockerfile` and the repository root as build context. Set `REEL_INTERNAL_KEY` as a runtime secret with at least 32 random characters. Never commit credentials. The server listens on `PORT` and requires the trusted Site proxy for download routes.

Temporary files expire after one hour. Platform availability varies; private, login-required and DRM-protected content is unsupported. Cosmos is experimental.
