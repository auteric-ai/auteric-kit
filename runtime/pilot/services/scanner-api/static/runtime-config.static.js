// The S3 shell uses same-origin /api requests. Cloudflare must route those
// requests to the live scanner runtime; the browser never redirects elsewhere.
window.AUTERIC_STATIC_MODE = false;
