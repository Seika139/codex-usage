# codex-usage

Codex App Serverから現在の5時間枠と週間枠を取得し、安定したJSON形式で出力するクロスプラットフォームCLIです。

## 必要なもの

- miseと、プロジェクト設定で管理するPython 3.12。
- インストール済みで、アカウント認証を済ませたCodex CLI。

CLIは`codex app-server --stdio`を起動してJSON-RPCの初期化を行い、`account/rateLimits/read`の応答を待ちます。Python標準ライブラリのみを使い、Windows、macOS、Linuxで動作します。

## 実行方法

リポジトリのディレクトリから実行します。

```sh
mise exec -- python -m codex_usage status
```

miseタスクも使えます。

```sh
mise run status
```

どちらも標準出力にはJSONを1つだけ出力します。エラーは標準エラー出力に出し、終了コードを0以外にします。App Serverの応答待ちには15秒のタイムアウトを設定しています。

## 利用枠の計測と履歴

`collect`はライブの利用枠を取得し、SQLiteに保存してから同じstatus JSONを標準出力へ返します。保存に成功するまではJSONを出力しません。

```sh
mise run collect
mise exec -- python -m codex_usage collect --db ./codex-usage.sqlite3
```

`history`は現在UTCから指定時間以内のスナップショットを古い順に返します。`--hours`には有限の正数を指定してください。

```sh
mise run history --hours 24
mise run history --hours 168 --db ./codex-usage.sqlite3
```

`--hours`は必須で、`--db PATH`は省略できます。どちらの引数もCLIへそのまま渡されます。

SQLiteの保存先は`--db PATH`、または`CODEX_USAGE_DB`で指定できます。既定ではWindowsは`%LOCALAPPDATA%/codex-usage/usage.sqlite3`、macOSは`~/Library/Application Support/codex-usage/usage.sqlite3`、Linuxは`$XDG_DATA_HOME/codex-usage/usage.sqlite3`（未設定なら`~/.local/share/codex-usage/usage.sqlite3`）です。履歴の読み込みでは、DBファイルや親ディレクトリが未作成なら何も作らず、空の`samples`を返します。

保存するのはstatusの枠有無、使用率、リセット時刻、取得時刻だけです。App Serverの生レスポンス、プロンプト、ファイルパス、コードは保存しません。定期収集は`mise run schedule-install`でOSのユーザースケジューラーに登録できます。

## 出力形式

```json
{
  "schemaVersion": 1,
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
  }
}
```

Codexが該当枠を返さない場合、その枠は`null`です。枠は`primary`や`secondary`という名前ではなく、期間（300分または10080分）で判別します。`rateLimitsByLimitId`が存在する場合は、その`codex`項目だけを使います。旧形式の`rateLimits`はマップがないか`null`で、かつ別の`limitId`を明示していない場合に限り使います。

## 対応範囲

利用枠の現在値取得、SQLiteへのスナップショット保存、指定期間の履歴出力に対応します。Codexセッションログの解析には対応しません。

## 開発

miseが管理するPythonとuvを使ってRuffのチェックとフォーマットを実行します。

```sh
mise run lint
mise run format
```

lintはRuffのlintとformat checkを行い、formatは自動修正を適用します。Python依存関係はuv.lockで固定します。

## 30分ごとの自動収集

次のタスクは現在のOSのユーザー領域へ登録・確認・削除を行います。`install`はスケジュール登録のみを行い、その場では収集しません。

```sh
mise run schedule-install
mise run schedule-status
mise run schedule-uninstall
```

各OSでユーザー権限のスケジューラーを使います。macOSはLaunchAgent、Linuxはsystemd user timer、WindowsはTask Schedulerに登録します。Linuxではsystemd user managerが起動している必要があり、利用できない場合はエラーになります。cronへの切り替えは行いません。登録後のDB保存先はインストール時に解決した絶対パスへ固定します。statusとuninstallはCodexを起動しません。

macOSのLaunchAgentはユーザーのログインセッション中に動作し、ログアウト中は収集しません。スリープ中は実行されず、復帰後は逃した複数回分を再生せず、必要に応じて1回実行します。Windowsのタスクは「ユーザーがログオンしている場合のみ」動作し、ログアウト中は収集しません。スリープからPCを起こす設定はなく、逃した実行はスケジューラーが利用可能になった後に開始される場合があります。Linuxの`Persistent=true`はsystemd user timerが停止していた間の実行を、timer再開後に補足します。

macOSとWindowsの標準出力・標準エラーはユーザーデータ領域の`logs/scheduled-collect.log`へ追記します。Linuxではsystemd journalに記録します。登録情報と所有manifestが一致しない場合やmanifestが欠落・改変された場合、既存jobの更新・削除は拒否します。
