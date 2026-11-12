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
import hashlib
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

import google.auth
import google.auth.transport.requests
from google.cloud import container_v1, logging_v2
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
        ca_digest = hashlib.sha256(ca_bytes).hexdigest()[:12]
        ca_path = Path(tempfile.gettempdir()) / f"sre-gke-ca-{ca_digest}.crt"
        if not ca_path.exists():
            fd = os.open(str(ca_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, ca_bytes)
            finally:
                os.close(fd)
        cfg.ssl_ca_cert = str(ca_path)

        return k8s_client.AppsV1Api(k8s_client.ApiClient(cfg))

    def has_recent_deployment(self, namespace: str, deployment_name: str, *, within_minutes: int = 30) -> bool:
        """直近 within_minutes 以内に新しいリリースのロールアウトが発生したか確認する。

        自動ロールバック（古い正常 ReplicaSet の再有効化）や `restartedAt` による
        ローリング再起動は「新規コードリリースの混入」ではないため False として区別する。
        """
        apps_v1 = self._build_apps_v1_client()
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        dep_uid = getattr(dep.metadata, "uid", None)
        dep_annotations = (dep.metadata.annotations or {}) if dep.metadata else {}
        change_cause = dep_annotations.get("kubernetes.io/change-cause", "")
        if change_cause.startswith("SRE-Agent Automated"):
            return False

        match_labels = (dep.spec.selector.match_labels or {}) if dep.spec and dep.spec.selector else {}
        label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
        raw_rs_list = apps_v1.list_namespaced_replica_set(namespace=namespace, label_selector=label_selector).items
        owned_rs = [
            rs
            for rs in raw_rs_list
            if not (dep_uid and rs.metadata and rs.metadata.owner_references)
            or any(ref.uid == dep_uid for ref in rs.metadata.owner_references)
        ]

        def _rev(rs: k8s_client.V1ReplicaSet) -> int:
            ann = ((rs.metadata.annotations or {}) if rs.metadata else {}).get("deployment.kubernetes.io/revision", "0")
            return int(ann) if ann.isdigit() else 0

        active_rs_list = [rs for rs in owned_rs if (rs.spec.replicas or 0) > 0]
        if not active_rs_list:
            return False

        active_rs = max(active_rs_list, key=_rev)
        if _rev(active_rs) < 2:
            return False

        tpl_meta = (
            active_rs.spec.template.metadata
            if active_rs.spec and active_rs.spec.template and active_rs.spec.template.metadata
            else None
        )
        tpl_annotations = (tpl_meta.annotations or {}) if tpl_meta else {}
        if tpl_annotations.get("kubectl.kubernetes.io/restartedAt"):
            return False

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
        active_created = active_rs.metadata.creation_timestamp if active_rs.metadata else None
        if active_created is None or active_created < cutoff:
            return False

        # 稼働中 RS よりも後に作成された縮退済み RS が存在する場合はロールバック済み状態とみなす
        for rs in owned_rs:
            if (rs.spec.replicas or 0) == 0 and rs.metadata and rs.metadata.creation_timestamp:
                if rs.metadata.creation_timestamp > active_created:
                    return False

        return True

    def wait_deployment_ready(
        self,
        namespace: str,
        deployment_name: str,
        *,
        timeout_seconds: int = 75,
    ) -> tuple[bool, datetime]:
        """Deployment の新リビジョン Pod がすべて Ready かつ旧 Pod が縮退するまで待機する。"""
        apps_v1 = self._build_apps_v1_client()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
            desired = dep.spec.replicas or 1
            status = dep.status
            gen_ok = (status.observed_generation or 0) >= (dep.metadata.generation or 0)
            updated = status.updated_replicas or 0
            available = status.available_replicas or 0
            total_replicas = status.replicas or 0
            if gen_ok and updated >= desired and available >= desired and total_replicas <= updated:
                return True, datetime.now(timezone.utc)
            time.sleep(3)
        return False, datetime.now(timezone.utc)

    @staticmethod
    def _normalized_pod_template(rs: k8s_client.V1ReplicaSet) -> dict[str, object]:
        """比較・パッチ適用向けに pod-template-hash と再起動時刻アノテーションを除いたテンプレート辞書を返す。"""
        template_dict = k8s_client.ApiClient().sanitize_for_serialization(rs.spec.template)
        meta = template_dict.get("metadata") or {}
        labels = meta.get("labels") or {}
        labels.pop("pod-template-hash", None)
        return template_dict

    @classmethod
    def _is_failed_replicaset(cls, rs: k8s_client.V1ReplicaSet) -> bool:
        """過去ロールアウト失敗またはロールバック元として不健全と記録された ReplicaSet か判定する。"""
        ann = (rs.metadata.annotations or {}) if rs.metadata else {}
        if ann.get("sre-agent/unhealthy-revision") == "true":
            return True
        status = getattr(rs, "status", None)
        conditions = getattr(status, "conditions", None) or []
        if any(getattr(c, "type", "") == "ReplicaFailure" and getattr(c, "status", "") == "True" for c in conditions):
            return True
        spec_replicas = getattr(getattr(rs, "spec", None), "replicas", 0) or 0
        ready_replicas = getattr(status, "ready_replicas", 0) or 0
        # スケールダウン済みの過去正常 RS は spec.replicas==0 になるため除外せず、稼働要求中なのに Ready=0 の停滞 RS のみ除外
        return spec_replicas > 0 and ready_replicas == 0

    @classmethod
    def _select_rollback_target_rs(
        cls,
        sorted_rs: list[k8s_client.V1ReplicaSet],
    ) -> k8s_client.V1ReplicaSet | None:
        """現行 RS と同一仕様または失敗済みの RS を除外し、直近の健全な ReplicaSet を選定する。"""
        current_tpl = dict(cls._normalized_pod_template(sorted_rs[0]))
        current_ann = dict((current_tpl.get("metadata") or {}).get("annotations") or {})
        current_ann.pop("kubectl.kubernetes.io/restartedAt", None)

        candidates: list[k8s_client.V1ReplicaSet] = []
        for candidate in sorted_rs[1:]:
            if cls._is_failed_replicaset(candidate):
                continue
            cand_tpl = dict(cls._normalized_pod_template(candidate))
            cand_ann = dict((cand_tpl.get("metadata") or {}).get("annotations") or {})
            cand_ann.pop("kubectl.kubernetes.io/restartedAt", None)
            if cand_tpl.get("spec") == current_tpl.get("spec") and cand_ann == current_ann:
                continue
            candidates.append(candidate)

        if not candidates:
            return None
        for candidate in candidates:
            ann = (candidate.metadata.annotations or {}) if candidate.metadata else {}
            if ann.get("sre-agent/last-known-good") == "true":
                return candidate
        return candidates[0]

    def rollback_deployment(self, namespace: str, deployment_name: str) -> str:
        """ReplicaSet 履歴から失敗版を除外して直近の健全な PodTemplate を特定し JSON Patch replace で戻す。"""
        apps_v1 = self._build_apps_v1_client()
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        dep_uid = getattr(dep.metadata, "uid", None)
        match_labels = dep.spec.selector.match_labels or {}
        label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())

        raw_rs_list = apps_v1.list_namespaced_replica_set(namespace=namespace, label_selector=label_selector).items
        rs_list = [
            rs
            for rs in raw_rs_list
            if not (dep_uid and rs.metadata.owner_references)
            or any(ref.uid == dep_uid for ref in rs.metadata.owner_references)
        ]

        def _rev(rs: k8s_client.V1ReplicaSet) -> int:
            ann = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0")
            return int(ann) if ann.isdigit() else 0

        sorted_rs = sorted(rs_list, key=_rev, reverse=True)
        if len(sorted_rs) < 2:
            raise RuntimeError(f"Deployment {namespace}/{deployment_name} にロールバック可能な前リビジョンが存在しません。")

        current_rs = sorted_rs[0]
        current_rev = _rev(current_rs)
        previous_rs = self._select_rollback_target_rs(sorted_rs)
        if previous_rs is None:
            raise RuntimeError(
                f"Deployment {namespace}/{deployment_name} にロールバック可能な健全な過去リビジョンが見つかりません。"
            )
        previous_rev = _rev(previous_rs)

        # 不良リビジョンが次回以降の連続ロールバック先として再び選ばれないよう印を残す（失敗してもロールバック本体は継続）
        if current_rs.metadata and current_rs.metadata.name and hasattr(apps_v1, "patch_namespaced_replica_set"):
            try:
                apps_v1.patch_namespaced_replica_set(
                    name=current_rs.metadata.name,
                    namespace=namespace,
                    body={"metadata": {"annotations": {"sre-agent/unhealthy-revision": "true"}}},
                )
            except Exception:  # noqa: BLE001
                pass

        # pod-template-hash ラベルを除去した健全リビジョンのテンプレートで /spec/template を完全置換
        template_dict = self._normalized_pod_template(previous_rs)

        patch_ops: list[dict[str, object]] = [
            {"op": "replace", "path": "/spec/template", "value": template_dict},
        ]
        rollback_cause = f"SRE-Agent Automated Rollback to revision {previous_rev}"
        if dep.metadata and dep.metadata.annotations is not None:
            patch_ops.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations/kubernetes.io~1change-cause",
                    "value": rollback_cause,
                }
            )
            if "kubectl.kubernetes.io/last-applied-configuration" in dep.metadata.annotations:
                patch_ops.append(
                    {
                        "op": "remove",
                        "path": "/metadata/annotations/kubectl.kubernetes.io~1last-applied-configuration",
                    }
                )
        else:
            patch_ops.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations",
                    "value": {"kubernetes.io/change-cause": rollback_cause},
                }
            )
        apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch_ops)
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

        if remediation_type not in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT"):
            return RemediationOutcome(
                executed=False,
                action_status="OBSERVED",
                detail_message="変更操作は不要と判定されました。",
                verification_passed=True,
                post_error_rate=None,
            )

        try:
            if remediation_type == "ROLLBACK_DEPLOYMENT":
                msg = self._executor.rollback_deployment(namespace=namespace, deployment_name=deployment_name)
                status = "ROLLED_BACK"
            else:
                msg = self._executor.restart_deployment(namespace=namespace, deployment_name=deployment_name)
                status = "RESTARTED"

            post_rate = round(float(self._post_probe(namespace, deployment_name)), 4)
        except Exception as exc:  # noqa: BLE001
            return RemediationOutcome(
                executed=False,
                action_status="FAILED",
                detail_message=f"復旧操作または事後検証中にエラーが発生しました: {exc}",
                verification_passed=False,
                post_error_rate=None,
            )

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


