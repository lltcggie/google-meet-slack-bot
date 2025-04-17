# google-meet-slack-bot

Slackで今すぐ開始するMeet付きのGoogleカレンダーの予定を作成するアプリ

## Slack App Manifest
```
{
    "display_information": {
        "name": "GoogleMeetEventCreater",
        "description": "Meet付きのGoogleカレンダーの予定を作成します",
        "background_color": "#262626"
    },
    "features": {
        "bot_user": {
            "display_name": "GoogleMeetEventCreater",
            "always_online": false
        },
        "slash_commands": [
            {
                "command": "/mtg",
                "description": "Meetの予定を作成",
                "usage_hint": "会議名 会議時間(分) [ゲストのメンション] [ゲストのメンション] ... ",
                "should_escape": true
            },
            {
                "command": "/reg-mtg-prefix",
                "description": "/mtg の会議名のプレフィックスを登録",
                "usage_hint": "会議名のプレフィックス",
                "should_escape": false
            }
        ]
    },
    "oauth_config": {
        "scopes": {
            "bot": [
                "commands",
                "users:read",
                "users:read.email",
                "channels:read",
                "groups:read"
            ]
        }
    },
    "settings": {
        "interactivity": {
            "is_enabled": true
        },
        "org_deploy_enabled": false,
        "socket_mode_enabled": true,
        "token_rotation_enabled": false
    }
}
```
