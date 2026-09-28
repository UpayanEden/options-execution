# Hostinger VPS Deployment Guide

## Iron Condor Live Trader — Production Setup

This guide walks through deploying the shifting iron condor trading system on a
Hostinger VPS (Ubuntu 22.04 LTS). It covers:

1. Initial VPS setup and SSH hardening
2. Python virtual environment
3. Nginx reverse proxy with SSL
4. Systemd services for the runner and dashboard
5. Daily Zerodha access token refresh (cron)
6. Log rotation

---

## 1. Initial VPS Setup

### 1.1 SSH into your VPS

```bash
ssh root@YOUR_VPS_IP
```

### 1.2 Create a non-root deploy user

```bash
adduser condorbot
usermod -aG sudo condorbot
# Copy your SSH public key
mkdir -p /home/condorbot/.ssh
cp ~/.ssh/authorized_keys /home/condorbot/.ssh/
chown -R condorbot:condorbot /home/condorbot/.ssh
chmod 700 /home/condorbot/.ssh
chmod 600 /home/condorbot/.ssh/authorized_keys
```

### 1.3 Disable root SSH login

```bash
sed -i 's/^PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart sshd
```

### 1.4 Update system packages

```bash
apt update && apt upgrade -y
apt install -y git nginx certbot python3-certbot-nginx python3.11 python3.11-venv \
               python3-pip build-essential libssl-dev supervisor
```

---

## 2. Application Deployment

### 2.1 Clone or upload the project

```bash
su - condorbot
cd /home/condorbot
git clone https://github.com/YOUR_USERNAME/options_execution.git
# OR use scp/rsync to upload:
# rsync -avz --exclude venv --exclude __pycache__ ./options_execution condorbot@VPS_IP:/home/condorbot/
cd options_execution
```

### 2.2 Create Python virtual environment

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r deploy/requirements.txt
```

### 2.3 Configure credentials

```bash
cp config/credentials.env.example config/credentials.env
nano config/credentials.env
# Fill in:
#   ZERODHA_API_KEY=your_key
#   ZERODHA_API_SECRET=your_secret
#   ZERODHA_ACCESS_TOKEN=today_token
#   TRADING_MODE=live
#   LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED
#   FLASK_SECRET_KEY=$(openssl rand -hex 32)
chmod 600 config/credentials.env
```

### 2.4 Create required directories

```bash
mkdir -p logs/live logs/gunicorn data
```

### 2.5 Test the setup in paper mode

```bash
source venv/bin/activate
python run_live.py --mode paper --list-symbols
```

---

## 3. Nginx Reverse Proxy

### 3.1 Create Nginx site configuration

```bash
sudo nano /etc/nginx/sites-available/condor-live
```

Paste the following (replace `your-domain.com`):

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    # SSL certificates (managed by certbot)
    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;

    # Proxy to gunicorn
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_buffering    off;
    }

    # Static files served directly by Nginx
    location /static/ {
        alias /home/condorbot/options_execution/web/static/;
        expires 1d;
        add_header Cache-Control "public";
    }

    # Block direct access to sensitive files
    location ~ /\.(env|git|gitignore) {
        deny all;
    }
}
```

### 3.2 Enable the site and obtain SSL certificate

```bash
sudo ln -s /etc/nginx/sites-available/condor-live /etc/nginx/sites-enabled/
sudo nginx -t
sudo certbot --nginx -d your-domain.com
sudo nginx -t && sudo systemctl reload nginx
```

---

## 4. Systemd Services

### 4.1 Web dashboard service

```bash
sudo nano /etc/systemd/system/condor-dashboard.service
```

```ini
[Unit]
Description=Iron Condor Live Dashboard (Gunicorn)
After=network.target

[Service]
User=condorbot
Group=condorbot
WorkingDirectory=/home/condorbot/options_execution
EnvironmentFile=/home/condorbot/options_execution/config/credentials.env
ExecStart=/home/condorbot/options_execution/venv/bin/gunicorn \
    -c /home/condorbot/options_execution/deploy/gunicorn.conf.py \
    web.app:app
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=30
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 4.2 Trading runner service

```bash
sudo nano /etc/systemd/system/condor-runner.service
```

```ini
[Unit]
Description=Iron Condor Live Trading Runner
After=network.target condor-dashboard.service
Requires=condor-dashboard.service

[Service]
User=condorbot
Group=condorbot
WorkingDirectory=/home/condorbot/options_execution
EnvironmentFile=/home/condorbot/options_execution/config/credentials.env
ExecStart=/home/condorbot/options_execution/venv/bin/python \
    /home/condorbot/options_execution/run_live.py \
    --mode live --web-only=false
StandardOutput=append:/home/condorbot/options_execution/logs/runner.log
StandardError=append:/home/condorbot/options_execution/logs/runner.log
KillMode=mixed
TimeoutStopSec=60
Restart=no

