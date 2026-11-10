# Agentic SRE ハンズオン — Cloud Run instances × GKE による常駐自律型システム運用エージェント

Cloud Monitoring や Cloud Logging のイベントストリームを 24 時間 365 日常時待ち受け、異常検知時に**「マルチシグナル相関分析」「隔離サンドボックスでの統計解析」「過去 100 件のインシデント履歴（ベクトル検索）との照合」**を自律実行し、**静観・GKE 自動復旧・Slack Interactive 承認連携**を安全に使い分ける本番志向の Agentic SRE ハンズオンリポジトリです。

---

## 1. 全体アーキテクチャと設計の特徴

監視対象の GKE クラスタが障害に陥っても監視エージェント自体が共倒れしないよう、常駐 SRE エージェントは別基盤の **Cloud Run instances（＋ Cloud Run sandboxes）** 上で稼働させます（Out-of-Band 監視）。また、外部からの着信エンドポイントを完全に無効化（`--ingress internal --no-default-url`）したまま、外向き通信のみで Pub/Sub 購読と双方向 Slack 承認ボタンを実現します。

```mermaid
flowchart TB
    subgraph TargetGKE["監視対象環境: GKE クラスタ (sre-demo-app)"]
        OrderApp["注文 API (order-service)<br/>v1 正常版 / v2 バグ版"]
    end

    subgraph StreamLayer["イベント駆動ストリーム層"]
        CloudObs["Cloud Monitoring アラート<br/>& Cloud Logging シンク"]
        PubSub["Cloud Pub/Sub<br/>(sre-alert-stream-sub)"]
        CloudObs --> PubSub
    end

    subgraph AgentInstance["常駐エージェント基盤: Cloud Run instances (--ingress internal --no-default-url)"]
        Dedup["① 決定論的プリフィルタ<br/>Fingerprint 重複排除・15分クールダウン"]
        Orch["② ADK 2.x Workflow<br/>(gemini-3.8-flash)"]
        Sandbox["③ Cloud Run sandboxes<br/>(/usr/local/gcp/bin/sandbox do)<br/>外向き通信遮断・時系列減衰解析"]
        Guard["④ 安全ガードレール<br/>許可 Namespace・類似度下限・1時間1回上限"]

        Dedup -->|"新規事象のみ起動"| Orch
        Orch <-->|"隔離 Python 実行"| Sandbox
        Orch --> Guard
    end

    subgraph KnowledgeAndTools["知識ベース・状態管理・観測ツール群"]
        RemoteMCP["Google Cloud 公式リモート MCP<br/>Monitoring / Logging / GKE (Read-Only)"]
        BQ["BigQuery (sre_agent_ops)<br/>・過去インシデント 100 件 (VECTOR_SEARCH)<br/>・gemini-embedding-2<br/>・追記型監査テーブル (incident_events)"]
        FS["Cloud Firestore (Native)<br/>・ADK セッション中断状態<br/>・Slack 承認待ちチケット永続化"]
    end

    subgraph ActionsAndSlack["3分岐アクション & Slack Socket Mode"]
        R1["🟢 ルート A: 静観 (OBSERVE)<br/>一過性エラーは再起動せず経過監視"]
        R2["🛠️ ルート B: 自動復旧 (AUTO_REMEDIATE)<br/>型付き API で Rollback / Restart + 回復検証"]
        R3["🟠 ルート C: 人間承認 (REQUIRE_APPROVAL)<br/>ADK 一時停止 → Approve 押下で再開"]
        Slack["Slack (Socket Mode 外向き WSS)<br/>事後レポート & Approve / Reject ボタン"]
    end

    OrderApp --> CloudObs
    PubSub -->|"外向き StreamingPull"| Dedup
    Orch <-->|"tool_filter 厳選"| RemoteMCP
    Orch <-->|"類似事例照合"| BQ
    Orch <-->|"セッション・承認状態"| FS
    Dedup <-->|"クールダウン確認・記録"| BQ
    Guard --> R1 & R2 & R3
    R2 -->|"AppsV1 API 最小権限"| OrderApp
    R3 <-->|"Outbound WebSocket"| Slack
    R3 -->|"承認時のみ実行"| OrderApp
    R1 & R2 --> Slack
```

### おもちゃのデモと本リポジトリの設計の違い

| 観点 | 一般的なデモ実装の課題 | 本ハンズオンの本番設計パターン |
|---|---|---|
| **常時監視コスト** | 毎分 LLM に生ログを送りトークン費用が破綻する | Cloud Monitoring / Log Sink で異常時のみ Pub/Sub 発火し、コード側のクールダウン判定で重複呼び出しを排除 |
| **一過性エラー対応** | 通信瞬断でも毎回 Pod を再起動し、再接続ストームで悪化させる | 隔離サンドボックスでエラー率の減衰傾きを計算し、過去履歴と照合して**「何もしない（静観）」**を自律選択 |
| **変更操作の安全性** | LLM に自由な `kubectl` シェルコマンドを生成させクラスタ破壊リスクがある | 読取は公式リモート MCP（Read-Only）、変更は許可 Namespace 固定・1 時間 1 回上限・事後回復確認付きの型付き関数に分離 |
| **承認コールバック & 再開耐性** | Slack Webhook 受信用に未認証公開 URL が必要、また承認待ち中のコンテナ再起動で状態が消える | 常駐 instance から **Slack Socket Mode** で外向き接続し、ADK セッション＋承認チケットを **Firestore** へ永続化して再起動後も再開可能 |

