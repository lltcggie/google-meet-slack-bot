#!/bin/bash

mkdir -p GoogleMeetEventCreater

docker stop google-meet-event-creater
docker rm google-meet-event-creater

docker run -d --name google-meet-event-creater \
  -e SLACK_BOT_TOKEN="xoxb-..." \
  -e SLACK_APP_TOKEN="xapp-..." \
  -e GOOGLE_SERVICE_ACCOUNT_FILE="/app/secrets/service-account.json" \
  -e GOOGLE_WORKSPACE_DOMAIN="example.com" \
  -v "$(pwd)"/secrets:/app/secrets:ro \
  -v "$(pwd)"/GoogleMeetEventCreater:/etc/GoogleMeetEventCreater \
  google-meet-event-creater
