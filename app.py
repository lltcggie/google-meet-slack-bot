import os
import re
import logging
import json
import pytz
import requests
from datetime import datetime, timedelta, timezone
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request as GoogleAuthRequest
from filelock import Timeout, FileLock

# --- 設定 ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN") # Socket Mode用
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") # サービスアカウントキー(JSON)へのパス
GOOGLE_WORKSPACE_DOMAIN = os.environ.get("GOOGLE_WORKSPACE_DOMAIN")
STORAGE_DIR = "/etc/GoogleMeetEventCreater/" # プレフィックス設定の保存先

# 必要なAPIスコープ
GOOGLE_API_SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/meetings.space.settings' # Meetスペースの設定読み取り/変更用スコープ
]
MEET_API_BASE_URL = "https://meet.googleapis.com/v2"

# JSTタイムゾーンオブジェクトを作成
JST = pytz.timezone('Asia/Tokyo')

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Slack Bolt アプリ初期化 ---
app = App(token=SLACK_BOT_TOKEN)

# --- Google API認証関数 ---
def get_google_credentials(user_email):
    """指定されたユーザーとしてGoogle APIの認証情報を取得"""
    try:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=GOOGLE_API_SCOPES,
            subject=user_email # ユーザーの代理として動作
        )
        # 必要に応じてトークンをリフレッシュ
        creds.refresh(GoogleAuthRequest())
        logger.debug(f"Successfully obtained Google API credentials for user: {user_email}")
        return creds
    except Exception as e:
        logger.error(f"Failed to obtain Google API credentials for {user_email}: {e}")
        raise

# --- Google Calendar Service 取得関数 ---
def get_calendar_service(credentials):
    """認証情報からGoogle Calendar APIサービスを取得"""
    try:
        service = build('calendar', 'v3', credentials=credentials, cache_discovery=False)
        return service
    except Exception as e:
        logger.error(f"Failed to build Google Calendar service: {e}")
        raise

# --- Slackユーザーのメールアドレス取得関数 ---
def get_user_email(client, user_id):
    """SlackユーザーIDからメールアドレスを取得"""
    try:
        user_info = client.users_info(user=user_id)
        if user_info["ok"] and user_info.get("user", {}).get("profile", {}).get("email"):
            email = user_info["user"]["profile"]["email"]
            logger.debug(f"Fetched email for user {user_id}: {email}")
            return email
        else:
            logger.warning(f"Could not retrieve email for user {user_id}. Response: {user_info}")
            return None
    except Exception as e:
        logger.error(f"Error fetching email for user {user_id}: {e}")
        return None

# --- Meet API関連の関数 ---
def get_meet_space_id(credentials, conference_id):
    """Meet APIを使用してConference IDからSpace ID (リソース名) を取得"""
    if not conference_id:
        logger.warning("Conference ID is missing, cannot get Space ID.")
        return None, "会議IDが不明です"

    url = f"{MEET_API_BASE_URL}/spaces/{conference_id}"
    headers = {
        'Authorization': f'Bearer {credentials.token}',
        'Content-Type': 'application/json'
    }
    try:
        logger.debug(f"[Meet API] Calling GET {url}")
        response = requests.get(url, headers=headers, timeout=10)
        logger.debug(f"[Meet API] GET {conference_id} - Response Code: {response.status_code}")

        if response.status_code == 200:
            space_data = response.json()
            space_name = space_data.get('name') # 形式: "spaces/{space_id}"
            if space_name:
                logger.debug(f"[Meet API] Found space resource name: {space_name}")
                return space_name, None
            else:
                logger.warning(f"[Meet API] 'name' field not found in space data for {conference_id}. Response: {response.text}")
                return None, f"Meetスペース情報の取得に失敗しました(レスポンス不正): {conference_id}"
        elif response.status_code == 404:
             logger.warning(f"[Meet API] Space not found (404) for {conference_id}. Might not exist yet.")
             return None, f"Meetスペースが見つかりません(404): {conference_id}"
        else:
            logger.error(f"[Meet API] Error getting space for {conference_id}. Status: {response.status_code}, Body: {response.text}")
            return None, f"Meetスペース情報取得エラー({response.status_code}): {conference_id}"

    except requests.exceptions.RequestException as e:
        logger.error(f"[Meet API] Exception during GET {url}: {e}")
        return None, f"Meet API呼び出し中に通信エラーが発生しました: {e}"
    except Exception as e:
         logger.error(f"[Meet API] Unexpected exception during GET {url}: {e}")
         return None, f"Meetスペース情報取得中に予期せぬエラーが発生しました: {e}"

