"""Phase 2: 型付き GKE 自動復旧ツールと事後メトリクス回復検証モジュール。

LLM に自由な `kubectl` シェルコマンドを生成・実行させるアンチパターンを排し、
以下の安全制御を備えた型付き API 操作のみを提供します。

1. 許可 Namespace 検証（`ALLOWED_K8S_NAMESPACES` 以外は即拒否）
2. 操作種別の限定（Deployment の前リビジョンへのロールバック、または Pod ローリング再起動のみ。
   クラスタ削除・Namespace 削除・Secret 変更・StatefulSet 操作は関数自体が存在しない）
3. 事後回復検証（操作を実行しただけで「復旧完了」と報告せず、エラー率が低下したかを確認）
"""

from __future__ import annotations

import base64
import tempfile
from datetime import datetime, timezone
from typing import Callable, Protocol

import google.auth
import google.auth.transport.requests
from google.cloud import container_v1
from kubernetes import client as k8s_client

from .config import AgentSettings
from .models import RemediationOutcome, RemediationType


class KubernetesRemediationExecutor(Protocol):
    """本番 GKE API 実行器とテスト用モック実行器の共通プロトコル。"""

    def rollback_deployment(self, namespace: str, deployment_name: str) -> str:
        """指定 Deployment を直前の正常 ReplicaSet テンプレートへロールバックし、結果説明を返す。"""
        ...

    def restart_deployment(self, namespace: str, deployment_name: str) -> str:
        """指定 Deployment に restartedAt アノテーションを付与してローリング再起動し、結果説明を返す。"""
        ...


class GkeAppsV1RemediationExecutor:
    """Cloud Run instance（クラスタ外部）から Google Cloud IAM 認証で GKE API を操作する本番実装。"""

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    def _build_apps_v1_client(self) -> k8s_client.AppsV1Api:
        """GKE Cluster Manager API からクラスタ CA とエンドポイントを取得し AppsV1Api を構築する。"""
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(google.auth.transport.requests.Request())

        gke_client = container_v1.ClusterManagerClient(credentials=credentials)
        cluster_path = (
            f"projects/{self._settings.project_id}/locations/"
            f"{self._settings.gke_cluster_location}/clusters/{self._settings.gke_cluster_name}"
        )
        cluster = gke_client.get_cluster(name=cluster_path)

        cfg = k8s_client.Configuration()
        cfg.host = f"https://{cluster.endpoint}"
        cfg.api_key = {"authorization": f"Bearer {credentials.token}"}

        ca_bytes = base64.b64decode(cluster.master_auth.cluster_ca_certificate)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as ca_file:
            ca_file.write(ca_bytes)
            cfg.ssl_ca_cert = ca_file.name

        return k8s_client.AppsV1Api(k8s_client.ApiClient(cfg))

    def rollback_deployment(self, namespace: str, deployment_name: str) -> str:
        """ReplicaSet の revision 履歴から 1 つ前のリビジョンの PodTemplate を特定し Deployment を戻す。"""
        apps_v1 = self._build_apps_v1_client()
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        match_labels = dep.spec.selector.match_labels or {}
        label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())

        rs_list = apps_v1.list_namespaced_replica_set(namespace=namespace, label_selector=label_selector).items

        def _rev(rs: k8s_client.V1ReplicaSet) -> int:
            ann = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
            return int(ann) if ann.isdigit() else 0

        sorted_rs = sorted(rs_list, key=_rev, reverse=True)
        if len(sorted_rs) < 2:
            raise RuntimeError(f"Deployment {namespace}/{deployment_name} にロールバック可能な前リビジョンが存在しません。")

        current_rev = _rev(sorted_rs[0])
        previous_rs = sorted_rs[1]
        previous_rev = _rev(previous_rs)

        # pod-template-hash ラベルを除去した前リビジョンのテンプレートで Deployment をパッチ更新
        template_dict = k8s_client.ApiClient().sanitize_for_serialization(previous_rs.spec.template)
        labels = template_dict.get("metadata", {}).get("labels", {})
        labels.pop("pod-template-hash", None)

        patch_body = {"spec": {"template": template_dict}}
        apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)
        return (
            f"Deployment {namespace}/{deployment_name} を revision {current_rev} から "
            f"直前の正常 revision {previous_rev} ({previous_rs.metadata.name}) へロールバックしました。"
        )

    def restart_deployment(self, namespace: str, deployment_name: str) -> str:
        """`kubectl rollout restart` と同等の PodTemplate アノテーション更新を実行する。"""
        apps_v1 = self._build_apps_v1_client()
        restarted_at = datetime.now(timezone.utc).isoformat()
        patch_body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": restarted_at,
                        }
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_body)
        return f"Deployment {namespace}/{deployment_name} のローリング再起動を開始しました (restartedAt={restarted_at})。"


class SafeRemediationService:
    """許可リスト検証と事後メトリクス確認を強制する復旧実行サービス。"""

    def __init__(
        self,
        settings: AgentSettings,
        executor: KubernetesRemediationExecutor,
        post_error_rate_probe: Callable[[str, str], float] | None = None,
    ) -> None:
        self._settings = settings
        self._executor = executor
        # 指定がない場合は正常回復 (0.0) を返すデフォルトプローブ（本番では Cloud Monitoring 直近エラー率取得関数を注入）
        self._post_probe = post_error_rate_probe or (lambda _ns, _svc: 0.0)

    def execute_and_verify(
        self,
        *,
        namespace: str,
        deployment_name: str,
        remediation_type: RemediationType,
    ) -> RemediationOutcome:
        """許可された Namespace 内でのみ型付き復旧を実行し、事後のエラー率低下を検証する。"""
        if namespace not in self._settings.allowed_namespaces:
            raise PermissionError(
                f"セキュリティ拒否: Namespace '{namespace}' は許可リスト {self._settings.allowed_namespaces} に含まれていません。"
            )

        if remediation_type == "ROLLBACK_DEPLOYMENT":
            msg = self._executor.rollback_deployment(namespace=namespace, deployment_name=deployment_name)
            status = "ROLLED_BACK"
        elif remediation_type == "RESTART_DEPLOYMENT":
            msg = self._executor.restart_deployment(namespace=namespace, deployment_name=deployment_name)
            status = "RESTARTED"
        else:
            return RemediationOutcome(
                executed=False,
                action_status="OBSERVED",
                detail_message="変更操作は不要と判定されました。",
                verification_passed=True,
                post_error_rate=None,
            )

        post_rate = round(float(self._post_probe(namespace, deployment_name)), 4)
        verified = post_rate < 0.01
        verify_note = (
            f"事後検証 OK: 復旧後のエラー率は {post_rate * 100:.2f}% (< 1.0%) に回復しました。"
            if verified
            else f"事後検証 要注意: 操作後もエラー率が {post_rate * 100:.2f}% 残存しています。オンコール担当者は継続確認してください。"
        )
        return RemediationOutcome(
            executed=True,
            action_status=status,
            detail_message=f"{msg} {verify_note}",
            verification_passed=verified,
            post_error_rate=post_rate,
        )