---

## 2. ハンズオンカリキュラム（所要時間: 約 3.5〜4 時間）

上から順番に進めることで、ゼロから全コンポーネントを構築し、3 つの実戦障害シナリオで動作を検証できます。各章には **Cloud Shell 用の手順** と **AI コーディングエージェント（Google Antigravity / Claude Code / Gemini CLI）用の指示プロンプト** の両方を記載しています。

| 章 | ドキュメント | 所要時間 | 構築・検証する内容 |
|---|---|---|---|
| **00** | [00-overview-and-architecture.md](docs/00-overview-and-architecture.md) | 20 分 | 全体設計、できること・不向きなことの境界、GCP API・IAM 事前準備 |
| **01** | [01-target-gke-and-alerting.md](docs/01-target-gke-and-alerting.md) | 35 分 | 監視対象 GKE アプリ（`order-service`）構築、最小権限 RBAC、Logging シンク → Pub/Sub 連携 |
| **02** | [02-bigquery-vector-kb.md](docs/02-bigquery-vector-kb.md) | 30 分 | BigQuery スキーマ作成、本番想定の過去インシデント **100 件** 投入、`gemini-embedding-2` 一括ベクトル化と類似検索 |
| **03** | [03-diagnostic-agent-and-sandbox.md](docs/03-diagnostic-agent-and-sandbox.md) | 40 分 | 公式リモート MCP 並列調査と、Cloud Run sandboxes（`sandbox do`）による隔離データ解析の実装 |
| **04** | [04-auto-remediation-and-slack-hitl.md](docs/04-auto-remediation-and-slack-hitl.md) | 45 分 | ADK 2.x `Workflow`、型付き GKE 復旧＋事後検証、Slack Socket Mode による一時停止・承認再開の実装 |
| **05** | [05-deploy-cloud-run-instance-and-drills.md](docs/05-deploy-cloud-run-instance-and-drills.md) | 45 分 | Cloud Run instance（`internal` + URL 無効化）への常駐デプロイと、3 大障害シナリオ訓練 |
| **06** | [06-optional-hardening-and-cleanup.md](docs/06-optional-hardening-and-cleanup.md) | 25 分 | 【任意】GKE Sandbox（`microVM`）構成、Model Armor ログ攻撃遮断、`pass^3` 信頼性評価、クリーンアップ手順 |

---

## 3. ローカル環境でのクイックテスト（クラウド課金 \$0）

Google Cloud へデプロイする前に、手元の Python 3.11+ 仮想環境で 100 件知識ベース検索・ADK 2.x ワークフロー 3 分岐・Slack 承認再開ロジックの単体/結合テストを実行できます。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

---

## 4. ディレクトリ構成

```text
.
├── AGENTS.md                        # AI コーディングエージェント用の安全・設計ガードレール
├── .env.example                     # 環境変数テンプレート
├── pyproject.toml                   # Python パッケージ・テスト設定
├── target-app/                      # 監視対象 GKE マイクロサービス & 3 種障害注入スクリプト
├── bigquery/                        # BigQuery DDL・リモートモデル・本番想定インシデント 100 件 JSONL
├── sre-agent/                       # Cloud Run instances 常駐エージェント本体 (ADK 2.x)
├── appendix/                        # 付録: GKE Standard microVM マニフェスト / Model Armor / pass^3 評価
├── tests/                           # ローカル自動テストスイート
└── docs/                            # 全 7 章のステップバイステップ Codelab 教材
```

---

## Links and References

* [Create and manage Cloud Run instances](https://docs.cloud.google.com/run/docs/instances/create-and-manage-instances) — 常駐シングルトン実行環境 Cloud Run instances のライフサイクルと対応リージョン
* [Configure sandboxes for instances (Cloud Run)](https://docs.cloud.google.com/run/docs/configuring/instances/sandboxes) — Cloud Run instances における隔離サンドボックスの有効化手順
* [Code execution in Cloud Run](https://docs.cloud.google.com/run/docs/code-execution) — `/usr/local/gcp/bin/sandbox` CLI の実行方法とセキュリティ境界
* [Isolate workloads by using GKE Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods) — GKE Sandbox（`microVM` / `gVisor`）の設定ガイド
* [Use the GKE, Cloud Monitoring, and Cloud Logging MCP servers](https://docs.cloud.google.com/mcp/overview) — Google Cloud リモート MCP サーバーの概要と IAM 設定
* [Agent Development Kit (ADK) Documentation](https://adk.dev/) — ADK Python 2.x のグラフワークフローと Human-in-the-Loop 仕様