def enable_meet_auto_artifact(credentials, space_id):
    """Meet APIを使用して指定されたSpace IDの自動録画、自動文字起こし、自動スマートメモを有効にする"""
    if not space_id:
        logger.warning("Space ID is missing, cannot enable auto recording.")
        return False, "MeetスペースIDが不明です"

    # space_idは "spaces/xxxx" の形式
    url = f"{MEET_API_BASE_URL}/{space_id}"
    headers = {
        'Authorization': f'Bearer {credentials.token}',
        'Content-Type': 'application/json'
    }
    # updateMaskクエリパラメータで更新対象フィールドを指定
    params = {'updateMask': 'config.artifactConfig.recordingConfig.autoRecordingGeneration,config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration,config.artifactConfig.smartNotesConfig.autoSmartNotesGeneration'}
    payload = json.dumps({
        "config": {
            "artifactConfig": {
                "recordingConfig": {
                    "autoRecordingGeneration": "ON"
                },
                "transcriptionConfig": {
                    "autoTranscriptionGeneration": "ON"
                },
                "smartNotesConfig": {
                    "autoSmartNotesGeneration": "ON"
                }
            }
        }
    })

    try:
        logger.debug(f"[Meet API] Calling PATCH {url}")
        response = requests.patch(url, headers=headers, params=params, data=payload, timeout=10)
        logger.debug(f"[Meet API] PATCH {space_id} - Response Code: {response.status_code}")

        if response.status_code >= 200 and response.status_code < 300:
            logger.debug(f"[Meet API] Successfully configured auto-recording for space {space_id}. Response: {response.text}")
            return True, None
        else:
            error_msg = f"Meet自動録画設定エラー({response.status_code}): {space_id}, Response: {response.text}"
            logger.error(f"[Meet API] Error configuring auto-recording. {error_msg}")
            if response.status_code == 403:
                 error_msg += " (権限不足の可能性があります。Meet APIスコープとWorkspace設定を確認してください)"
            return False, error_msg

    except requests.exceptions.RequestException as e:
        logger.error(f"[Meet API] Exception during PATCH {url}: {e}")
        return False, f"Meet API呼び出し中に通信エラーが発生しました: {e}"
    except Exception as e:
        logger.error(f"[Meet API] Unexpected exception during PATCH {url}: {e}")
        return False, f"Meet自動録画設定中に予期せぬエラーが発生しました: {e}"


# --- スラッシュコマンド `/reg-mtg-prefix` のハンドラー ---
@app.command("/reg-mtg-prefix")
def handle_reg_prefix(ack, command, client, respond, logger):
    """ /reg-mtg-prefix コマンドを処理してチャンネルごとの会議名プレフィックスを設定/解除 """
    ack() # Slackにコマンド受信を3秒以内に通知
    logger.info(f"Received /reg-mtg-prefix command: {command}")

    user_id = command["user_id"]
    channel_id = command["channel_id"]
    prefix_text = command["text"].strip()

    # --- 権限チェック (チャンネル作成者も許可) ---
    try:
        # 1. ユーザーがAdmin/Ownerかチェック
        user_info = client.users_info(user=user_id)
        is_admin = user_info.get("user", {}).get("is_admin", False)
        is_owner = user_info.get("user", {}).get("is_owner", False)
        is_creator = False # デフォルトはFalse

        # 2. ユーザーがチャンネル作成者かチェック (Admin/Ownerでない場合)
        #    conversations.infoを呼び出す (channels:read or groups:read スコープが必要)
        try:
            conv_info = client.conversations_info(channel=channel_id)
            if conv_info['ok']:
                creator_id = conv_info.get('channel', {}).get('creator')
                if creator_id == user_id:
                    is_creator = True
                    logger.debug(f"User {user_id} is the creator of channel {channel_id}.")
            else:
                logger.warning(f"Failed to get conversation info for {channel_id}: {conv_info.get('error')}")
                # チャンネル情報が取得できない場合、creatorチェックは失敗とする
        except Exception as conv_e:
            logger.error(f"Error calling conversations.info for channel {channel_id}: {conv_e}")
            # API呼び出し自体に失敗した場合もcreatorチェックは失敗とする

        # 3. 最終的な権限判定
        if not (is_admin or is_owner or is_creator):
            logger.warning(f"User {user_id} is not an admin, owner, or creator. Permission denied for /reg-mtg-prefix in channel {channel_id}.")
            respond(text="このコマンドを実行する権限がありません。Slackの管理者/オーナー、またはこのチャンネルの作成者のみが実行できます。")
            return

    except Exception as e:
        logger.error(f"Error checking user permission for {user_id}: {e}")
        respond(text="ユーザー権限の確認中にエラーが発生しました。")
        return

    # --- ディレクトリとファイルパスの準備 ---
    filepath = os.path.join(STORAGE_DIR, f"{channel_id}.txt")
    filepath_lock = filepath + ".lock" # ロックファイルのパス

    try:
        with FileLock(filepath_lock, timeout=3): # 3秒間ロックを待機
            if not prefix_text:
                # プレフィックスが空 -> 設定解除
                if os.path.exists(filepath):
                    os.remove(filepath)
                    logger.info(f"Removed prefix file for channel {channel_id}: {filepath}")
            else:
                # プレフィックスを設定
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(prefix_text)
                logger.info(f"Set prefix for channel {channel_id} to '{prefix_text}' in {filepath}")

        # ★★★ 成功メッセージ -> in_channel ★★★
        if not prefix_text:
            respond(text=f"<#{channel_id}> の会議名プレフィックスを解除しました。", response_type='in_channel')
        else:
            respond(text=f"<#{channel_id}> の会議名プレフィックスを `{prefix_text}` に設定しました。", response_type='in_channel')

    except PermissionError:
        logger.error(f"Permission denied when trying to access {STORAGE_DIR} or {filepath}")
        respond(text=f"エラー: 設定ファイルの保存場所 ({STORAGE_DIR}) へのアクセス権限がありません。")
    except Timeout as e:
        logger.error(f"Timeout FileLock for channel {channel_id}: {e}")
        respond(text=f"プレフィックスの設定/解除中にエラーが発生しました: {e}")
    except Exception as e:
        logger.error(f"Error handling prefix file for channel {channel_id}: {e}")
        respond(text=f"プレフィックスの設定/解除中にエラーが発生しました: {e}")

