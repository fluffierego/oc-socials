#!/bin/zsh
set -e
cd "$(dirname "$0")"

echo "OC Socials setup"
echo ""
echo "Paste the Discord Client ID from Discord Developer Portal."
read "CLIENT_ID?Client ID: "
echo ""
echo "Paste the Discord Client Secret. It will be saved only on this Mac."
read -s "CLIENT_SECRET?Client Secret: "
echo ""
cat > .env <<EOF
FLASK_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
PORT=5050
DISCORD_CLIENT_ID=$CLIENT_ID
DISCORD_CLIENT_SECRET=$CLIENT_SECRET
DISCORD_REDIRECT_URI=http://127.0.0.1:5050/oauth/callback
EOF
echo ""
echo "Saved .env."
echo "Make sure Discord OAuth2 Redirects contains:"
echo "http://127.0.0.1:5050/oauth/callback"
echo ""
echo "Now double-click start.command"
