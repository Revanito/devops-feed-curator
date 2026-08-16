# Deploying behind a reverse proxy

Two hosts involved:

- **The nginx host** — wherever your reverse-proxy nginx runs, terminating TLS and fronting whatever
  subdomain you're using for this.
- **The app host** — wherever this container runs (Docker/LXC/VM, doesn't matter).

## 1. App container

On the app host:

```
git clone https://github.com/Revanito/devops-feed-curator.git
cd devops-feed-curator
cp .env.example .env   # fill in ONE_MIN_API_KEY, optionally ADMIN_TOKEN
docker compose up -d --build
curl -s localhost:8080 | head -5   # sanity check it's actually serving
```

(Adjust the port here and in `nginx-reverse-proxy.conf` to whatever you map in `docker-compose.yml`.)

## 2. DNS

Point your chosen subdomain (e.g. `feeds.example.com`) at your public IP, same as any other reverse
proxied service you already run.

## 3. nginx vhost, on the nginx host

```
sudo cp nginx-reverse-proxy.conf /etc/nginx/sites-available/feeds
sudo ln -s /etc/nginx/sites-available/feeds /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Edit the copied file first to replace `<YOUR_DOMAIN>` and `<APP_HOST_IP>` with your real values.

## 4. TLS

```
sudo certbot --nginx -d <YOUR_DOMAIN>
```

This rewrites the vhost in place to add the 443/TLS server block and an http→https redirect. Verify with
`sudo nginx -t && sudo systemctl reload nginx` (certbot usually reloads for you).

## 5. Verify

```
curl -I https://<YOUR_DOMAIN>
```

Should return `200 OK` from FastAPI, not nginx's default page or a 502 (502 means nginx can't reach the
app host on the expected port — check the container is up and no firewall rule is blocking it).