# --- スラッシュコマンド `/mtg` のハンドラー ---
# コマンドの登録時に `Escape channels, users, and links sent to your app` をチェックしておくこと
@app.command("/mtg")
def handle_mtg_command(ack, command, client, respond, logger):
    """ /mtg コマンドを処理してGoogleカレンダーイベントを作成し、Meetの自動録画を設定 """
    ack() # Slackにコマンド受信を3秒以内に通知
    logger.info(f"Received /mtg command: {command}")

    user_id = command["user_id"]
    channel_id = command["channel_id"] # チャンネルIDを取得
    command_text = command["text"].strip()

    # --- プレフィックスの読み込み ---
    prefix = ""

    filepath = os.path.join(STORAGE_DIR, f"{channel_id}.txt")
    filepath_lock = filepath + ".lock" # ロックファイルのパス
    with FileLock(filepath_lock, timeout=3): # 3秒間ロックを待機
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    loaded_prefix = f.read().strip()
                    if loaded_prefix: # プレフィックスが空でない場合のみ採用
                        prefix = loaded_prefix
                        logger.debug(f"Loaded prefix '{loaded_prefix}' for channel {channel_id}")
            except Timeout as e:
                logger.error(f"Timeout FileLock for channel {channel_id}: {e}")
                respond(text=f"プレフィックスの取得中にエラーが発生しました: {e}")
                return
            except Exception as e:
                logger.error(f"Failed to read prefix file {filepath}: {e}")
                respond(text=f"プレフィックスの取得中にエラーが発生しました: {e}")
                return
            

    meeting_title = None
    duration_minutes = None
    mentions_text = ""

    # コマンドテキストをパース
    # パターン1: ダブルクォートで囲まれた会議名 ("会議名" 時間 [@メンション...])
    quoted_match = re.match(r'^"([^"]+)"\s+(\d+)\s*(.*)$', command_text)
    # パターン2: ダブルクォートなしの会議名 (会議名 時間 [@メンション...])
    #            会議名はスペースを含まない単語とする
    unquoted_match = re.match(r'^(\S+)\s+(\d+)\s*(.*)$', command_text)

    if quoted_match:
        meeting_title = quoted_match.group(1)
        duration_minutes = int(quoted_match.group(2))
        mentions_text = quoted_match.group(3)
        logger.debug(f"Parsed command (quoted format): title='{meeting_title}', duration={duration_minutes}, mentions='{mentions_text}'")
    elif unquoted_match:
        meeting_title = unquoted_match.group(1)
        duration_minutes = int(unquoted_match.group(2))
        mentions_text = unquoted_match.group(3)
        logger.debug(f"Parsed command (unquoted format): title='{meeting_title}', duration={duration_minutes}, mentions='{mentions_text}'")
    else:
        error_message = (
            'コマンドの形式が正しくありません。\n'
            '形式:\n'
            '  `/mtg "会議名にスペースを含む場合" 会議時間(分) [@ゲスト...]`\n'
            'または\n'
            '  `/mtg 会議名スペースなし 会議時間(分) [@ゲスト...]`'
        )
        # パースエラーはEphemeralで応答
        respond(text=error_message)
        return

    # 実行ユーザー & ゲストのメールアドレス取得
    try:
        owner_email = get_user_email(client, user_id)
        if not owner_email:
            # ユーザー情報取得エラーはEphemeralで応答
            respond(text=f"<@{user_id}> のメールアドレスをSlackプロファイルから取得できませんでした。")
            return

        try:
            user_domain = owner_email.split('@')[1]
            if user_domain.lower() != GOOGLE_WORKSPACE_DOMAIN.lower():
                logger.info(f"User {user_id} ({owner_email}) is not in allowed domain {GOOGLE_WORKSPACE_DOMAIN}.")
                respond(text=f"このコマンドは {GOOGLE_WORKSPACE_DOMAIN} ドメインのユーザーのみ利用できます。")
                return
        except IndexError:
            logger.warning(f"Could not parse domain from email: {owner_email}")
            respond(text="メールアドレスの形式が正しくありません。")
            return

        attendee_emails = []
        mention_ids = re.findall(r'<@([UW][A-Z0-9]+)(?:\|[^>]+)?>', mentions_text)
        failed_guest_lookups = []
        for mention_id in mention_ids:
            # 自分自身へのメンションはゲストリストから除外
            if mention_id == user_id:
                continue
            guest_email = get_user_email(client, mention_id)
            if guest_email:
                # 重複を避けて追加
                if guest_email not in attendee_emails:
                    attendee_emails.append(guest_email)
            else:
                failed_guest_lookups.append(f"<@{mention_id}>")
                logger.warning(f"Could not find email for mentioned user: {mention_id}")

    except Exception as e:
        logger.error(f"Error getting user emails: {e}")
        # ユーザー情報取得中の予期せぬエラーはEphemeralで応答
        respond(text="ユーザー情報の取得中にエラーが発生しました。")
        return

    # イベントの日時を計算 (UTC)
    start_time_utc = datetime.now(timezone.utc)
    end_time_utc = start_time_utc + timedelta(minutes=duration_minutes)
    start_iso = start_time_utc.isoformat()
    end_iso = end_time_utc.isoformat()

    # JSTに変換
    start_time_jst = start_time_utc.astimezone(JST)
    end_time_jst = end_time_utc.astimezone(JST)

    # Google API 認証
    try:
        credentials = get_google_credentials(owner_email)
        calendar_service = get_calendar_service(credentials)
    except Exception as e:
         # 認証エラーはEphemeralで応答
         respond(text=f"Google APIの認証に失敗しました: {e}")
         return

    # Google Calendar イベント作成
    event_url = None
    meet_url = None
    conference_id = None
    recording_error_msg = None # 録画設定エラーメッセージのみ保持

    try:
        # 読み込んだプレフィックスを会議名に追加
        final_meeting_title = f"{prefix}{meeting_title}"

        event_body = {
            'summary': final_meeting_title, # プレフィックス付きタイトルを設定
            'description': f'',
            'start': {'dateTime': start_iso, 'timeZone': 'UTC'},
            'end': {'dateTime': end_iso, 'timeZone': 'UTC'},
            'attendees': [{'email': email} for email in attendee_emails],
            'conferenceData': {
                'createRequest': {
                    'requestId': f"{user_id}-{start_time_utc.timestamp()}", # リクエストごとにユニークID
                    'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                }
            },
            'reminders': {'useDefault': True}, # デフォルトリマインダーを使用
        }

        logger.debug(f"Creating Google Calendar event for {owner_email}: {event_body}")
        # conferenceDataVersion=1 を指定してMeetリンクを確実に生成させる
        created_event = calendar_service.events().insert(
            calendarId='primary', # ユーザーのプライマリカレンダー
            body=event_body,
            conferenceDataVersion=1
        ).execute()

        event_url = created_event.get('htmlLink')
        meet_url = created_event.get('hangoutLink')
        # Meet APIで使うための会議IDを取得
        conference_data = created_event.get('conferenceData', {})
        conference_id = conference_data.get('conferenceId')

        logger.info(f"Successfully created event: {event_url}. Conference ID: {conference_id}")

    except HttpError as error:
        logger.error(f"An API error occurred during calendar event creation: {error}")
        error_details = error.resp.get('content', '{}')
        # カレンダーAPIエラーはEphemeralで応答
        respond(text=f"Googleカレンダーへの予定作成中にエラーが発生しました: {error_details}")
        return
    except Exception as e:
        logger.error(f"An unexpected error occurred during calendar event creation: {e}")
        # カレンダーAPI中の予期せぬエラーはEphemeralで応答
        respond(text=f"予期せぬエラーが発生しました(カレンダーイベント作成時): {e}")
        return

    # Meet 自動録画設定
    if conference_id:
        try:
             space_id, space_id_error = get_meet_space_id(credentials, conference_id)
             if space_id:
                 # enable_meet_auto_artifact は成功/失敗(bool)とエラーメッセージを返す
                 recording_configured, error_msg_on_fail = enable_meet_auto_artifact(credentials, space_id)
                 if not recording_configured:
                     # 失敗した場合のみエラーメッセージを記録
                     recording_error_msg = error_msg_on_fail
             else:
                 recording_error_msg = space_id_error
                 logger.warning(f"Skipping auto-recording setup because Space ID could not be obtained: {recording_error_msg}")
        except Exception as meet_error:
            logger.error(f"An unexpected error occurred during Meet API interaction: {meet_error}")
            recording_error_msg = f"Meet API操作中に予期せぬエラーが発生しました: {meet_error}"
    else:
        logger.warning("Conference ID not found in created event, skipping auto-recording setup.")
        recording_error_msg = "会議IDがイベントから取得できなかったため、自動録画は設定されませんでした。"

    # 最終結果をSlackに送信
    guest_list_str = ", ".join([f"<@{mid}>" for mid in mention_ids if mid != user_id]) if mention_ids else "なし"
    # 日時をJST形式でフォーマット
    time_format = '%Y-%m-%d %H:%M'
    time_format2 = '%H:%M'
    time_str = f"{start_time_jst.strftime(time_format)} ～ {end_time_jst.strftime(time_format2)}"

    message = (
        f"✅ Googleカレンダーに予定を作成\n"
        f"*{final_meeting_title}*\n"
        f"日時: {time_str}\n"
        f"オーナー: <@{user_id}>\n"
        f"ゲスト: {guest_list_str}\n"
        f"Google Meet: {meet_url if meet_url else '取得失敗'}\n"
        f"カレンダーで表示: {event_url if event_url else '取得失敗'}"
    )

    # 自動録画設定に失敗した場合のみ報告を追加
    if recording_error_msg:
        message += f"\n自動録画: ⚠️ 設定に失敗しました ({recording_error_msg})"

    # ゲストのメールアドレス取得失敗に関する注意を追加
    if failed_guest_lookups:
        message += f"\n\n⚠️ 注意: 次のゲストのメールアドレスが見つからず、招待できませんでした: {', '.join(failed_guest_lookups)}"

    # 成功メッセージはチャンネル全員に見えるように応答
    respond(text=message, response_type='in_channel')


# --- アプリ起動 ---
if __name__ == "__main__":
    # 必要な環境変数が設定されているかチェック
    if not SLACK_BOT_TOKEN:
        logger.error("SLACK_BOT_TOKEN environment variable not set.")
        exit(1)
    if not SLACK_APP_TOKEN:
        logger.error("SLACK_APP_TOKEN environment variable not set (required for Socket Mode).")
        exit(1)
    if not GOOGLE_SERVICE_ACCOUNT_FILE or not os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        logger.error(f"Google service account file not found or GOOGLE_SERVICE_ACCOUNT_FILE env var not set correctly: {GOOGLE_SERVICE_ACCOUNT_FILE}")
        exit(1)
    if not GOOGLE_WORKSPACE_DOMAIN:
        logger.error("GOOGLE_WORKSPACE_DOMAIN environment variable not set.")
        exit(1)

    try:
        # ディレクトリが存在しない場合は作成 (権限が必要)
        os.makedirs(STORAGE_DIR, exist_ok=True)
        logger.debug(f"Ensured directory exists: {STORAGE_DIR}")
    except PermissionError:
        logger.error(f"Permission denied when trying to access {STORAGE_DIR}")

    logger.info("Starting Slack Bolt app in Socket Mode...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
