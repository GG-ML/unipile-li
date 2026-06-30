# Deployment Guide — Unipile LinkedIn Outreach Backend

This guide covers local Docker deployment and production HTTPS deployment using Nginx + Let's Encrypt. The setup is reused from `/root/hiringday_linkedin_login2` and adapted for the Unipile outreach backend.

---

## Local development

```bash
cd /root/unipile-outreach-backend

# Create virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1 — API
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# Terminal 2 — worker
python -m app.worker
```

API will be at `http://localhost:8000`.

---

## Docker (local / internal)

```bash
cd /root/unipile-outreach-backend

# Edit environment variables if needed
cp .env .env

# Start API + Redis + worker
docker compose up --build

# Scale workers
docker compose up --scale worker=3
```

This exposes only port `8000`. Use this for internal testing without a public domain.

---

## Production deployment with domain

### 1. Do you need a domain?

Yes. The frontend will call the API from a browser. For HTTPS + CORS you need a domain pointed at this server.

Default domain configured: `linkedin-server.libingo.io`. Update `nginx.conf` and `setup-ssl.sh` if you want a different domain.

### 2. DNS

Point your domain's A record to the server IP (`103.14.123.198` by default). Example:

```
linkedin-server.libingo.io  A  103.14.123.198
```

### 3. Firewall

Open ports:
- `80` — Let's Encrypt challenge + HTTP redirect
- `443` — HTTPS API traffic
- `8000` — optional, only if you want direct access without Nginx
- `6379` — Redis, should NOT be exposed publicly

### 4. SSL setup

The setup script was copied from `/root/hiringday_linkedin_login2` and simplified.

```bash
cd /root/unipile-outreach-backend

# Make executable
chmod +x setup-ssl.sh

# Run setup
./setup-ssl.sh
```

Options:
1. **Self-signed** — development only, browser warning expected
2. **Let's Encrypt for `linkedin-server.libingo.io`** — production default
3. **Let's Encrypt for custom domain** — updates `nginx.conf` automatically
4. **Remove certificates** — cleanup

### 5. Start production stack

```bash
# Start API + Redis + worker + Nginx + Certbot
docker compose --profile production up -d --build

# Verify
docker compose ps
```

API will be available at:
- `https://linkedin-server.libingo.io`
- `https://linkedin-server.libingo.io/docs`
- `https://linkedin-server.libingo.io/api/...`

HTTP requests automatically redirect to HTTPS.

### 6. Update the frontend

Change the frontend API base URL from `http://localhost:8000` to:

```
https://linkedin-server.libingo.io
```

---

## Migrating from `hiringday_linkedin_login2`

If you previously used the Nginx + SSL setup in `/root/hiringday_linkedin_login2`:

1. The outreach backend now has its own `nginx.conf`, `docker-compose.yml` production profile, and `setup-ssl.sh`.
2. The old project is **not needed** for this API.
3. If you want to reuse the same domain (`linkedin-server.libingo.io`):
   - Stop the old Nginx container: `docker -f /root/hiringday_linkedin_login2/docker-compose.yml stop nginx`
   - Copy the old certificates: `cp -r /root/hiringday_linkedin_login2/certbot/conf /root/unipile-outreach-backend/certbot/`
   - Run `./setup-ssl.sh` option 2 to renew / reissue if needed.

---

## Useful commands

```bash
# View logs
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f nginx
docker compose logs -f certbot

# Test nginx config
docker compose --profile production exec nginx nginx -t

# Check certificates
docker compose --profile production run --rm certbot certificates

# Manual renewal
docker compose --profile production run --rm certbot certbot renew --force-renewal

# Restart services
docker compose --profile production restart api worker nginx

# Stop everything
docker compose --profile production down
```

---

## Troubleshooting

### Nginx fails to start

1. Check certificate files exist:
   ```bash
   ls -la ssl/
   ```
2. Test nginx config:
   ```bash
   docker compose --profile production exec nginx nginx -t
   ```
3. Check logs:
   ```bash
   docker compose --profile production logs nginx
   ```

### Let's Encrypt fails

1. Verify DNS: `dig linkedin-server.libingo.io`
2. Verify port 80 is reachable: `curl -I http://linkedin-server.libingo.io/.well-known/acme-challenge/test`
3. Check firewall rules
4. Use staging mode in `setup-ssl.sh` if you hit rate limits

### CORS errors from frontend

Add your frontend origin to `nginx.conf` in the `if ($http_origin = "...")` blocks under the `/api/` location.

### API not reachable

1. Check API is running: `curl http://localhost:8000/health`
2. Check Nginx can reach API: `docker compose --profile production exec nginx wget -qO- http://api:8000/health`
3. Check `docker compose ps`

---

## Production checklist

- [ ] Domain DNS points to server IP
- [ ] Firewall allows 80 and 443
- [ ] SSL certificates issued (Let's Encrypt)
- [ ] `docker compose --profile production up -d` runs successfully
- [ ] HTTPS redirect works
- [ ] API endpoints accessible via `https://your-domain/api/...`
- [ ] Frontend origin added to `nginx.conf` CORS rules
- [ ] Worker process is running and scheduling tasks
- [ ] Redis is not exposed publicly
- [ ] Certificate auto-renewal tested
