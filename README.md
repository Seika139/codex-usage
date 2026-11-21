# codex-usage

Codex App Serverから5時間枠、週間枠、設定されている場合は月間の個別枠を取得し、SQLiteに時系列として保存します。`collect`はあわせてCodexのスレッド一覧を読み、スレッドIDごとの最新メタデータだけを保持し、静的な利用枠ダッシュボードを更新します。モデル別利用量の照会は行いません。

## 必要なもの

- miseと、プロジェクト設定で管理するPython 3.12およびuv。
- インストール済みで、アカウント認証を済ませたCodex CLI。

CLIは`codex app-server --stdio`を起動してJSON-RPCを初期化します。`status`は利用枠だけを取得し、`collect`は利用枠と全種別のスレッド一覧を取得します。Discord通知には[discord-notify](https://github.com/Seika139/discord-notify)を使います。このライブラリはPython標準ライブラリのみで動作し、codex-usageはWindows、macOS、Linuxで動作します。

## 実行方法

リポジトリのディレクトリから実行します。

```sh
mise exec -- uv run --locked python -m codex_usage status
mise run status
```

`status`と`collect`は標準出力にJSONを1つだけ出力します。エラーは標準エラー出力に出し、終了コードを0以外にします。通常のApp Server応答待ちには15秒のタイムアウトを設定しています。

## 利用枠の計測と履歴

`collect`は利用枠とスレッド一覧を取得してSQLiteへ保存し、JSONを標準出力へ返します。スレッド別利用量の照会はしません。スレッド一覧の取得に失敗した場合、その回の一覧は保存せず、既存の最新情報を保持します。

```sh
mise run collect
mise exec -- uv run --locked python -m codex_usage collect --db ./codex-usage.sqlite3
```

`collect`は保存前に直前の利用枠スナップショットと5時間枠・週間枠の残量を比較します。残量が50%、20%、5%を下回ったとき、または0%以下になったときに通知します。1回の計測で複数の段階を越えた場合は該当する段階を1つのDiscordメッセージにまとめます。初回の計測は比較対象がないため通知しません。リセット日時の差が1秒以内なら同じ枠として扱い、2秒以上の差では前回残量を100%として比較します。

Discord通知の計測日時とリセット日時はDiscordのタイムスタンプ形式で送信し、閲覧者のタイムゾーンと言語設定に合わせて表示します。

通知先は`CODEX_USAGE_DISCORD_WEBHOOK_URL`環境変数、またはユーザーデータディレクトリに置く`discord-webhook-url`ファイルで指定します。環境変数が設定されている場合はそちらを優先します。環境変数は手動実行に使えますが、定期実行のスケジューラーへは渡さないため、定期通知にはsecret fileを使ってください。たとえば既定のLinux DBを使う場合は`~/.local/share/codex-usage/discord-webhook-url`です。`CODEX_USAGE_DB`でDBを指定した場合は、そのDBのディレクトリをsecret fileの場所として使います。`--db`はsecret fileの場所に影響しません。ファイルにはWebhook URLだけを保存し、Linux/macOSではアクセス権を`600`にしてください。

Webhookが未設定の場合は通知を送らず、スナップショットを保存します。通知の送信に失敗した場合は`collect`も失敗し、スナップショットを保存しません。次の収集で同じ閾値超過を再評価できます。Discordが受信した後に応答だけが失われた場合、送信成功後のDB保存に失敗した場合、または複数の`collect`が同時に実行された場合は重複通知が発生する可能性があります。

`history`は現在UTCから指定時間以内の利用枠スナップショットを古い順に返します。`--hours`には有限の正数を指定してください。

```sh
mise run history --hours 24
mise run history -H 24 -v
mise run history --hours 168 --db ./codex-usage.sqlite3
```

`--hours`（短縮形`-H`）は必須で、`--db PATH`は省略できます。`-v`または`--verbose`を付けると、レート制限履歴とは別に、DB内のスレッドごとの最新メタデータを`latestThreads`として一度だけ返します。この一覧は`--hours`の期間にかかわらず最新の保存状態で、各スレッドに最終観測時刻`lastSeenAt`を含みます。通常出力には`latestThreads`を含めません。

## スマホ向け静的ダッシュボード

`collect`はスナップショットを保存したあと、DBごとの専用ディレクトリに自己完結した静的HTMLページを生成します。ページには5時間枠の残率を直近24時間、週間枠の残率を直近7日間で表示します。値はクレジット数ではなく、App Serverが返す利用率から算出した割合です。枠データの欠測は線を途切れさせます。観測間隔から推定した目安（観測間隔の下側中央値の2.5倍）を超えた場合も線を分けますが、計測点が少ないと間隔を推定できず、長い空白が線でつながる場合があります。ページにはスレッド情報やDBのパス、Webhook URLを含めません。

手動で再生成する場合は次を実行します。`--db`で別DBを指定すると、そのDB専用の別ディレクトリに生成され、他のダッシュボードを上書きしません。コマンドはHTMLファイルの絶対パスを出力します。

```sh
mise run dashboard
mise run dashboard --db ./codex-usage.sqlite3
```

常駐Webアプリは必要ありません。Tailscale Serveで生成したHTMLファイルだけをtailnet内へ配信できます。定期収集が実行されるたびに配信ファイルも更新されますが、すでにブラウザーで開いているページは自動更新しないため、新しい計測を見るにはページを再読み込みしてください。手動で最新化する場合は、まず`mise run dashboard`でHTMLを再生成してからブラウザーを再読み込みします。Linuxではファイル配信の設定にroot権限が必要です。Serveの引数には上のコマンドが表示する`index.html`のパスを使います。

```sh
dashboard_path="$(mise run dashboard)"
sudo tailscale serve --bg "$dashboard_path"
sudo tailscale serve status
```

表示されたHTTPS URLをスマホのブラウザーで開きます。スマホも同じtailnetへ接続している必要があり、Serveはtailnetのアクセス制御に従います。通常はVPSの新しい受信ポートを開ける必要はありません。Tailscale ServeはHTTPS証明書とtailnet内向け配信を提供します。[Tailscale Serveの公式ドキュメント](https://tailscale.com/docs/reference/tailscale-cli/serve)を参照してください。

Serve対象は生成された`index.html`だけにしてください。DBやWebhook secret fileがある親ディレクトリ全体は配信しないでください。ページは収集時に更新される静的スナップショットなので、表示できるのは最後に成功した収集までのデータです。

SQLiteの保存先は`--db PATH`、または`CODEX_USAGE_DB`で指定できます。既定ではWindowsは`%LOCALAPPDATA%/codex-usage/usage.sqlite3`、macOSは`~/Library/Application Support/codex-usage/usage.sqlite3`、Linuxは`$XDG_DATA_HOME/codex-usage/usage.sqlite3`（未設定なら`~/.local/share/codex-usage/usage.sqlite3`）です。履歴の読み込みでは、DBファイルや親ディレクトリが未作成なら何も作らず、空の`snapshots`を返します。

利用枠のスナップショットは自動削除されず無期限に保持します。スレッド情報は`thread_id`ごとに最新の1組だけを保持します。スレッド名、プレビュー、プロンプト、ファイルパス、コード、生のApp Serverレスポンスは保存しません。

## データベースの確認と圧縮

`db-stats`はsnapshot件数、ユーザーテーブル別の行数と容量、SQLiteのメインDBファイルサイズを表示します。

```sh
mise run db-stats
mise run db-stats --db ./codex-usage.sqlite3
```

ユーザーデータディレクトリに作られるDBは`~/.local/share/codex-usage/usage.sqlite3`です。schema v5への移行は次の`collect`または`db-compact`で実行されます。移行ではレート制限スナップショットを維持し、過去のスレッド観測・モデル利用量履歴を各スレッドの最新メタデータ1件へ集約します。そのため過去の各snapshotに紐づくスレッド詳細とモデル利用量推定は以後表示されません。移行前の履歴を保管したい場合は、最初の`collect`または`db-compact`の前にDBをバックアップしてください。

SQLiteは削除した行の領域をすぐOSへ返さないため、移行後に一度`db-compact`を実行してDBファイルを縮小してください。このコマンドは未実行のschema移行を先にcommitし、その後`VACUUM`で空きページを回収します。

```sh
mise run db-compact
mise run db-compact --db ./codex-usage.sqlite3
```

`VACUUM`は一時ファイルを使い、DBファイルの最大約2倍の追加空き容量を必要とすることがあります。約200 MiBのDBなら、実行前に少なくとも約400 MiBの空き容量を確保してください。圧縮に失敗しても、先にcommitされた移行後DBは整合した状態で残ります。詳しくは[SQLite VACUUM](https://www.sqlite.org/lang_vacuum.html)を参照してください。

`db-compact`はDBのバックアップを作らないため、重要なデータでは事前にDBファイルをコピーしてください。WALなどのサイドカーファイルを含めたバックアップ方法はSQLiteのjournal modeに応じてください。

`db-stats`は`usageSnapshots.snapshotCount`に全snapshot件数、`tables`にユーザーテーブル別の行数と容量を返します。各テーブルの`dataSizeBytes`はテーブル本体、`indexSizeBytes`はそのテーブルに属する全インデックス、`sizeBytes`はその合計です。SQLiteの`dbstat`が使えない場合も行数は返し、容量は`null`、`tableStorageAvailable`は`false`になります。テーブル別容量には空きページや管理領域を含まないため、合計はメインDBファイルの実サイズと一致しないことがあります。WALなどのサイドカーファイルは実サイズに含みません。

```json
{
  "databasePath": "/home/user/.local/share/codex-usage/usage.sqlite3",
  "exists": true,
  "usageSnapshots": {
    "snapshotCount": 123
  },
  "tableStorageAvailable": true,
  "tables": [
    {
      "name": "threads",
      "rowCount": 42,
      "dataSizeBytes": 8192,
      "indexSizeBytes": 4096,
      "sizeBytes": 12288
    },
    {
      "name": "usage_snapshots",
      "rowCount": 123,
      "dataSizeBytes": 12288,
      "indexSizeBytes": 4096,
      "sizeBytes": 16384
    }
  ],
  "sizeBytes": 32768,
  "sizeHumanReadable": "32.0 KiB"
}
```

`db-compact`は圧縮前後のサイズと回収バイト数をJSONで返します。

`history --verbose`の出力例です。`latestThreads`は履歴期間と独立した最新状態で、古いsnapshotへの紐付けを表しません。

```json
{
  "generatedAt": "2026-11-21T03:40:00Z",
  "rangeHours": 24,
  "snapshots": [],
  "latestThreads": [
    {
      "threadId": "thread-id",
      "parentThreadId": null,
      "source": "vscode",
      "updatedAt": 1795164595,
      "recencyAt": 1795164562,
      "activityAt": 1795164562,
      "activityAtField": "recencyAt",
      "archived": false,
      "lastSeenAt": "2026-11-21T03:32:55Z"
    }
  ]
}
```

## 出力形式

利用枠スナップショットの出力例です。

```json
{
  "schemaVersion": 5,
  "source": "live",
  "collectedAt": "2026-11-21T03:32:55Z",
  "fiveHour": {
    "usedPercent": 25.0,
    "remainingPercent": 75.0,
    "resetsAt": "2026-11-21T06:30:00Z"
  },
  "weekly": {
    "usedPercent": 60.0,
    "remainingPercent": 40.0,
    "resetsAt": "2026-11-25T00:00:00Z"
  },
  "individualLimit": null
}
```

`collect`のJSONには`threadInventory`が加わり、完全な一覧取得時は`threadListComplete: true`と`threads`を含みます。各thread entryは`threadId`、`parentThreadId`、`source`、`updatedAt`、`recencyAt`、`activityAt`、`activityAtField`、`archived`、`lastSeenAt`を持ちます。`activityAt`は`recencyAt`があればそれを、なければ`updatedAt`を使って作ります。`source`の任意文字列は`custom`分類に丸めます。

Codexが該当枠を返さない場合、その枠は`null`です。5時間枠と週間枠は期間（300分または10080分）で判別します。月間の個別枠はApp Serverの`individualLimit`から取得します。`rateLimitsByLimitId`が存在する場合は、その`codex`項目だけを使います。旧形式の`rateLimits`はマップがないか`null`で、かつ別の`limitId`を明示していない場合に限り使います。

## 対応範囲

利用枠と個別上限の計測、SQLiteへの時系列保存、利用枠履歴、最新スレッド一覧の保持と表示に対応します。モデル別利用量やトークン利用量の取得には対応しません。

## 開発

miseが管理するPythonとuvを使ってRuffのチェックとフォーマットを実行します。

```sh
mise run lint
mise run format
```

lintはRuffのlintとformat checkを行い、formatは自動修正を適用します。Python依存関係はuv.lockで固定します。

## 定期収集

次のタスクは現在のOSのユーザー領域へ登録・確認・削除を行います。`install`はスケジュール登録のみを行い、その場では収集しません。

```sh
mise run schedule install
mise run schedule install --interval-minutes 10
mise run schedule install -I 10
mise run schedule status
mise run schedule uninstall
```

`--interval-minutes`（短縮形`-I`）には1〜44640（31日）の整数を指定できます。省略時は30分です。再インストールで省略した場合も30分に設定されます。`status`は登録済みの間隔を秒数で返し、`status`と`uninstall`はこのオプションを受け付けません。

各OSでユーザー権限のスケジューラーを使います。macOSはLaunchAgent、Linuxはsystemd user timer、WindowsはTask Schedulerに登録します。Linuxではsystemd user managerが起動している必要があり、利用できない場合はエラーになります。cronへの切り替えは行いません。登録後のDB保存先はインストール時に解決した絶対パスへ固定します。statusとuninstallはCodexを起動しません。旧形式の30分スケジュールもstatusとuninstallで扱え、次のinstall時に新形式へ更新されます。

macOSのLaunchAgentはユーザーのログインセッション中に動作し、ログアウト中は収集しません。スリープ中は実行されず、復帰後は逃した複数回分を再生せず、必要に応じて1回実行します。Windowsのタスクは「ユーザーがログオンしている場合のみ」動作し、ログアウト中は収集しません。スリープからPCを起こす設定はなく、逃した実行はスケジューラーが利用可能になった後に開始される場合があります。新形式のLinuxタイマーは経過時間で動き、停止中に逃した実行を再生しません。旧形式のタイマーは次のinstallまでカレンダー式の挙動を維持します。

macOSとWindowsの標準出力・標準エラーはユーザーデータ領域の`logs/scheduled-collect.log`へ追記し、自動ローテーションや削除は行いません。Linuxではsystemd journalに記録され、保存期間はsystemdの設定に従います。登録情報と所有manifestが一致しない場合やmanifestが欠落・改変された場合、既存jobの更新・削除は拒否します。
