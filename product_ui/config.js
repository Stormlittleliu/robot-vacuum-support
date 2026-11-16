/* Public runtime configuration for the static product UI.
 *
 * Default: same-origin reverse proxy, API under /api.
 * For a separately hosted frontend, copy this file at deploy time and set an
 * absolute API base such as "https://api.example.com/api". Do not put a
 * token in this file: the admin token and optional visitor token live only in
 * browser sessionStorage/headers.
 */
window.RVS_CONFIG = Object.assign(
  { apiBase: "/api" },
  window.RVS_CONFIG || {}
);
