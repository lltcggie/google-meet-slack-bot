#!/bin/bash

mkdir -p channnel-settings
chmod 777 channnel-settings

docker stop google-meet-slack-bot
docker rm google-meet-slack-bot

docker run -d --name google-meet-slack-bot \
  -e SLACK_BOT_TOKEN="xoxb-..." \
  -e SLACK_APP_TOKEN="xapp-..." \
  -e GOOGLE_SERVICE_ACCOUNT_FILE="/app/secrets/service-account.json" \
  -e GOOGLE_WORKSPACE_DOMAIN="example.com" \
  -v "$(pwd)"/secrets:/app/secrets:ro \
  -v "$(pwd)"/channnel-settings:/etc/GoogleMeetEventCreater \
  google-meet-slack-bot
