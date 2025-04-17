# ベースとなるPythonイメージを選択 (適宜バージョン調整)
FROM python:3.13-slim

# Python関連の環境変数を設定
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# コンテナ内の作業ディレクトリを設定
WORKDIR /app

# アプリケーション実行用の非rootユーザーとグループを作成
RUN addgroup --system app && adduser --system --group app

# プレフィックス保存用ディレクトリを作成し、appユーザーに権限を付与
# ボリュームマウント時に権限問題を避けるため先に作成・権限設定
RUN mkdir -p /etc/GoogleMeetEventCreater/ && chown -R app:app /etc/GoogleMeetEventCreater/

# requirements.txt をコピーして依存関係を先にインストール (Dockerキャッシュを活用)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードをコピーし、appユーザーに権限を付与
COPY --chown=app:app app.py .

# 実行ユーザーをappユーザーに切り替え
USER app

# コンテナ起動時に実行するコマンド
CMD ["python", "app.py"]