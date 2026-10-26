# セットアップ

## ローカルデモ

Python3.11以上でREADMEの手順に沿ってインストールします。CLIと `examples/dashboard_demo.py` は外部認証を使いません。実運用のDBと分けた `data/portfolio*` を使用してください。

## Discord

```bash
python -m pip install -e '.[discord,gmail-auth]'
cp .env.example .env.local
chmod 600 .env.local
python scripts/setup_discord_token.py
```

自分の検証用Discordアプリ・サーバー・チャンネル・ユーザーを用意し、`.env.local` の許可IDを設定します。空の許可リストやDMからの操作は拒否します。

```bash
python scripts/run_discord.py
```

この起動はDiscordへ接続してコマンド登録を行います。閲覧のみのポートフォリオ確認には不要です。

## Gmail

Google CloudプロジェクトでGmail APIを有効にし、OAuth同意画面とDesktop用クライアントを設定します。組織アカウントでは管理ポリシーも確認してください。クライアントJSONはGit除外の `data/google/client_secret.json` へ置き、親0700・ファイル0600で管理します。

```bash
python scripts/setup_gmail.py --client data/google/client_secret.json
# メールの分類・保管まで行う場合だけ、必要性を確認してmodify権限を取得
python scripts/setup_gmail.py --client data/google/client_secret.json --modify --replace
```

認証後、`MAILBOT_GMAIL_TOKEN_PATH`、必要ならアカウント専用のDBパスを設定します。通知は `MAILBOT_GMAIL_ENABLED=true`、変更操作は `MAILBOT_GMAIL_ACTIONS_ENABLED=true` を明示的に設定します。ゴミ箱・タスク連携後の保管は対応する追加フラグを確認してください。認証情報を取得しただけで全フラグを有効にしないでください。

複数アカウントでは `examples/accounts.example.json` を参考に非公開の `data/google/accounts.json` を作り、アカウントごとに異なるtoken/DB/チャンネルを指定します。実際のメールアドレスとIDは各自のものに置換してください。マニフェストの親0700・ファイル0600を要求します。各チャンネルは環境変数の許可リストにも登録します。

```bash
python scripts/setup_gmail_account.py --account personal
```

`setup_gmail_account.py` はリポジトリ基準のtokenパスを検証します。Piへ移す場合はPi用の絶対パスへマニフェストを書き換え、トークンを非公開で配置し直してください。OAuthの本番化・審査・再認証・組織制限は、公開コードだけでは完了しません。

## Todoist・Gemini

それぞれ `setup_todoist_token.py`、`setup_gemini_key.py` で非表示入力し、`.env.example` の任意フラグを設定します。外部サービスへ実データを送る場合は送信範囲と利用料を確認してください。Geminiの有効化には送信への明示同意フラグも必要です。複数Gmailの自動要約は現在のアカウント管理コードで無効化されています。

## カレンダーと画面

Google Calendarは読み取り専用です。`setup_calendar.py` は非公開の `data/google/calendar/selection.json` とクライアント設定を使います。自分のアカウント・カレンダーIDを設定してから認証してください。`examples/dashboard.example.json` は設定の構造例です。

```bash
python -m mailbot.dashboard --config /absolute/path/to/private/config.json
```

既定の接続先は `http://127.0.0.1:8765/` です。実データを返す画面をインターネットへ直接公開しないでください。認証付きの外出先アクセスは未実装です。

## Piでのsystemd運用

`deploy/mailbot.service` と `deploy/mailbot-dashboard.service` は構成例です。専用の `mailbot` ユーザーと、コード `/opt/mailbot/current`、非公開状態 `/var/lib/mailbot`、設定 `/etc/mailbot/mailbot.env` を前提とします。コード配置・venv・所有権・OAuthファイルは各自の環境に合わせて用意します。

サービスを有効にする前に、手動起動、アカウント一致、許可リスト、バックアップ、復元手順を確認してください。GUI、Chromium、Xorg、画面解像度の設定は端末固有のため、自動インストーラーをこの公開版に同梱していません。サービス例の配置だけでHDMI表示が完成するものではありません。

## 既知の運用条件

- 新着取得の初回は現在位置から開始し、過去メールを全件取り込みません。
- Gmail履歴が失効した場合は、未取得期間の扱いを確認して基準を再設定します。
- バックアップはタスクDBだけでなくアカウント別Gmail DBも必要です。
- 通知の削除と元メールの削除は別です。保管はINBOXラベルの除去であり永久削除ではありません。
- 個人用の手動デプロイ・過去の環境修復・ラベル一括改名スクリプトは含めていません。`category_label_migration.py` は固定のLinux設定場所を参照する保守モジュールで、初回設定には使いません。
