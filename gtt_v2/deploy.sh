#!/bin/bash
# Deploy gtt_v2 to Lightsail
# Usage: ./deploy.sh <lightsail-ip> [ssh-key-path]
# Example: ./deploy.sh 13.235.10.42
#          ./deploy.sh 13.235.10.42 ~/.ssh/lightsail_key.pem

set -e

REMOTE_IP="${1:?Usage: ./deploy.sh <lightsail-ip> [ssh-key-path]}"
SSH_KEY="${2:-~/.ssh/lightsail_key.pem}"
REMOTE_USER="ubuntu"
REMOTE_DIR="/home/ubuntu/gtt_v2"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $REMOTE_USER@$REMOTE_IP"
RSYNC="rsync -avz --progress -e \"ssh -i $SSH_KEY -o StrictHostKeyChecking=no\""

echo "▶ Deploying to $REMOTE_USER@$REMOTE_IP:$REMOTE_DIR"

# 1. Install Docker on remote if not present
echo "▶ Checking Docker on remote..."
$SSH "docker --version 2>/dev/null || (curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker ubuntu)"

# 1b. Add 2GB swap if not already present (prevents OOM during Docker builds on 2GB RAM)
echo "▶ Ensuring 2GB swap exists..."
$SSH "[ -f /swapfile ] || (sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab)"
$SSH "free -h | grep -i swap"

# 2. Create directories on remote
$SSH "mkdir -p $REMOTE_DIR/data"

# 3. Sync code (exclude local-only files)
echo "▶ Syncing code..."
eval $RSYNC \
  --exclude='.env' \
  --exclude='data/*.session' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  . $REMOTE_USER@$REMOTE_IP:$REMOTE_DIR/

# 4. Copy Telegram session file
echo "▶ Copying Telegram session..."
scp -i $SSH_KEY data/session_name.session \
    $REMOTE_USER@$REMOTE_IP:$REMOTE_DIR/data/session_name.session

# 5. Copy .env (strip local overrides, set cloud values)
echo "▶ Uploading .env..."
# Read current .env, send it as-is (POSTGRES_DSN/REDIS_HOST are overridden by docker-compose)
scp -i $SSH_KEY ../.env $REMOTE_USER@$REMOTE_IP:$REMOTE_DIR/.env

# 6. Build and start
echo "▶ Building Docker images one at a time (avoids OOM on 2GB instance)..."
for svc in db redis signal-ingestor trading-engine order-tracker price-monitor admin-api; do
  echo "  Building $svc..."
  $SSH "cd $REMOTE_DIR && docker compose build $svc"
done

echo "▶ Starting services..."
$SSH "cd $REMOTE_DIR && docker compose up -d"

echo ""
echo "✅ Deployed! Services:"
$SSH "cd $REMOTE_DIR && docker compose ps"
echo ""
echo "📋 Live logs:  ssh -i $SSH_KEY $REMOTE_USER@$REMOTE_IP 'cd $REMOTE_DIR && docker compose logs -f'"
echo "🌐 Admin API:  http://$REMOTE_IP:8080"
