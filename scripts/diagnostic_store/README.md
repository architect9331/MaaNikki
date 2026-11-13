# Diagnostic screenshot store

Standalone Python 3.11+ service for the optional
`telemetry.sentry.failure_upload_endpoint` setting. Sentry continues receiving errors
and structured logs; screenshot ZIPs go here instead. An upload failure records an
omission reason without falling back to a Sentry attachment. Projects without this
setting retain the generic client's existing Sentry attachment transport.

## Deployment

1. Create a system account `maanikki-diagnostics` without a login shell. Install
   `server.py` under `/opt/maanikki-diagnostics/`, owned by root and not writable by
   the service account. Create `/srv/maanikki-diagnostics` owned by
   `maanikki-diagnostics:www-data` with mode `2750`.
2. Copy `config.example.json` to `/etc/maanikki-diagnostics.json` (root owned,
   readable by the service). Install the supplied systemd unit, run
   `systemd-analyze verify`, then enable and start it. Only loopback port 9340 is
   exposed. `GET http://127.0.0.1:9340/healthz` reports process availability.
3. Install `nginx-limits.conf` in `/etc/nginx/conf.d/`. Install
   `nginx-location.conf` as a snippet included **only in the HTTPS server**.
   Create `/etc/nginx/maanikki-diagnostics.htpasswd` with a separate operator
   password using `htpasswd`, root ownership, group `www-data` and mode `0640`.
   Keep the password outside source control and application packages.
4. Run `nginx -t` before reloading nginx. Configure the application with
   `https://YOUR_DOMAIN/maanikki-diagnostics/upload`. Do not put the operator
   password in this URL or in the distributed client.

Default limits: 5,000,000,000 stored bytes, 30-day retention, at least 2 GiB disk
free, 512 MiB daily intake (UTC), 10,000 files, 5 MiB per upload, 128 images per
ZIP and eight concurrent service connections. nginx additionally limits upload
rate and connections. Keep request buffering disabled so incoming bodies do not
consume nginx's disk spool. Requests require a bounded Content-Length.

Cleanup runs at startup, before storing each upload and every ten minutes. It
removes expired bundles and interrupted temporary writes. A full budget rejects
new uploads; it never evicts unexpired evidence to accommodate incoming traffic.
The data cap excludes filesystem metadata, so the disk reserve is independent.
The service does not extract ZIPs or serve files. nginx serves downloads only
after operator authentication, with caching disabled and directory browsing off.

Uploads are anonymous because credentials embedded in a public desktop client
cannot authenticate a trustworthy sender. Random 256-bit IDs prevent enumeration,
but **download access also requires the operator password**. Rate, daily and total
budgets bound abuse; malicious uploads can still exhaust the daily intake budget.
IP limits use nginx's actual peer. Behind a CDN, several users can share a limit;
only configure real-IP restoration with a maintained trusted proxy range list.

## Operations and rollout

Use `systemctl status maanikki-diagnostics` and
`journalctl -u maanikki-diagnostics` for service status. Review disk usage with
`du -sh /srv/maanikki-diagnostics` and `df -h /`.

After restarting the newly compiled client, an actual failed task with a captured
screenshot should report `attachment.status=external` and `attachment.url` in the
Sentry event's additional data. Open the URL and log in with the operator account.
The event also includes the ZIP's SHA-256, byte count and expiry (Unix seconds).
The client verifies the receipt and creates the download URL on the configured
origin. Uploads are bounded to 20 seconds, two concurrent client requests, and
cancel when diagnostic consent is revoked or the application exits. Already
received data is subject to the same retention policy.

Older distributed executables continue their previous upload behavior until users
update. Existing Sentry attachments remain subject to Sentry's own retention and
billing cycle. To roll back a deployment, remove the endpoint setting and use a
compatible client; that deliberately restores the Sentry attachment transport.

Deployment/configuration checks do not verify a real gameplay upload. Gameplay
verification is performed by the user; do not manufacture failures or run tests.
