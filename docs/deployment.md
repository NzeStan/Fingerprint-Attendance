# Deployment

## Plain HTTP and custom ports

Many ZKTeco firmwares only push over **plain HTTP**, often to a non-standard port, and cannot
validate certificates. Accept that safely:

1. **Separate listener.** Expose ADMS on its own port or host through a reverse proxy that only
   forwards `/<ADMS_URL_PREFIX>` paths to Django. The REST API and admin stay on HTTPS.
2. **Restrict sources.** Allow only your sites' public IPs (proxy ACL and/or
   `ADMS_ALLOWED_IPS`), or better, connect devices over a site-to-site VPN and never expose
   ADMS publicly.
3. **Trust the proxy explicitly.** Set `ADMS_TRUSTED_PROXY_IPS` so device IPs come from
   `X-Forwarded-For` only when the request comes from your proxy.
4. **Keep the device unapproved until verified**, and consider disabling auto-registration once
   rollout is complete.
5. Template data travels between device and server in whatever form the firmware uses. On
   untrusted networks, use a VPN.

Example nginx:

```nginx
server {
    listen 8081;                         # the port configured on the devices
    location /iclock/ {
        allow 203.0.113.0/24;            # your sites
        deny all;
        client_max_body_size 20m;        # backlogs after long outages
        proxy_read_timeout 120s;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Host $host;
        proxy_pass http://127.0.0.1:8000;
    }
    location / { return 404; }
}
```

If firmware cannot set a path and you use a secret `ADMS_URL_PREFIX`, rewrite in the proxy:
`location /iclock/ { proxy_pass http://127.0.0.1:8000/k3j2h/iclock/; }`.

## Workers and timeouts

- Backlog uploads can hold thousands of lines. Keep `ADMS_MAX_REQUEST_BYTES` and the proxy
  body limit in step, and allow ~60–120 s timeouts.
- Use `TASK_BACKEND="celery"` (or `django`) in production so processing, hooks, webhooks and
  backfills run off the request path, and schedule `beat_schedule()`.
- The ADMS rate limiter and the offline detector use the Django cache. Use a shared cache
  (Redis/Memcached) with several web workers.
- `fpa_live_capture` (pull mode) is a long-running process. Run one instance per set of devices
  under a supervisor.

## Database

Any Django-supported database works. PostgreSQL is recommended (row locking with
`SKIP LOCKED` for command hand-out, and good `bulk_create` conflict handling). Indexes cover
punch lookups by enrollee/device/PIN and time, the command queue, and retry scans.

## Time

`USE_TZ=True` is required. Set `TIME_ZONE` or `DEFAULT_DEVICE_TIMEZONE` to where most devices
are, and give devices in other zones their own `timezone`. Keep server clocks on NTP. Server
time is the reference for drift detection.

## Checklist

- [ ] `python manage.py check --deploy` and `check` are clean (no `fpa.E` errors)
- [ ] Encryption keys come from a secret store; a backup key is recorded
- [ ] ADMS port is restricted; devices are approved and have timezones set
- [ ] Celery (or cron) runs the periodic jobs
- [ ] Retention settings match your policy
- [ ] Admin and API permissions reviewed (who has `view_template_data`, `clear_device_*`)
