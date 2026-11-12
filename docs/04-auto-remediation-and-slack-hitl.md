# 第 4 章：ADK 2.x ワークフロー・型付き自動復旧・Slack 連携・Error Budget 効果計測 (`04-auto-remediation-and-slack-hitl.md`)

**所要時間**: 約 45 分  
**到達目標**: ADK Python 2.x のグラフワークフロー（`Workflow` / `JoinNode` / `@node(require_confirmation=True)`）による 3 分岐制御、型付き GKE 復旧と事後検証、公開 URL ゼロの Slack Socket Mode 双方向承認、Runbook 鮮度ライフサイクル、および **Saved Error Budget（温存エラーバジェット）算出ロジック**を理解し、通知と承認の経路を準備する。Slack を使えない場合は、メール通知と承認コマンドの代替ルートを選べる。

---

## 1. この章で確認・設定する場所（現在地マップ）

**ADK 2.x グラフ制御層**、**型付き GKE 復旧・SLO 効果算出層**、および **Slack Socket Mode 連携層** を確認・設定します。

![第 4 章で実装する ADK 2.x ワークフロー・型付き復旧・Slack HITL 連携図](images/04-01-chapter4-scope.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
flowchart LR
    subgraph Prev["第 1〜3 章で構築・確認済み"]
        Diag["Phase 1 補完・並列診断層<br/>・telemetry_enrichment.py<br/>・公式リモート MCP 読取<br/>・Cloud Run sandbox<br/>・BigQuery 100件照合"]
    end

    subgraph Now["本章の対象: 意思決定・安全復旧・Slack 双方向 HITL"]
        WF["workflow.py / slo_metrics.py<br/>ADK 2.x Workflow & Saved Budget 算出"]
        Rem["remediation_tools.py<br/>型付き Rollback / Restart + 事後検証"]
        SlackMod["slack_socket.py / ticket_store.py<br/>Socket Mode 外向き WSS"]
        DB["Firestore 本番 / SQLite 単体<br/>ADK セッション + 承認チケット永続化"]
        WF --> Rem
        WF <--> SlackMod
        WF <--> DB
    end

    subgraph Target["監視対象 GKE & Slack"]
        GKE["order-service<br/>Namespace: sre-demo-app"]
        SlackUI["Slack チャンネル"]
    end

    Diag --> WF
    Rem -->|"AppsV1 API"| GKE
    SlackMod <-->|"Outbound TLS 443"| SlackUI

    classDef done fill:#e6f4ea,stroke:#137333,stroke-width:2px,color:#202124;
    classDef cur fill:#ffffff,stroke:#1a73e8,stroke-width:2px,color:#202124;
    class Diag done;
    class WF,Rem,SlackMod,DB,GKE,SlackUI cur;
```

</details>

---

## 2. ADK 2.x グラフワークフローと 5 重の安全ガードレール (`workflow.py`)

手順の進行をすべて LLM の自由裁量に任せると手順飛ばしや再起動ループが起きるため、[sre-agent/agent_app/workflow.py](../sre-agent/agent_app/workflow.py) では ADK 2.x の `Workflow` で実行順序と分岐条件を有向グラフとして明示固定しています。

![ADK 2.x Workflow・JoinNode・require_confirmation によるインシデント処理グラフ構造図](images/04-02-adk-workflow-graph.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
flowchart TD
    Pre["前処理: CloudTelemetryEnricher.enrich_alert<br/>ログ時系列エラー率・直近デプロイ有無を補完"] --> S(["START"])
    S --> P1["@node: analyze_metrics_in_sandbox<br/>隔離空間でスロープ計算"]
    S --> P2["@node: search_similar_incidents<br/>BigQuery AI.SEARCH 上位3件照合"]
    P1 & P2 --> J["JoinNode: join_diagnostics<br/>両方の診断完了を待機・合流"]
    J --> LLM["@node: correlate_and_decide<br/>gemini-3.8-flash + 公式リモート MCP<br/>TriageDecision 構造化出力"]
    LLM --> Router["@node: enforce_guardrails_and_route<br/>許可NS・類似度・鮮度90日・1回/h検査"]

    Router -->|"route = OBSERVE"| N1["@node: observe_node<br/>変更操作なし・静観記録"]
    Router -->|"route = AUTO_REMEDIATE"| N2["@node: auto_remediate_node<br/>即時復旧 + 回復確認・鮮度更新 / NG時降格"]
    Router -->|"route = REQUIRE_APPROVAL"| N3["@node: approved_remediate_node<br/>require_confirmation=True で自動一時停止"]

    N3 -.->|"Slack で Approve 押下"| Exec["Firestore 復元・処置実行<br/>初見は AUTO-* 登録 / 既知は鮮度更新"]
    N3 -.->|"Slack で Reject 押下"| Cancel["実行スキップ・手動引継・承認制へ降格"]

    classDef nodeBox fill:#ffffff,stroke:#4285F4,stroke-width:1.5px,color:#202124;
    classDef ok fill:#e6f4ea,stroke:#137333,stroke-width:2px,color:#202124;
    classDef auto fill:#e8f0fe,stroke:#1967d2,stroke-width:2px,color:#202124;
    classDef hitl fill:#fef7e0,stroke:#b06000,stroke-width:2px,color:#202124;
    classDef reject fill:#fce8e6,stroke:#c5221f,stroke-width:2px,color:#202124;
    class Pre,S,P1,P2,J,LLM,Router nodeBox;
    class N1,Exec ok;
    class N2 auto;
    class N3 hitl;
    class Cancel reject;
```

</details>

### 安全ガードレールによる自動降格と事後回復プローブ

LLM が `AUTO_REMEDIATE` を提案しても、`enforce_guardrails_and_route` が以下の 5 条件を決定論的に検証し、1 つでも外れれば `REQUIRE_APPROVAL`（人間承認待ち）へ強制降格します。

1. **許可 Namespace 境界**: `ALLOWED_K8S_NAMESPACES`（既定：`sre-demo-app`）以外は変更不可。
2. **類似度下限**: コサイン類似度が `AUTO_REMEDIATE_SIMILARITY_THRESHOLD`（既定：`0.85`）未満なら降格。
3. **型付き操作限定**: [remediation_tools.py](../sre-agent/agent_app/remediation_tools.py) の `rollback_deployment` と `restart_deployment` 以外は自動実行不可。
4. **Runbook 鮮度有効期限**: 最終検証日（`last_verified_at`）から `RUNBOOK_FRESHNESS_TTL_DAYS`（既定：`90` 日）を超過した手順は再評価のため降格。
5. **1 時間あたり回数上限**: `MAX_AUTO_ACTIONS_PER_HOUR`（既定：`1` 回）を超える連続実行を遮断。

![安全ガードレールによる承認待機への自動降格条件と事後エラー率検証フロー図](images/04-03-guardrail-downgrade-and-probe.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
flowchart LR
    Start["LLM が AUTO_REMEDIATE を提案<br/>ROLLBACK_DEPLOYMENT / RESTART_DEPLOYMENT"]

    Start -->|"対象が許可 Namespace 外"| Downgrade["REQUIRE_APPROVAL へ強制降格<br/>Slack 承認ボタン待ちへ移行"]
    Start -->|"類似度スコア 0.85 未満"| Downgrade
    Start -->|"最終検証から 90 日超過"| Downgrade
    Start -->|"1時間以内に自動復旧実施済み"| Downgrade
    Start -->|"全 4 条件をクリア"| Exec["型付き Kubernetes AppsV1 API 実行<br/>Deployment Rollback / Restart"]

    Exec --> Probe{"事後回復プローブ検証<br/>直近エラー率を再計測"}
    Probe -->|"エラー率 < 1.0%"| Resolved["自動復旧完了<br/>last_verified_at 更新 + Slack 完了通知"]
    Probe -->|"エラー率 >= 1.0%"| Escalate["要オンコール対応通知<br/>Runbook を REQUIRE_APPROVAL へ自動降格"]

    classDef prop fill:#fef7e0,stroke:#b06000,stroke-width:2px,color:#202124;
    classDef act fill:#e8f0fe,stroke:#1967d2,stroke-width:2px,color:#202124;
    classDef ok fill:#e6f4ea,stroke:#137333,stroke-width:2px,color:#202124;
    classDef ng fill:#fce8e6,stroke:#c5221f,stroke-width:2px,color:#202124;
    class Start,Downgrade prop;
    class Exec,Probe act;
    class Resolved ok;
    class Escalate ng;
```

</details>

さらに処置後は `SafeRemediationService.execute_and_verify` が新リビジョン Ready 到達後のエラー率を実測し、**`1.0%` 未満への回復を確認できた場合のみ成功と判定**します。未回復時は連鎖操作を行わず、該当 Runbook を承認制へ自動降格したうえでオンコール担当者へ警告通知します。

---

## 3. Error Budget 温存効果（Saved Error Budget）の定量算出 (`slo_metrics.py`)

[sre-agent/agent_app/slo_metrics.py](../sre-agent/agent_app/slo_metrics.py) は、異常ログ発生時刻（`alert.observed_at`）から事後回復プローブ完了時刻までの実測所要時間 $\text{MTTR}_{\text{actual}}$（分）を計測し、Cloud Monitoring API から現在の日次 99.0% / 月次 99.9% SLO 残量を読み取って Slack 完了カードと BigQuery `incident_events` へ記録します。

$$\text{Saved Budget (min)} = \max\left(0,\; \text{MTTR}_{\text{manual}} - \text{MTTR}_{\text{actual}}\right), \qquad \text{Monthly 99.9\% Saved (\%)} = \frac{\text{Saved Budget (min)}}{43.2} \times 100$$

* **誠実な計上ルール**: `Saved Budget` と実測 `mttr_seconds` は、**型付き復旧（Rollback / Restart）を実行して事後エラー率 `< 1.0%` への回復が実証されたケースのみ計上**します。一過性エラーの静観（`OBSERVE`）や、オンコール担当者へ手動調査を引き継いだケース（`MANUAL_INTERVENTION`）では、架空の短縮分数を計上せず二次障害回避として区別記録します。

---

## 4. Slack Socket Mode・即時 UX 更新・Runbook 鮮度ライフサイクル

常駐 Cloud Run instance から **Slack Socket Mode（外向き TLS WebSocket）** で接続することで公開 URL をゼロにし、中断セッションと承認チケットを **Cloud Firestore** に保存してインスタンス再起動耐性を確保します。また、担当者がボタンを押した直後（0.5 秒以内）にボタンを消去して **`⏳ 承認受付・実行中...`** へ切り替える 2 段階更新により、GKE ロールアウト待機中の二度押しを防ぎます。

![Slack Socket Mode と Cloud Firestore による一時停止・承認再開シーケンス図](images/04-04-slack-hitl-resume-sequence.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
sequenceDiagram
    autonumber
    participant WF as ADK 2.x Workflow<br/>Cloud Run instance 内
    participant DB as Firestore & BigQuery<br/>状態永続化 & 知識ベース
    participant Bolt as SlackHitlCoordinator<br/>Outbound WebSocket
    participant Slack as Slack チャンネル<br/>オンコール担当者
    participant GKE as 監視対象 GKE<br/>sre-demo-app

    rect rgb(232, 240, 254)
        Note over WF,Slack: フェーズ 1: 自動一時停止と状態永続化 — 公開受信 URL なしで承認依頼を送信
        WF->>DB: approved_remediate_node 到達時に中断セッション保存
        WF-->>Bolt: WorkflowRunResult 返却 paused_for_approval=True
        Bolt->>DB: 承認待ちチケット保存 sre_pending_approval_tickets
        Bolt->>Slack: Block Kit 調査レポート + Approve / Reject ボタン投稿
    end

    rect rgb(254, 247, 224)
        Note over WF,Slack: 待機中も着信 HTTP ポート不要・instance 再起動時も Firestore 上の状態を維持
    end

    rect rgb(230, 244, 234)
        Note over WF,GKE: フェーズ 2: WebSocket 承認受信・即時実行中表示・ADK 再開・自律ナレッジ / 鮮度更新
        Slack-->>Bolt: 担当者が Approve / Reject をクリックし WebSocket 経由で受信
        Bolt->>Bolt: 押下者の Slack User ID が許可リスト内か検証
        Bolt->>DB: Firestore トランザクションでチケット取得・ロック
        Bolt->>Slack: 即時 chat.update — ボタン消去し「承認受付・実行中」表示へ切替
        Bolt->>WF: resume_from_approval confirmed=True/False, invocation_id=...
        WF->>DB: 中断箇所から ADK セッション復元
        WF->>GKE: Approve 時は型付き復旧 + 事後検証を実行
        WF->>DB: 初見時 AUTO-* 登録 / 再承認時 last_verified_at 更新 / 却下・未回復時 降格
        WF-->>Bolt: RemediationOutcome 返却 learned / refreshed / demoted ID 付き
        Bolt->>DB: 完了済み承認チケット削除
        Bolt->>Slack: 元カードを最終結果「承認・実行完了 + 知識/鮮度更新」へ更新
    end
```

</details>

### 自律ナレッジ昇格と鮮度ライフサイクル遷移表

| 判定・処置ケース | `incident_runbooks` の更新 | 次回発生時の挙動 |
|---|---|---|
| **初見（類似度 `< 0.85`）で `Approve` 後に型付き復旧で回復確認** | 新規 `AUTO-*` を `MERGE` 登録・`verified_count = 1` | **`AUTO_REMEDIATE` へ昇格**［今後 90 日間有効］ |
| **初見で `Approve` されたが `MANUAL_INTERVENTION` 種別** | 新規 `AUTO-*` を `MERGE` 登録 | **`REQUIRE_APPROVAL` を維持**［DB 等の無断操作を抑止］ |
| **鮮度内の自動復旧成功、または 90 日超過手順の再 `Approve` 回復成功** | `last_verified_at = NOW`・`verified_count + 1`［ベクトル再計算なし］ | **さらに 90 日間 `AUTO_REMEDIATE` 資格を継続・復活** |
| **既存 `AUTO_REMEDIATE` 手順が `Reject` された、または事後検証 NG** | `recommended_action = 'REQUIRE_APPROVAL'` へ更新 | **以後の無断自動実行を恒久凍結**［毎回承認を要求］ |

---

## 5. 通知と承認の経路の準備

エージェントは判定のたびに通知を送り、承認待ちのときだけ人の操作を待ちます。経路は次の 2 つで、片方だけでも、両方を同時にでも使えます。

| 経路 | 通知の届き方 | 承認と却下の操作 | 準備手順 |
|---|---|---|---|
| **標準: Slack** | Slack チャンネルへの投稿 | 投稿されたカードのボタン | 5.1 |
| **代替: メール通知と承認コマンド** | エージェントが送るメール | Cloud Shell またはローカル PC のターミナル | 5.2 |

Slack ワークスペースへアプリを追加できる場合は 5.1 を、追加できない場合は 5.2 を実施します。どちらも設定しない場合は、通知の内容が Cloud Logging へ出力されるだけで、訓練 3 と訓練 4 の承認操作はできません。

### 5.1 標準ルート：Slack App の作成と Secret Manager へのトークン登録

以下の App Manifest で Socket Mode 対応アプリを作成します。

1. [Slack API: Your Apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest** で以下を貼り付けます。

```yaml
display_information:
  name: Autonomous SRE Agent
  description: Cloud Run instances + ADK 2.x Autonomous SRE Agent
features:
  bot_user:
    display_name: sre-agent
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write
      - chat:write.public
settings:
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

2. **Basic Information** → **App-Level Tokens** で `connections:write` スコープのトークン（`xapp-...`）を発行し、**Install to Workspace** で Bot Token（`xoxb-...`）を取得します。
3. 対象チャンネルで `/invite @sre-agent` を実行し、チャンネル ID（`C...`）を `.env` の `SLACK_ALERT_CHANNEL_ID` に、ご自身のメンバー ID（`U...`）を `SLACK_ALLOWED_APPROVER_IDS` に設定してから Secret Manager へ格納します。

```bash
source .env

read -rsp "Slack Bot Token (xoxb-...) を入力: " INPUT_BOT_TOKEN && echo ""
printf "%s" "${INPUT_BOT_TOKEN}" | gcloud secrets create sre-slack-bot-token \
  --data-file=- \
  --replication-policy="automatic" \
  --project="${GOOGLE_CLOUD_PROJECT}"
unset INPUT_BOT_TOKEN

read -rsp "Slack App Token (xapp-...) を入力: " INPUT_APP_TOKEN && echo ""
printf "%s" "${INPUT_APP_TOKEN}" | gcloud secrets create sre-slack-app-token \
  --data-file=- \
  --replication-policy="automatic" \
  --project="${GOOGLE_CLOUD_PROJECT}"
unset INPUT_APP_TOKEN
```

### 5.2 代替ルート：メール通知と承認コマンド

エージェントは、Slack へ通知するのと同じ箇所から **Application Integration** のメール送信タスクを実行して通知メールを送ります。承認と却下だけを、ターミナルの承認コマンド [approve_incident.py](../target-app/scripts/approve_incident.py) で行います。外部サービスのアカウントは不要で、エージェントの着信 URL も開けません。

![メール通知と承認コマンドによる一時停止・承認再開シーケンス図](images/04-05-email-approval-command-sequence.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
sequenceDiagram
    autonumber
    participant Agent as SRE エージェント<br/>Cloud Run instance
    participant FS as Cloud Firestore<br/>承認待ちチケット
    participant AI as Application Integration<br/>メール送信用の連携
    participant Human as 担当者<br/>メールとターミナル
    participant Cmd as 承認コマンド<br/>approve_incident.py
    participant PS as Pub/Sub<br/>承認専用トピック
    participant GKE as 監視対象 GKE<br/>sre-demo-app

    rect rgb(232, 240, 254)
        Note over Agent,Human: フェーズ 1: 一時停止と通知 — Slack へ通知するのと同じ箇所からメールも送る
        Agent->>FS: 承認待ちチケットを保存
        Agent->>AI: 件名と本文を渡して連携を実行
        AI->>Human: 承認待ちのメール — 宛先は連携の定義に固定
    end

    rect rgb(254, 247, 224)
        Note over Human,PS: フェーズ 2: 担当者がターミナルで判断 — コマンドは発行したら終了する
        Human->>Cmd: メールに書かれたコマンドを実行
        Cmd->>FS: チケットを 1 回だけ読み取り
        Cmd-->>Human: 判定内容を表示
        Human->>Cmd: approve か reject を入力
        Cmd->>PS: 承認または却下を発行
    end

    rect rgb(230, 244, 234)
        Note over Agent,GKE: フェーズ 3: 再開と結果の通知 — エージェントは外向き接続だけで受信する
        PS-->>Agent: StreamingPull で受信し、形式と発行時刻を検証
        Agent->>FS: チケットを取得して ADK セッションを再開
        Agent->>GKE: 承認された処置を実行し、実行後のエラー率を確認
        Agent->>AI: 結果の件名と本文を渡して連携を実行
        AI->>Human: 承認結果のメール — 読むだけで完了
    end
```

</details>

メールが届いた後に必要な操作は、通知の種類で決まります。

| 通知の種類 | メールが届いた後の操作 |
|---|---|
| 静観の報告 | なし |
| 自動復旧の結果 | なし |
| 承認待ち | メールに書かれたコマンドを実行し、判定内容を確認して `approve` か `reject` を入力 |
| 承認結果 | なし |

Slack は承認待ちのカードを結果へ書き換えますが、メールは書き換えられないため、承認待ちと承認結果は別のメールで届きます。

#### セットアップ

API を有効化し、承認専用の Pub/Sub トピックとサブスクリプションを作成します。

```bash
source .env

gcloud services enable integrations.googleapis.com --project="${GOOGLE_CLOUD_PROJECT}"

gcloud pubsub topics create sre-approval-decisions --project="${GOOGLE_CLOUD_PROJECT}"

gcloud pubsub subscriptions create sre-approval-decisions-sub \
  --topic=sre-approval-decisions \
  --ack-deadline=60 \
  --message-retention-duration=10m \
  --project="${GOOGLE_CLOUD_PROJECT}"
```

`.env` の次の 3 項目に値を設定します。`NOTIFY_EMAIL_TO` には通知を受け取るメールアドレスを書きます。

```bash
EMAIL_NOTIFY_INTEGRATION="sre-notify-email"
NOTIFY_EMAIL_TO="you@example.com"
APPROVAL_PUBSUB_SUBSCRIPTION="sre-approval-decisions-sub"
```

メール送信用の連携を登録し、エージェントのサービスアカウントへ連携の実行権限を付与します。

```bash
source .env
source .venv/bin/activate

python3 ./target-app/scripts/setup_email_notification.py --send-test

gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
  --member="serviceAccount:sre-autonomous-agent-sa@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com" \
  --role="roles/integrations.integrationInvoker" \
  --condition=None \
  --quiet
```

件名 `[SRE Agent] テスト: 通知メールの設定確認` のメールが届けば準備完了です。[setup_email_notification.py](../target-app/scripts/setup_email_notification.py) は次のことを行います。

* **リージョンの有効化**: 初回だけ行います。このとき Google Cloud が、サービス エージェントへのロール付与と `ExecuteConnection` という連携の作成を自動で行います。
* **連携の登録と公開**: 登録済みの定義と同じなら何もしないため、再実行しても重複しません。宛先を変える場合は `NOTIFY_EMAIL_TO` を書き換えて再実行します。
* **送信上限の表示**: メールの 1 日あたりの送信上限はプロジェクトによって異なります。訓練 1 周で送るメールは 9 通です。

#### 権限の境界と失敗時の扱い

* **宛先は連携の定義に固定**: エージェントが渡すのは件名と本文だけです。エージェントが誤動作しても、決めた宛先以外へは送れません。
* **承認専用トピックを分ける理由**: アラート用の `sre-alert-stream` には監視対象アプリのログが流れます。同じトピックで承認を受け付けると、監視対象アプリがログを 1 行出すだけで承認を偽装できてしまいます。
* **承認できる人**: 承認専用トピックへの発行権限 `roles/pubsub.publisher` を持つ人です。Pub/Sub は発行者を証明しないため、コマンドが記録する操作者名は自己申告の監査記録として扱います。
* **承認は 1 チケット 1 回限り**: 形式が違うイベント、発行から 5 分を超えたイベント、処理済みのチケットへの承認は実行しません。再開に失敗した場合は自動で再試行せず、チケットを承認待ちへ戻して、エラーの内容を添えた承認待ちメールを送り直します。
* **通知が失敗した場合**: Slack とメールは送信先ごとに独立して送ります。失敗はエラーログ `event="notification_failed"` に記録するだけで、判定・復旧・承認の処理は止めず、再送もしません。承認待ちチケットは通知より先に保存されるため、メールが届かなくても、引数なしの承認コマンドで一覧を確認できます。
* **検知できる失敗の範囲**: メールの失敗として検知できるのは、API がエラーを返した場合だけです。API が受け付けた後に届かなかった場合は、エージェントからは分かりません。
* **エージェントの実行権限の範囲**: `roles/integrations.integrationInvoker` はプロジェクト内のすべての連携を実行できます。対象を 1 つの連携に絞る場合は IAM Conditions を使えます（本教材では未検証）。

本教材の検証は、組織に属するプロジェクトで行いました。組織に属さないプロジェクトでの動作は未確認です。

---

## 6. 全テストスイート（51 件）のローカル実行確認

ワークフロー 3 分岐、Slack 再開・即時状態更新、鮮度ライフサイクル、Error Budget 温存計算、メール通知と承認コマンドを含む全 51 件のテストを実行します。

```bash
source .venv/bin/activate
pytest -v
```

### 期待される出力（全 51 件 PASS）

```text
tests/test_dedup_and_guardrails.py ........... (6 passed)
tests/test_email_and_approval_command.py ..... (25 passed)
tests/test_sandbox_runner.py ................. (4 passed)
tests/test_slack_hitl_resume.py .............. (11 passed)
tests/test_slo_metrics.py .................... (4 passed)
tests/test_workflow_routing.py ............... (1 passed)
============================== 51 passed ==============================
```

---

## 🤖 AI コーディングエージェントを使う場合の指示プロンプト

```markdown
`AGENTS.md` を読み込み、第4章（`docs/04-auto-remediation-and-slack-hitl.md`）のテスト検証を実行してください。
- `.venv` 環境で `pytest -v` を実行し、全 51 件のテスト（重複排除、サンドボックス隔離、Workflow 3分岐、Slack 再開と即時状態更新、自律ナレッジ昇格、鮮度管理、Error Budget 温存計算、メール通知と承認コマンド）が PASS することを確認して報告すること。
- Secret Manager への Slack トークン登録は私が手元で入力するため、コマンドのみ案内すること。
- 代替ルートを使う場合、API 有効化・Pub/Sub 作成・`setup_email_notification.py`・IAM 付与は、実行前に提示して私の `y` 承認を得てから実行すること。
```

---

## Links and References

* [Human input for agent workflows (ADK)](https://adk.dev/graphs/human-input/) — ADK 2.x グラフワークフローにおける Human-in-the-Loop 一時停止・再開仕様
* [Retrieving SLO data with time-series selectors](https://docs.cloud.google.com/stackdriver/docs/solutions/slo-monitoring/api/timeseries-selectors) — `select_slo_budget_fraction`・`select_slo_burn_rate` の公式仕様
* [Slack Socket Mode documentation](https://api.slack.com/apis/connections/socket) — 外向き WebSocket による Interactivity 受信仕様
* [Secret Manager overview](https://docs.cloud.google.com/secret-manager/docs/overview) — シークレットの安全な管理と Cloud Run 連携
* [Send Email task (Application Integration)](https://docs.cloud.google.com/application-integration/docs/configure-send-email-task) — メール送信タスクの宛先・件名・本文の設定
* [API trigger (Application Integration)](https://docs.cloud.google.com/application-integration/docs/configure-api-trigger) — アプリケーションから連携を実行する方法
* [Add IAM conditions (Application Integration)](https://docs.cloud.google.com/application-integration/docs/add-iam-conditions) — 連携ごとにアクセスを絞る IAM Conditions の設定

次へ進む 👉 [第 5 章：閉域常駐デプロイ・4 大障害訓練・Error Budget 温存効果の実測](05-deploy-cloud-run-instance-and-drills.md)
