# Deploying feeds.vaultinc.fr

Two LXCs involved:

- **192.168.1.170** — the nginx reverse-proxy LXC (same one fronting site.vaultinc.fr and the other
  `*.vaultinc.fr` subdomains).
- **192.168.1.192** — the docker LXC running this app (alongside discord-1min-proxy).

## 1. App container

On **192.168.1.192**, if not already done:

```
git clone https://github.com/Revanito/devops-feed-curator.git
cd devops-feed-curator
cp .env.example .env   # fill in ONE_MIN_API_KEY, optionally ADMIN_TOKEN
docker compose up -d --build
curl -s localhost:8085 | head -5   # sanity check it's actually serving
```

## 2. DNS

Add an A record for `feeds.vaultinc.fr` pointing at the same public IP the other `*.vaultinc.fr`
subdomains resolve to.

## 3. nginx vhost, on 192.168.1.170

```
sudo cp feeds.vaultinc.fr.conf /etc/nginx/sites-available/feeds.vaultinc.fr
sudo ln -s /etc/nginx/sites-available/feeds.vaultinc.fr /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

(`feeds.vaultinc.fr.conf` in this directory — copy it over, e.g. `scp` from this repo checkout, or
recreate it by hand.)

## 4. TLS

Same per-subdomain certbot pattern as the other vhosts:

```
sudo certbot --nginx -d feeds.vaultinc.fr
```

This rewrites the vhost in place to add the 443/TLS server block and an http→https redirect on port 80.
Verify with `sudo nginx -t && sudo systemctl reload nginx` (certbot usually reloads for you).

## 5. Verify

```
curl -I https://feeds.vaultinc.fr
```

Should return `200 OK` from FastAPI, not nginx's default page or a 502 (502 means nginx can't reach
192.168.1.192:8085 — check the container is up and no firewall/FirewallD rule on .192 is blocking port
8085 from .170).
