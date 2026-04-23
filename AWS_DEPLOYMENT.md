# AWS Deployment Guide

## Recommended Architecture

For this app, the cheapest practical AWS design is:

- `1 x Amazon Lightsail instance` or `1 x EC2 instance`
- `Docker + docker compose`
- `1 app container`
- `1 Redis container`
- `1 static public IP`
- local persistent volume for:
  - Telegram session
  - SQLite DB
  - logs

## Why Not Serverless For Core Runtime

This app is not a good fit for Lambda/EventBridge as the primary runtime because it needs:

- long-running Telegram connectivity
- long-running WebSocket connectivity to Upstox
- local Telethon session persistence
- continuous background processing

Serverless can still be used later for support tasks like backups, alerts, or health checks.

## Lowest-Cost AWS Choices

### Best overall

`Amazon Lightsail instance + static IP`

Why:
- fixed monthly pricing
- simpler than EC2/ECS
- static IP support
- enough for one Dockerized Python app + Redis

### Best if you want more AWS flexibility

`EC2 t4g.small or t4g.micro + Elastic IP`

Why:
- works well with Docker Compose
- easy to grow later
- clean static-IP story for Upstox whitelisting

## Deployment Layout

```text
AWS instance
  ├─ Docker
  ├─ app container
  │   ├─ telegram_ai_listener.py
  │   ├─ gtt_strategy.py
  │   └─ upstox_order_tracker.py
  ├─ redis container
  └─ persistent volume
      ├─ session_name.session
      ├─ gtt_signals.db
      └─ logs/
```

## Files Added For AWS

- `Dockerfile`
- `docker-compose.aws.yml`
- `settings.py`

## First Deployment Steps

1. Create a Lightsail or EC2 instance in your preferred region.
2. Attach a static IP / Elastic IP.
3. Register that public IP in Upstox My Apps.
4. Install Docker and Docker Compose.
5. Copy this repo to the server.
6. Copy your `.env`.
7. Copy your `session_name.session` into the mounted app data directory.
8. Create a local runtime folder:

```bash
mkdir -p data/logs data/redis
```

9. Copy your Telegram session file into:

```bash
data/session_name.session
```

10. Start with:

```bash
docker compose -f docker-compose.aws.yml up -d --build
```

## Accessing the Frontend on AWS

When `PROCESS_MODE=web`, the frontend bridge is published on port `8787`.

Open:

```text
http://<YOUR_AWS_PUBLIC_IP>:8787/signal
```

Examples:
- `http://3.111.123.148:8787/signal`
- `http://<elastic-ip>:8787/signal`

If your browser is on the same machine and you don't want to expose the port publicly, you can also use SSH port forwarding:

```bash
ssh -L 8787:127.0.0.1:8787 ubuntu@<YOUR_AWS_PUBLIC_IP>
```

Then open:

```text
http://127.0.0.1:8787/signal
```

## Persistent Data

The app stores runtime state in `./data`, mounted into `/app/data` in Docker:

- `gtt_signals.db`
- `logs/`
- `session_name.session`
- `app.pid`

## Notes

- If the server IP changes, Upstox order placement can fail again with static-IP restriction errors.
- Keep the static IP attached permanently before generating your next access token.
- You will still need to refresh Upstox access tokens unless you automate that login flow later.
- Make sure the AWS security group allows inbound TCP `8787` if you want direct browser access.
