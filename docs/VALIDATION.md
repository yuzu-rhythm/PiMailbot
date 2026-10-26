# 公開コピーの検証記録

日付：2026-10-27。環境：macOS、Python3.14.6、Node.js、Chrome headless。対象はこの公開スナップショットです。

## 実施した確認

| 確認 | 結果 |
| --- | --- |
| Python構文コンパイル | `src tests scripts examples` に対して成功 |
| JavaScript構文 | `app.js` / `timeline.js` の `node --check` 成功 |
| パッケージ作成 | wheel作成成功。任意の外部連携依存は同梱しない |
| 新規venvへのインストール | 作成したwheelを `--no-index --no-deps` で導入成功 |
| 依存の整合性 | 新規venvの `pip check` 成功 |
| ローカルメールデモ | 架空メールの取り込み・仮想通知・操作予定の出力、終了コード0 |
| ローカル記録デモ | 架空タスク・手動クレジット等の作成、終了コード0 |
| 画面デモ | ループバックの `/api/snapshot` が200、架空の5情報源を返す |
| 画面例 | 新規Chromeプロファイル、1920×1200で撮影し、予定・タスク・架空メール件数を目視確認 |
| 公開ファイル監査 | 秘密値・個人設定の除外と文字列点検。詳細はSECURITY_REVIEW.md |

画面デモは表示時刻を10:15に固定し、起動日の架空予定を表示します。実APIや本番DBを参照しません。モバイル表示・iPhoneの操作を確認したという意味ではありません。

## 実行コマンド

```bash
python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
python3 -m venv .venv
.venv/bin/python -m pip install --no-index --no-deps dist/offline_discord_mailbot-0.1.0-py3-none-any.whl
.venv/bin/python -m compileall -q src tests scripts examples
node --check src/mailbot/dashboard_static/app.js
node --check src/mailbot/dashboard_static/timeline.js
.venv/bin/python -m pip check
.venv/bin/python -m mailbot mail --mail-db data/portfolio-mail.sqlite3 demo
.venv/bin/python -m mailbot --db data/portfolio.sqlite3 --image-dir data/portfolio-images demo
.venv/bin/python examples/dashboard_demo.py
```

ビルドはsetuptoolsのあるビルド用環境から実行しました。新規venvは作成したwheelだけを導入しています。テスト用DB・venv・wheelはGitへ含めません。

## 実施していない確認

- **既存unittestの実行**：テストコードは同梱していますが、今回の公開作業では実行していません。
- 公開コピーでのGmail・Discord・Todoist・Calendar・Geminiへの認証付き接続。
- 最新の内容分類→Gmail保管→Discord通知削除→Undoの実データでの一連操作。
- 外部APIの障害・応答不明・再認証・履歴失効を含む網羅的な統合検証。
- Piの長期間連続稼働、停電後の復旧、バックアップからの復元。
- インターネットに公開したWebサービスとしての認証・負荷・侵入への対策。

開発時の実機・実サービス確認は [STATUS.md](STATUS.md) に別記し、この公開コピーで再確認した結果と混同しません。