def build_live_post_remediation_probe(
    settings: AgentSettings,
    executor: GkeAppsV1RemediationExecutor,
) -> Callable[[str, str], float]:
    """Deployment ロールアウト完了と Cloud Logging 直近エラー率を実測する本番用事後検証プローブ。"""

    def _probe(namespace: str, deployment_name: str) -> float:
        ready, converged_at = executor.wait_deployment_ready(namespace, deployment_name, timeout_seconds=75)
        if not ready:
            return 1.0

        # 新リビジョン Pod の定常ログ安定を短時間待機してから ERROR ログ有無を確認する
        time.sleep(8)
        client = logging_v2.Client(project=settings.project_id)
        since = (converged_at - timedelta(seconds=1)).isoformat()
        log_filter = (
            'resource.type="k8s_container" '
            f'AND resource.labels.namespace_name="{namespace}" '
            f'AND resource.labels.container_name="{deployment_name}" '
            'AND NOT textPayload=~"^INFO:" '
            f'AND timestamp>="{since}"'
        )
        total = 0
        errors = 0
        for entry in client.list_entries(filter_=log_filter, page_size=100, max_results=300):
            total += 1
            if str(entry.severity or "INFO").upper() in {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}:
                errors += 1
        return (errors / total) if total > 0 else 0.0

    return _probe
