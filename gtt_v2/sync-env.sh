#!/bin/bash
# Push .env to server and restart all containers to pick up changes.
# Usage: ./sync-env.sh <lightsail-ip> [ssh-key-path]
# Example: ./sync-env.sh 3.111.123.148 ~/Downloads/LightsailDefaultKey-ap-south-1.pem

set -e
REMOTE_IP="${1:?Usage: ./sync-env.sh <lightsail-ip> [ssh-key-path]}"
SSH_KEY="${2:-~/.ssh/lightsail_key.pem}"
REMOTE_USER="ubuntu"
REMOTE_DIR="/home/ubuntu/gtt_v2"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $REMOTE_USER@$REMOTE_IP"

ENV_FILE="$(dirname "$(realpath "$0")")/../.env"

echo "▶ Uploading .env to $REMOTE_IP..."
scp -i $SSH_KEY "$ENV_FILE" $REMOTE_USER@$REMOTE_IP:$REMOTE_DIR/.env

echo "▶ Restarting containers to pick up new .env..."
$SSH "cd $REMOTE_DIR && docker compose up -d --force-recreate"

echo ""
echo "✅ Done. Services restarted with updated .env"
$SSH "cd $REMOTE_DIR && docker compose ps"