[Install]
WantedBy=multi-user.target
```

### 4.3 Enable and start services

```bash
sudo systemctl daemon-reload
sudo systemctl enable condor-dashboard condor-runner
sudo systemctl start condor-dashboard
# Start runner manually each trading day after token refresh:
# sudo systemctl start condor-runner
```

### 4.4 Check service status

```bash
sudo systemctl status condor-dashboard
sudo systemctl status condor-runner
journalctl -u condor-runner -f
```

---

## 5. Daily Zerodha Access Token Refresh

Zerodha access tokens expire daily. You must refresh the token before each
trading session (market opens at 09:15 IST).

### 5.1 Create the token refresh script

```bash
nano /home/condorbot/options_execution/deploy/refresh_token.sh
```

```bash
#!/bin/bash
# refresh_token.sh — Refresh Zerodha access token and restart services
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$SCRIPT_DIR/venv/bin/activate"

echo "[$(date)] Starting Zerodha token refresh..."

# Use the token helper to get a new access token
# This requires completing the Kite login flow; adapt to your auth mechanism
# Option A: Use a headless browser script (selenium/playwright)
# Option B: Use the token_helper.py in brokers/
# Option C: Manually paste the request token from the Kite login URL

NEW_TOKEN=$(python "$SCRIPT_DIR/brokers/token_helper.py" 2>/dev/null || echo "")

if [ -z "$NEW_TOKEN" ]; then
    echo "[$(date)] ERROR: Failed to obtain new access token. Aborting."
    exit 1
fi

# Update the credentials.env file
sed -i "s/^ZERODHA_ACCESS_TOKEN=.*/ZERODHA_ACCESS_TOKEN=$NEW_TOKEN/" \
    "$SCRIPT_DIR/config/credentials.env"

echo "[$(date)] Access token refreshed successfully."

# Restart the runner service to pick up the new token
sudo systemctl restart condor-runner
echo "[$(date)] condor-runner restarted."
```

```bash
chmod +x /home/condorbot/options_execution/deploy/refresh_token.sh
```

### 5.2 Schedule the cron job

Run the token refresh every trading day at 08:45 IST (03:15 UTC):

```bash
crontab -e
```

Add the following lines:

```cron
# ── Iron Condor token refresh (weekdays at 08:45 IST = 03:15 UTC) ────────
15 3 * * 1-5 /home/condorbot/options_execution/deploy/refresh_token.sh \
    >> /home/condorbot/options_execution/logs/token_refresh.log 2>&1

# ── Kill switch auto-deactivate at market open (09:15 IST = 03:45 UTC) ───
45 3 * * 1-5 rm -f /home/condorbot/options_execution/logs/KILL_SWITCH_ACTIVE

# ── Stop runner after market close (15:35 IST = 10:05 UTC) ───────────────
5 10 * * 1-5 sudo systemctl stop condor-runner

# ── Log cleanup: keep last 14 days of logs ────────────────────────────────
0 2 * * * find /home/condorbot/options_execution/logs/live -name "*.jsonl" \
    -mtime +14 -delete 2>/dev/null
```

---

## 6. Log Rotation

### 6.1 Configure logrotate

```bash
sudo nano /etc/logrotate.d/condor-live
```

```
/home/condorbot/options_execution/logs/live/*.jsonl
/home/condorbot/options_execution/logs/gunicorn/*.log
/home/condorbot/options_execution/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 condorbot condorbot
    sharedscripts
    postrotate
        systemctl reload condor-dashboard 2>/dev/null || true
    endscript
}
```

---

## 7. Firewall Setup

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
sudo ufw status
```

---

## 8. Monitoring Commands

```bash
# Live runner logs
tail -f /home/condorbot/options_execution/logs/live/trades.jsonl | python3 -m json.tool

# Service status
sudo systemctl status condor-runner condor-dashboard

# Nginx access log
sudo tail -f /var/log/nginx/access.log

# Kill switch status
cat /home/condorbot/options_execution/logs/KILL_SWITCH_ACTIVE 2>/dev/null || echo "Kill switch: INACTIVE"

# Activate kill switch via curl
curl -X POST https://your-domain.com/api/kill_switch/activate \
     -H "Content-Type: application/json" \
     -d '{"reason": "Manual stop from SSH"}'
```

---

## 9. Security Checklist

- [ ] `config/credentials.env` has `chmod 600` permissions
- [ ] `config/credentials.env` is in `.gitignore`
- [ ] SSH root login is disabled
- [ ] Password authentication is disabled (key-only SSH)
- [ ] UFW firewall is enabled
- [ ] Nginx enforces HTTPS-only
- [ ] Flask `SECRET_KEY` is a random 32+ byte string
- [ ] `LIVE_TRADING_CONFIRMATION` is set in the environment
- [ ] Log files do not contain API keys or access tokens
- [ ] Gunicorn is bound to `127.0.0.1` (not exposed directly)

---

## 10. Quick Reference

| Task                      | Command                                              |
|---------------------------|------------------------------------------------------|
| Start dashboard           | `sudo systemctl start condor-dashboard`              |
| Start runner              | `sudo systemctl start condor-runner`                 |
| Stop runner               | `sudo systemctl stop condor-runner`                  |
| Activate kill switch      | `touch logs/KILL_SWITCH_ACTIVE`                      |
| Deactivate kill switch    | `rm -f logs/KILL_SWITCH_ACTIVE`                      |
| View live trades          | `tail -f logs/live/trades.jsonl`                     |
| Refresh Zerodha token     | `./deploy/refresh_token.sh`                          |
| Run tests                 | `venv/bin/pytest tests/ -v`                          |
| Paper test session        | `python run_live.py --mode paper`                    |
