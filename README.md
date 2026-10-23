# codex-usage

Codex App Serverから現在の5時間枠、週間枠、設定されている場合は月間の個別枠を取得します。`collect`では、最近更新されたスレッドのモデル別累積利用量も記録し、前回観測との差分を推定します。

## 必要なもの

- miseと、プロジェクト設定で管理するPython 3.12およびuv。
- インストール済みで、アカウント認証を済ませたCodex CLI。

CLIは`codex app-server --stdio`を起動してJSON-RPCの初期化を行います。`status`は利用枠だけを読みます。`collect`は全種別のスレッド一覧をページングし、指定された更新期間内のスレッドに限ってモデル別利用量を問い合わせます。Discord通知には[discord-notify](https://github.com/Seika139/discord-notify)を使います。このライブラリはPython標準ライブラリのみで動作し、codex-usageはWindows、macOS、Linuxで動作します。

## 実行方法

リポジトリのディレクトリから実行します。

```sh
mise exec -- uv run --locked python -m codex_usage status
```

miseタスクも使えます。

```sh
mise run status
```

どちらも標準出力にはJSONを1つだけ出力します。エラーは標準エラー出力に出し、終了コードを0以外にします。通常のApp Server応答待ちには15秒のタイムアウトを設定しています。

## 利用枠の計測と履歴

`collect`は利用枠とスレッド別利用量を取得し、SQLiteに保存してから同じJSONを標準出力へ返します。保存に成功するまではJSONを出力しません。既定では直近24時間以内に更新されたスレッドだけに`account/usage/read`を呼び出します。期間は`--active-within-hours`で変更できます。

```sh
mise run collect
mise exec -- uv run --locked python -m codex_usage collect --db ./codex-usage.sqlite3
mise exec -- uv run --locked python -m codex_usage collect --active-within-hours 72
```

`collect`は保存前に直前のスナップショットと5時間枠・週間枠の残量を比較します。残量が50%、20%、5%を下回ったとき、または0%以下になったときに通知します。1回の計測で複数の段階を越えた場合は該当する段階を1つのDiscordメッセージにまとめます。初回の計測は比較対象がないため通知しません。リセット日時が前回から変わっていれば新しい枠として残量100%から比較します。

通知先は`CODEX_USAGE_DISCORD_WEBHOOK_URL`環境変数、またはユーザーデータディレクトリに置く`discord-webhook-url`ファイルで指定します。環境変数が設定されている場合はそちらを優先します。環境変数は手動実行に使えますが、定期実行のスケジューラーへは渡さないため、定期通知にはsecret fileを使ってください。たとえば既定のLinux DBを使う場合は`~/.local/share/codex-usage/discord-webhook-url`です。`CODEX_USAGE_DB`でDBを指定した場合は、そのDBのディレクトリをsecret fileの場所として使います。`--db`はsecret fileの場所に影響しません。ファイルにはWebhook URLだけを保存し、Linux/macOSではアクセス権を`600`にしてください。

Webhookが未設定の場合は通知を送らず、スナップショットを保存します。通知の送信に失敗した場合は`collect`も失敗し、スナップショットを保存しません。次の収集で同じ閾値超過を再評価できます。Discordが受信した後に応答だけが失われた場合、送信成功後のDB保存に失敗した場合、または複数の`collect`が同時に実行された場合は重複通知が発生する可能性があります。

`history`は現在UTCから指定時間以内のスナップショットを古い順に返します。`--hours`には有限の正数を指定してください。

```sh
mise run history --hours 24
mise run history -H 24
mise run history -H 24 -v
mise run history --hours 168 --db ./codex-usage.sqlite3
```

`--hours`（短縮形`-H`）は必須で、`--db PATH`は省略できます。`-v`または`--verbose`を付けると、各スナップショットのスレッド別利用量も表示します。どちらの引数もCLIへそのまま渡されます。

SQLiteの保存先は`--db PATH`、または`CODEX_USAGE_DB`で指定できます。既定ではWindowsは`%LOCALAPPDATA%/codex-usage/usage.sqlite3`、macOSは`~/Library/Application Support/codex-usage/usage.sqlite3`、Linuxは`$XDG_DATA_HOME/codex-usage/usage.sqlite3`（未設定なら`~/.local/share/codex-usage/usage.sqlite3`）です。履歴の読み込みでは、DBファイルや親ディレクトリが未作成なら何も作らず、空の`snapshots`を返します。

スナップショットは自動削除されず、DBに無期限で保持されます。`history --hours`は表示対象を絞るだけで、DBの行は削除しません。既定の30分間隔で収集すると1日48件、1年で約17,520件が追加されます。DBサイズを抑える必要がある場合は、バックアップのうえSQLiteの行を手動で削除してください。

`db-stats`はDB内の全スナップショット件数と、SQLiteのメインDBファイルのサイズを確認します。`history --hours`の期間指定とは無関係に全件を数え、DBや親ディレクトリが未作成の場合も新しいファイルは作らず、件数とサイズを0として返します。

```sh
mise run db-stats
mise run db-stats --db ./codex-usage.sqlite3
```

出力には絶対パス、ファイルの有無、`usage_snapshots`の全件数、バイト単位のサイズと1024基準の読みやすいサイズを含みます。`sizeBytes`は未使用ページを含むメインDBファイルの実サイズです。WALや共有メモリなどのサイドカーファイルは含みません。DBのバージョン確認と件数取得は読み取り専用で行い、スキーマ移行やDBへの書き込みはしません。

```json
{
  "databasePath": "/home/user/.local/share/codex-usage/usage.sqlite3",
  "exists": true,
  "usageSnapshots": {
    "snapshotCount": 123
  },
  "sizeBytes": 16384,
  "sizeHumanReadable": "16.0 KiB"
}
```

保存するのは利用枠、取得時刻、各スレッドのID・親ID・source分類・更新時刻・取得状態、スレッド/モデルごとの累積クレジットとトークン数、連続観測間の推定差分です。sourceの任意文字列は`custom`分類に丸めます。モデル別クレジットはApp Serverの整数マイクロクレジットのまま保存します。スレッド名、プレビュー、プロンプト、ファイルパス、コード、生のApp Serverレスポンスは保存しません。定期収集は`mise run schedule install`でOSのユーザースケジューラーに登録できます。

## 出力形式

```json
{
  "schemaVersion": 4,
  "source": "live",
  "collectedAt": "2026-10-22T06:30:00Z",
  "fiveHour": {
    "usedPercent": 25.0,
    "remainingPercent": 75.0,
    "resetsAt": "2026-10-22T09:30:00Z"
  },
  "weekly": {
    "usedPercent": 60.0,
    "remainingPercent": 40.0,
    "resetsAt": "2026-10-26T00:00:00Z"
  },
  "individualLimit": {
    "limit": "30000",
    "used": "8617.864060759544",
    "remainingPercent": 71,
    "resetsAt": "2026-11-01T00:00:00Z"
  },
  "modelUsage": {
    "status": "complete",
    "observedAt": "2026-10-22T06:30:01Z",
    "threadListComplete": true,
    "threadCount": 28,
    "listedThreadCount": 28,
    "targetedThreadCount": 3,
    "notTargetedThreadCount": 25,
    "attemptedThreadCount": 3,
    "notAttemptedThreadCount": 0,
    "retrievedThreadCount": 3,
    "unavailableThreadCount": 0,
    "failedThreadCount": 0,
    "activeWithinHours": 24,
    "threads": [
      {
        "threadId": "thread-id",
        "parentThreadId": null,
        "source": "cli",
        "updatedAt": 1792650000,
        "recencyAt": 1792650000,
        "activityAt": 1792650000,
        "activityAtField": "recencyAt",
        "archived": false,
        "usageStatus": "success",
        "observedAt": "2026-10-22T06:30:02Z",
        "models": [
          {
            "model": "gpt-5-codex",
            "cumulativeUsage": {
              "estimatedUsageCreditsMicros": 1250000,
              "inputTokens": 12000,
              "cachedInputTokens": 3000,
              "outputTokens": 2000,
              "totalTokens": 14000
            },
            "periodUsageEstimate": {
              "status": "baseline",
              "intervalStart": null,
              "intervalEnd": "2026-10-22T06:30:02Z",
              "intervalSeconds": null,
              "estimatedUsageCreditsMicros": null,
              "inputTokens": null,
              "cachedInputTokens": null,
              "outputTokens": null,
              "totalTokens": null,
              "attribution": "detected_at_interval_end"
            }
          }
        ]
      }
    ]
  }
}
```

Codexが該当枠を返さない場合、その枠は`null`です。5時間枠と週間枠は`primary`や`secondary`という名前ではなく、期間（300分または10080分）で判別します。月間の個別枠はApp Serverの`individualLimit`から取得し、`limit`と`used`は受信した文字列を保持します。`rateLimitsByLimitId`が存在する場合は、その`codex`項目だけを使います。旧形式の`rateLimits`はマップがないか`null`で、かつ別の`limitId`を明示していない場合に限り使います。

`status`と`history`の各スナップショットでは`individualLimit.limit`に上限値を表示し、上限が変わった場合も取得時点ごとの値を記録します。

`status`は利用枠のみを表示し、スレッド照会を行いません。`collect`は`recencyAt`（Codexのスレッド一覧でrecency orderingに使われる時刻）を活動判定と降順表示に使い、欠落時は最終更新時刻の`updatedAt`へfallbackします。出力の`activityAtField`は採用したフィールドを示します。`history`の`modelUsage.threads`は要求範囲内にusage観測時刻があるスレッドだけを表示します。取得件数などのsummary fieldsはスレッド一覧・照会対象全体のcollection状態を表し、表示中のthreads件数とは一致しない場合があります。`not_targeted`は活動範囲外または活動時刻が取得できず、usage照会を行わなかった状態です。`not_attempted`は対象でしたが通信失敗や収集期限により照会されなかった状態です。`complete`は指定された対象範囲の全件を取得できたことを表し、全履歴の利用量を取得したという意味ではありません。通信エラーや時間上限により一部を取得できない場合は`partial`または`unavailable`と件数で示します。

`cumulativeUsage`はスレッド/モデルごとの累積値です。`periodUsageEstimate`は記録開始後に同じスレッド/モデルで前回成功した観測との差です。初回観測は`baseline`として保存し、過去利用量として計上しません。差分区間には開始・終了観測時刻と秒数を表示します。カウンター減少は負の利用量にせず`counter_reset`とし、差分を`null`にします。App ServerがUSD額を返さないため、クレジットの整数マイクロ単位を保存し、USD額は出力しません。

App Serverの応答でモデル内訳が省略された場合、そのモデルの累積値を0とはみなしません。次にそのモデルが返った観測では、保存済みの同一thread/model値との差を出し、区間開始時刻はそのモデルの直近の観測時刻になります。

### `history`の出力と読み方

`history`のJSONには、取得時刻の`generatedAt`、指定時間の`rangeHours`、モデル別推定集計の`modelUsageEstimate`、保存記録の配列`snapshots`が含まれます。`snapshots`は記録時刻の古い順です。従来の出力キー`samples`を`snapshots`に変更したため、JSONを読み込むスクリプトがある場合は参照するキー名を更新してください。この変更でSQLiteの保存形式は変わりません。

通常の出力では、各スナップショットの`modelUsage`に含まれる集計状態や件数は表示しますが、長くなりやすい`modelUsage.threads`は省略します。`-v`または`--verbose`を付けると、スレッドごとの状態とモデル別利用量を含む全詳細を表示します。`modelUsageEstimate`は、表示を省略した場合も含め、保存されている全スレッド情報から計算します。

`modelUsageEstimate`は、App Serverから返された累積利用量そのものではなく、同じスレッド・モデルについて連続して成功した観測の差分を期間内で合算した推定です。APIから利用期間を指定して取得することはできないため、観測できた区間だけを使います。

| フィールド | 意味 |
| --- | --- |
| `rangeStart` / `rangeEnd` | 集計期間の開始・終了時刻です。UTCで、`rangeEnd`はコマンド実行時刻です。 |
| `attribution` | 差分を実際に使った時刻ではなく、差分を検出した区間終了時刻に割り当てます。 |
| `intervalBoundaryPolicy` | 区間終了時刻が期間内なら、区間全体を加算します。 |
| `coverage` | `observed_intervals_only`は、保存された観測間の差分だけを集計し、観測していない区間を補間しないことを示します。 |
| `subagentDoubleCountingPossible` | `true`は、サブエージェントと親の両方に利用量が含まれて二重計上となる可能性を示します。 |
| `partialCollectionCount` | 集計期間内で、モデル利用量の収集が完全でなかったスナップショット数です。 |
| `baselineIntervalCount` | 集計期間内で、前回値がなく差分を計算できなかった初回観測数です。 |
| `counterResetIntervalCount` | 集計期間内で、累積カウンターが前回より小さく、リセットと判断して差分から除外した区間数です。 |
| `invalidIntervalCount` | 集計期間内で、時刻や利用量の値に問題があり、差分から除外した区間数です。 |
| `byModel` | 次の表の項目をモデルごとにまとめた配列です。空の場合、期間内に合算可能な差分がありません。 |

`byModel`内の値は以下のとおりです。クレジットはApp Serverのマイクロクレジット単位で、1,000,000マイクロクレジットが1クレジットです。USDの請求額ではありません。

トークン数の項目は、合算対象の区間に値がないものが一つでもあれば`null`になります。欠けた区間を0とみなして過少集計することを避けるためです。

| フィールド | 意味 |
| --- | --- |
| `model` | モデル名です。 |
| `estimatedUsageCreditsMicros` | 期間内の有効な観測区間について、クレジット差分を合計した値です。 |
| `inputTokens` | 入力トークンの差分合計です。 |
| `cachedInputTokens` | 入力のうちキャッシュ由来のトークン数です。`inputTokens`の内数なので、別途足さないでください。 |
| `outputTokens` | 出力トークンの差分合計です。 |
| `totalTokens` | 入力と出力の差分合計です。 |
| `contributingThreadCount` | 有効な差分を合計に含めたスレッド数です。 |
| `intervalCount` | 合計に含めたスレッド・モデル単位の観測区間数です。 |

たとえば集計範囲が10:00から翌10:00までで、ある差分区間の観測が9:50と10:10だった場合、終了時刻の10:10が範囲内なので差分全体を加算します。9:50から10:00までの利用分も含まれます。正確な10:00以降の分だけに按分はしません。

観測が11:00に累積5,000、次に15:00に累積5,800だった場合、その間の差分800を15:00にまとめて計上します。両端の成功観測があれば、間が空いても累積差分は計算できますが、11:00から15:00のどの時点で使われたかは分かりません。15:00以降に次の成功観測がなければ、その後の利用量の差分はまだ計算できません。

初回に累積値10,000を観測しても比較元がないため、その10,000を期間利用量には含めません。前回10,000、今回200のように累積値が減った場合も、負の差分とはせず`counter_reset`として区間を除外します。`baselineIntervalCount`と`counterResetIntervalCount`は、このように合計へ入れなかった区間数です。次の観測から、リセット後の増加分を計算できます。

```json
{
  "modelUsageEstimate": {
    "rangeStart": "2026-10-22T00:00:00Z",
    "rangeEnd": "2026-10-23T00:00:00Z",
    "attribution": "detected_at_interval_end",
    "intervalBoundaryPolicy": "include_full_interval_when_end_is_in_range",
    "coverage": "observed_intervals_only",
    "subagentDoubleCountingPossible": true,
    "partialCollectionCount": 0,
    "baselineIntervalCount": 1,
    "counterResetIntervalCount": 0,
    "invalidIntervalCount": 0,
    "byModel": [
      {
        "model": "gpt-5-codex",
        "estimatedUsageCreditsMicros": 750000,
        "inputTokens": 6000,
        "cachedInputTokens": 1500,
        "outputTokens": 1000,
        "totalTokens": 7000,
        "contributingThreadCount": 2,
        "intervalCount": 3
      }
    ]
  }
}
```

サブエージェントの子スレッドは親スレッドと別のレコードとして表示します。親のusageに子の利用が含まれるかはAPIから確認できないため、モデル別合算は二重計上の可能性があります。`modelUsageEstimate.subagentDoubleCountingPossible`でその可能性を明示します。既存DBの移行前スナップショットはスレッド帰属が復元できないため`modelUsage: null`となり、新形式の差分は移行後の最初の観測をbaselineとして開始します。

## 対応範囲

利用枠と最近更新されたスレッドごとのモデル累積値取得、SQLiteへの保存、連続観測差分の推定、指定期間の履歴出力に対応します。利用の実発生時刻やCodexセッションログの解析には対応しません。

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
