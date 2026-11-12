"""アラート重複排除・クールダウン判定・サーキットブレーカー・監査記録モジュール。

本番運用で頻発する「同一障害で 5 分間に数十通のアラートが発火し、LLM 呼び出しと
再起動コマンドが連打される事故」を決定論的ロジックで防ぎます。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from google.cloud import bigquery

from .config import AgentSettings
from .models import AlertEvent, DecisionType, RemediationType


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _parse_iso_utc(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return _ensure_utc(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class IncidentEventRecord:
    """BigQuery `incident_events` テーブルの 1 行に対応する不変レコード。"""

    event_id: str
    incident_id: str
    fingerprint: str
    namespace: str
    service_name: str
    error_code: str
    decision: str
    remediation_type: str
    matched_runbook_id: str
    similarity_score: float
    verification_passed: bool
    summary: str
    created_at: datetime
    mttr_seconds: float | None = None
    estimated_manual_mttr_minutes: float | None = None
    saved_error_budget_minutes: float | None = None
    slo_budget_remaining_pct: float | None = None


class EventStoreBackend(Protocol):
    """BigQuery 本番実装とインメモリ単体テスト実装を切り替えるためのプロトコル。"""

    def find_recent_by_fingerprint(self, fingerprint: str, since: datetime) -> list[IncidentEventRecord]:
        """指定時刻以降の同一 fingerprint イベント一覧を返す。"""
        ...

    def count_auto_actions_for_service(self, namespace: str, service_name: str, since: datetime) -> int:
        """指定時刻以降に対象サービスへ自動実行された復旧操作の件数を返す。"""
        ...

    def append_event(self, record: IncidentEventRecord) -> None:
        """監査・状態イベントを追記する。"""
        ...


class InMemoryEventStoreBackend:
    """ローカル開発・自動テスト用のインメモリ追記型ストア。"""

    def __init__(self) -> None:
        self._rows: list[IncidentEventRecord] = []

    @property
    def rows(self) -> list[IncidentEventRecord]:
        return list(self._rows)

    def _last_reset_at(self) -> datetime | None:
        resets = [r.created_at for r in self._rows if r.decision == "ADMIN_RESET"]
        return max(resets) if resets else None

    def find_recent_by_fingerprint(self, fingerprint: str, since: datetime) -> list[IncidentEventRecord]:
        cutoff = self._last_reset_at()
        effective_since = max(since, cutoff) if cutoff else since
        return [
            r
            for r in self._rows
            if r.fingerprint == fingerprint
            and r.decision != "ADMIN_RESET"
            and r.created_at > effective_since
            if cutoff
        ] if cutoff else [
            r for r in self._rows if r.fingerprint == fingerprint and r.created_at >= since
        ]

    def count_auto_actions_for_service(self, namespace: str, service_name: str, since: datetime) -> int:
        cutoff = self._last_reset_at()
        return sum(
            1
            for r in self._rows
            if r.namespace == namespace
            and r.service_name == service_name
            and r.decision == "AUTO_REMEDIATE"
            and r.created_at >= since
            and (cutoff is None or r.created_at > cutoff)
        )

    def append_event(self, record: IncidentEventRecord) -> None:
        self._rows.append(record)


class BigQueryEventStoreBackend:
    """BigQuery `incident_events` テーブルを用いる本番用追記型ストア。"""

    def __init__(self, settings: AgentSettings, client: bigquery.Client | None = None) -> None:
        self._settings = settings
        self._client = client or bigquery.Client(project=settings.project_id, location=settings.region)
        self._table_fqn = f"{settings.project_id}.{settings.bq_dataset}.{settings.bq_events_table}"

    def find_recent_by_fingerprint(self, fingerprint: str, since: datetime) -> list[IncidentEventRecord]:
        sql = f"""
            SELECT event_id, incident_id, fingerprint, namespace, service_name, error_code,
                   decision, remediation_type, matched_runbook_id, similarity_score,
                   verification_passed, summary, created_at
            FROM `{self._table_fqn}`
            WHERE fingerprint = @fingerprint
              AND decision != 'ADMIN_RESET'
              AND created_at >= @since
              AND created_at > COALESCE(
                (SELECT MAX(created_at) FROM `{self._table_fqn}` WHERE decision = 'ADMIN_RESET'),
                TIMESTAMP('1970-01-01T00:00:00Z')
              )
            ORDER BY created_at DESC
            LIMIT 5
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("fingerprint", "STRING", fingerprint),
                bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
            ]
        )
        rows = self._client.query(sql, job_config=job_config).result()
        return [
            IncidentEventRecord(
                event_id=row.event_id,
                incident_id=row.incident_id,
                fingerprint=row.fingerprint,
                namespace=row.namespace,
                service_name=row.service_name,
                error_code=row.error_code,
                decision=row.decision,
                remediation_type=row.remediation_type,
                matched_runbook_id=row.matched_runbook_id or "NONE",
                similarity_score=float(row.similarity_score or 0.0),
                verification_passed=bool(row.verification_passed),
                summary=row.summary or "",
                created_at=row.created_at,
            )
            for row in rows
        ]

    def count_auto_actions_for_service(self, namespace: str, service_name: str, since: datetime) -> int:
        sql = f"""
            SELECT COUNT(*) AS cnt
            FROM `{self._table_fqn}`
            WHERE namespace = @namespace
              AND service_name = @service_name
              AND decision = 'AUTO_REMEDIATE'
              AND created_at >= @since
              AND created_at > COALESCE(
                (SELECT MAX(created_at) FROM `{self._table_fqn}` WHERE decision = 'ADMIN_RESET'),
                TIMESTAMP('1970-01-01T00:00:00Z')
              )
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("namespace", "STRING", namespace),
                bigquery.ScalarQueryParameter("service_name", "STRING", service_name),
                bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
            ]
        )
        row = next(iter(self._client.query(sql, job_config=job_config).result()))
        return int(row.cnt)

    def append_event(self, record: IncidentEventRecord) -> None:
        base_row: dict[str, object] = {
            "event_id": record.event_id,
            "incident_id": record.incident_id,
            "fingerprint": record.fingerprint,
            "namespace": record.namespace,
            "service_name": record.service_name,
            "error_code": record.error_code,
            "decision": record.decision,
            "remediation_type": record.remediation_type,
            "matched_runbook_id": record.matched_runbook_id,
            "similarity_score": record.similarity_score,
            "verification_passed": record.verification_passed,
            "summary": record.summary,
            "created_at": record.created_at.isoformat(),
        }
        slo_fields: dict[str, object] = {}
        if record.mttr_seconds is not None:
            slo_fields["mttr_seconds"] = record.mttr_seconds
        if record.estimated_manual_mttr_minutes is not None:
            slo_fields["estimated_manual_mttr_minutes"] = record.estimated_manual_mttr_minutes
        if record.saved_error_budget_minutes is not None:
            slo_fields["saved_error_budget_minutes"] = record.saved_error_budget_minutes
        if record.slo_budget_remaining_pct is not None:
            slo_fields["slo_budget_remaining_pct"] = record.slo_budget_remaining_pct

        full_row = {**base_row, **slo_fields}
        errors = self._client.insert_rows_json(self._table_fqn, [full_row])
        if errors and slo_fields and "no such field" in str(errors).lower():
            # 旧スキーマの既存テーブル環境でも監査書き込みを落とさないためのフォールバック
            errors = self._client.insert_rows_json(self._table_fqn, [base_row])
        if errors:
            raise RuntimeError(f"BigQuery 監査イベントの書き込みに失敗しました: {errors}")


class GuardrailCoordinator(Protocol):
    """単一または複数インスタンス間で処理中リースと自動復旧レート上限を原子的に共有する境界。"""

    def check_lease(self, fingerprint: str, now: datetime) -> str | None: ...

    def try_acquire_lease(
        self,
        fingerprint: str,
        incident_id: str,
        now: datetime,
        *,
        in_flight_ttl_seconds: int = 300,
    ) -> tuple[bool, str]: ...

    def complete_lease(
        self,
        fingerprint: str,
        incident_id: str,
        decision: str,
        completed_at: datetime,
        *,
        cooldown_minutes: int,
    ) -> None: ...

    def release_lease(self, fingerprint: str, incident_id: str) -> None: ...

    def try_reserve_auto_action(
        self,
        namespace: str,
        service_name: str,
        now: datetime,
        *,
        max_per_hour: int,
        existing_bq_count: int = 0,
    ) -> tuple[bool, int]: ...

    def reset_all(self, now: datetime) -> None: ...


class InMemoryGuardrailCoordinator:
    """ローカル開発・単体テスト用のスレッドセーフなインメモリ排他コーディネーター。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # fingerprint -> (updated_at, expires_at, incident_id, status, desc)
        self._leases: dict[str, tuple[datetime, datetime, str, str, str]] = {}
        self._rate_slots: dict[str, list[datetime]] = {}
        self._reset_at: datetime | None = None

    def _is_active(self, updated_at: datetime, expires_at: datetime, now: datetime) -> bool:
        if self._reset_at is not None and updated_at <= self._reset_at:
            return False
        return updated_at <= now < expires_at

    def check_lease(self, fingerprint: str, now: datetime) -> str | None:
        current = _ensure_utc(now)
        with self._lock:
            entry = self._leases.get(fingerprint)
            if entry is not None and self._is_active(entry[0], entry[1], current):
                return entry[4]
        return None

    def try_acquire_lease(
        self,
        fingerprint: str,
        incident_id: str,
        now: datetime,
        *,
        in_flight_ttl_seconds: int = 300,
    ) -> tuple[bool, str]:
        current = _ensure_utc(now)
        with self._lock:
            entry = self._leases.get(fingerprint)
            if entry is not None and self._is_active(entry[0], entry[1], current):
                return False, entry[4]
            desc = f"in_flight incident_id={incident_id}"
            self._leases[fingerprint] = (
                current,
                current + timedelta(seconds=in_flight_ttl_seconds),
                incident_id,
                "IN_FLIGHT",
                desc,
            )
            return True, ""

    def complete_lease(
        self,
        fingerprint: str,
        incident_id: str,
        decision: str,
        completed_at: datetime,
        *,
        cooldown_minutes: int,
    ) -> None:
        ts = _ensure_utc(completed_at)
        desc = f"前回 incident_id={incident_id}, decision={decision}"
        with self._lock:
            self._leases[fingerprint] = (
                ts,
                ts + timedelta(minutes=cooldown_minutes),
                incident_id,
                "COOLDOWN",
                desc,
            )

    def release_lease(self, fingerprint: str, incident_id: str) -> None:
        with self._lock:
            entry = self._leases.get(fingerprint)
            if entry is not None and entry[3] == "IN_FLIGHT" and entry[2] == incident_id:
                self._leases.pop(fingerprint, None)

    def try_reserve_auto_action(
        self,
        namespace: str,
        service_name: str,
        now: datetime,
        *,
        max_per_hour: int,
        existing_bq_count: int = 0,
    ) -> tuple[bool, int]:
        current = _ensure_utc(now)
        one_hour_ago = current - timedelta(hours=1)
        key = f"{namespace}__{service_name}"
        with self._lock:
            slots = [
                ts
                for ts in self._rate_slots.get(key, [])
                if ts >= one_hour_ago and (self._reset_at is None or ts > self._reset_at)
            ]
            effective = max(len(slots), existing_bq_count)
            if effective >= max_per_hour:
                self._rate_slots[key] = slots
                return False, effective
            slots.append(current)
            self._rate_slots[key] = slots
            return True, effective

    def reset_all(self, now: datetime) -> None:
        with self._lock:
            self._reset_at = _ensure_utc(now)
            self._leases.clear()
            self._rate_slots.clear()


class FirestoreGuardrailCoordinator:
    """Cloud Firestore トランザクションで冗長複数インスタンス間の重複排除と1時間上限を排他制御する。"""

    # 注意: Firestore では前後が "__" で囲まれたドキュメント ID は予約語エラーになるため通常名を使う
    _ADMIN_DOC_ID = "admin_cooldown_reset"

    def __init__(self, settings: AgentSettings, *, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(
                project=settings.project_id,
                database=settings.firestore_database,
            )
        self._client = client
        self._collection_name = settings.firestore_guardrail_collection

    @staticmethod
    def _safe_id(raw: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in raw)[:200]

    def _col(self) -> Any:
        return self._client.collection(self._collection_name)

    @staticmethod
    def _read_reset_at(admin_snap: Any) -> datetime | None:
        if not admin_snap.exists:
            return None
        return _parse_iso_utc((admin_snap.to_dict() or {}).get("reset_at"))

    @staticmethod
    def _active_desc(data: dict[str, Any], reset_at: datetime | None, current: datetime) -> str | None:
        updated_at = _parse_iso_utc(data.get("updated_at"))
        expires_at = _parse_iso_utc(data.get("expires_at"))
        if updated_at is None or expires_at is None:
            return None
        if reset_at is not None and updated_at <= reset_at:
            return None
        if updated_at <= current < expires_at:
            return str(data.get("desc", "active lease"))
        return None

    def check_lease(self, fingerprint: str, now: datetime) -> str | None:
        current = _ensure_utc(now)
        col = self._col()
        snap = col.document(f"lease_{fingerprint}").get()
        if not snap.exists:
            return None
        reset_at = self._read_reset_at(col.document(self._ADMIN_DOC_ID).get())
        return self._active_desc(snap.to_dict() or {}, reset_at, current)

    def try_acquire_lease(
        self,
        fingerprint: str,
        incident_id: str,
        now: datetime,
        *,
        in_flight_ttl_seconds: int = 300,
    ) -> tuple[bool, str]:
        current = _ensure_utc(now)
        col = self._col()
        doc_ref = col.document(f"lease_{fingerprint}")
        admin_ref = col.document(self._ADMIN_DOC_ID)
        payload = {
            "status": "IN_FLIGHT",
            "incident_id": incident_id,
            "updated_at": current.isoformat(),
            "expires_at": (current + timedelta(seconds=in_flight_ttl_seconds)).isoformat(),
            "desc": f"in_flight incident_id={incident_id}",
        }

        try:
            from google.cloud import firestore
        except ImportError:
            firestore = None  # type: ignore[assignment]

        if firestore is not None and isinstance(self._client, firestore.Client):
            tx = self._client.transaction()

            @firestore.transactional
            def _acquire_in_tx(transaction: Any) -> tuple[bool, str]:
                snap = doc_ref.get(transaction=transaction)
                admin_snap = admin_ref.get(transaction=transaction)
                reset_at = self._read_reset_at(admin_snap)
                if snap.exists:
                    active = self._active_desc(snap.to_dict() or {}, reset_at, current)
                    if active is not None:
                        return False, active
                transaction.set(doc_ref, payload)
                return True, ""

            return _acquire_in_tx(tx)

        snap = doc_ref.get()
        reset_at = self._read_reset_at(admin_ref.get())
        if snap.exists:
            active = self._active_desc(snap.to_dict() or {}, reset_at, current)
            if active is not None:
                return False, active
        doc_ref.set(payload)
        return True, ""

    def complete_lease(
        self,
        fingerprint: str,
        incident_id: str,
        decision: str,
        completed_at: datetime,
        *,
        cooldown_minutes: int,
    ) -> None:
        ts = _ensure_utc(completed_at)
        self._col().document(f"lease_{fingerprint}").set(
            {
                "status": "COOLDOWN",
                "incident_id": incident_id,
                "updated_at": ts.isoformat(),
                "expires_at": (ts + timedelta(minutes=cooldown_minutes)).isoformat(),
                "desc": f"前回 incident_id={incident_id}, decision={decision}",
            }
        )

    def release_lease(self, fingerprint: str, incident_id: str) -> None:
        doc_ref = self._col().document(f"lease_{fingerprint}")
        try:
            from google.cloud import firestore
        except ImportError:
            firestore = None  # type: ignore[assignment]

        if firestore is not None and isinstance(self._client, firestore.Client):
            tx = self._client.transaction()

            @firestore.transactional
            def _release_in_tx(transaction: Any) -> None:
                snap = doc_ref.get(transaction=transaction)
                if not snap.exists:
                    return
                raw = snap.to_dict() or {}
                if raw.get("status") == "IN_FLIGHT" and raw.get("incident_id") == incident_id:
                    transaction.delete(doc_ref)

            _release_in_tx(tx)
            return

        snap = doc_ref.get()
        if snap.exists:
            raw = snap.to_dict() or {}
            if raw.get("status") == "IN_FLIGHT" and raw.get("incident_id") == incident_id:
                doc_ref.delete()

    def try_reserve_auto_action(
        self,
        namespace: str,
        service_name: str,
        now: datetime,
        *,
        max_per_hour: int,
        existing_bq_count: int = 0,
    ) -> tuple[bool, int]:
        current = _ensure_utc(now)
        one_hour_ago = current - timedelta(hours=1)
        col = self._col()
        doc_ref = col.document(self._safe_id(f"rate_{namespace}__{service_name}"))
        admin_ref = col.document(self._ADMIN_DOC_ID)

        def _filter_slots(raw_list: list[Any], reset_at: datetime | None) -> list[datetime]:
            valid: list[datetime] = []
            for item in raw_list:
                dt = _parse_iso_utc(item)
                if dt is not None and dt >= one_hour_ago and (reset_at is None or dt > reset_at):
                    valid.append(dt)
            return valid

        try:
            from google.cloud import firestore
        except ImportError:
            firestore = None  # type: ignore[assignment]

        if firestore is not None and isinstance(self._client, firestore.Client):
            tx = self._client.transaction()

            @firestore.transactional
            def _reserve_in_tx(transaction: Any) -> tuple[bool, int]:
                snap = doc_ref.get(transaction=transaction)
                admin_snap = admin_ref.get(transaction=transaction)
                reset_at = self._read_reset_at(admin_snap)
                raw_slots = (snap.to_dict() or {}).get("action_timestamps", []) if snap.exists else []
                slots = _filter_slots(raw_slots, reset_at)
                effective = max(len(slots), existing_bq_count)
                if effective >= max_per_hour:
                    return False, effective
                slots.append(current)
                transaction.set(
                    doc_ref,
                    {
                        "action_timestamps": [s.isoformat() for s in slots],
                        "updated_at": current.isoformat(),
                    },
                )
                return True, effective

            return _reserve_in_tx(tx)

        snap = doc_ref.get()
        reset_at = self._read_reset_at(admin_ref.get())
        raw_slots = (snap.to_dict() or {}).get("action_timestamps", []) if snap.exists else []
        slots = _filter_slots(raw_slots, reset_at)
        effective = max(len(slots), existing_bq_count)
        if effective >= max_per_hour:
            return False, effective
        slots.append(current)
        doc_ref.set(
            {
                "action_timestamps": [s.isoformat() for s in slots],
                "updated_at": current.isoformat(),
            }
        )
        return True, effective

    def reset_all(self, now: datetime) -> None:
        ts = _ensure_utc(now)
        self._col().document(self._ADMIN_DOC_ID).set({"reset_at": ts.isoformat()})


class IncidentGuardrailManager:
    """重複排除・クールダウン・自動実行回数上限（サーキットブレーカー）を一元管理する。"""

    def __init__(
        self,
        settings: AgentSettings,
        backend: EventStoreBackend,
        *,
        coordinator: GuardrailCoordinator | None = None,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._coordinator: GuardrailCoordinator = coordinator or InMemoryGuardrailCoordinator()

    def is_in_cooldown(self, alert: AlertEvent, now: datetime | None = None) -> tuple[bool, str]:
        """直近のクールダウン期間内に同一障害が処理済み（または処理中）であれば True と理由を返す。"""
        current = _ensure_utc(now or datetime.now(timezone.utc))
        window = timedelta(minutes=self._settings.cooldown_minutes)
        fp = alert.compute_fingerprint()

        active_desc = self._coordinator.check_lease(fp, current)
        if active_desc is not None:
            return (
                True,
                f"クールダウン期間内 ({self._settings.cooldown_minutes}分) に同一 fingerprint={fp} が処理中または処理済みです ({active_desc})",
            )

        since = current - window
        recent = self._backend.find_recent_by_fingerprint(fp, since=since)
        if recent:
            latest = recent[0]
            self._coordinator.complete_lease(
                fp,
                latest.incident_id,
                latest.decision,
                latest.created_at,
                cooldown_minutes=self._settings.cooldown_minutes,
            )
            return (
                True,
                f"クールダウン期間内 ({self._settings.cooldown_minutes}分) に同一 fingerprint={fp} が処理済みです "
                f"(前回 incident_id={latest.incident_id}, decision={latest.decision})",
            )
        return False, ""

    def claim_for_processing(self, alert: AlertEvent, now: datetime | None = None) -> tuple[bool, str]:
        """複数インスタンス・並行受信時の重複起動を防ぐため、クールダウン確認と処理権取得を原子的に行う。"""
        current = _ensure_utc(now or datetime.now(timezone.utc))
        window = timedelta(minutes=self._settings.cooldown_minutes)
        fp = alert.compute_fingerprint()

        acquired, active_desc = self._coordinator.try_acquire_lease(fp, alert.incident_id, current)
        if not acquired:
            return (
                False,
                f"クールダウン期間内 ({self._settings.cooldown_minutes}分) に同一 fingerprint={fp} を重複排除しました ({active_desc})",
            )

        since = current - window
        recent = self._backend.find_recent_by_fingerprint(fp, since=since)
        if recent:
            latest = recent[0]
            self._coordinator.complete_lease(
                fp,
                latest.incident_id,
                latest.decision,
                latest.created_at,
                cooldown_minutes=self._settings.cooldown_minutes,
            )
            desc = f"前回 incident_id={latest.incident_id}, decision={latest.decision}"
            return False, f"クールダウン期間内に同一 fingerprint={fp} が処理済みです ({desc})"

        return True, ""

    def release_claim(self, alert: AlertEvent) -> None:
        """例外発生により処理未完了となった場合に処理中ロックを解放する。"""
        fp = alert.compute_fingerprint()
        self._coordinator.release_lease(fp, alert.incident_id)

    def enforce_safety_guardrails(
        self,
        *,
        namespace: str,
        service_name: str,
        proposed_decision: DecisionType,
        proposed_remediation: RemediationType,
        similarity_score: float,
        matched_runbook_age_days: float | None = None,
        matched_runbook_action: DecisionType | None = None,
        matched_runbook_remediation: RemediationType | None = None,
        recent_deployment_within_30m: bool | None = None,
        is_naturally_decaying: bool | None = None,
        latest_error_rate: float | None = None,
        now: datetime | None = None,
    ) -> tuple[DecisionType, RemediationType, str]:
        """LLM が提案した判断に対し、決定論的ルールで安全側への降格・昇格を強制する。"""
        if proposed_decision == "OBSERVE":
            # ログ経由のプロンプトインジェクション等による「持続障害の静観（握りつぶし）誘導」を遮断
            if is_naturally_decaying is False and (latest_error_rate or 0.0) >= 0.05:
                fallback_remediation: RemediationType = (
                    matched_runbook_remediation
                    if matched_runbook_remediation in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT", "MANUAL_INTERVENTION")
                    else "MANUAL_INTERVENTION"
                )
                return (
                    "REQUIRE_APPROVAL",
                    fallback_remediation,
                    f"隔離サンドボックスの実測エラー率が継続中 ({latest_error_rate:.1%}, 自然減衰なし) のため、"
                    "静観 (OBSERVE) 判定を棄却し人間確認へ昇格しました。",
                )
            return "OBSERVE", "NONE", ""

        # 1. 許可されていない Namespace は自動変更を絶対禁止
        if namespace not in self._settings.allowed_namespaces:
            return (
                "REQUIRE_APPROVAL",
                proposed_remediation,
                f"対象 Namespace '{namespace}' は自動復旧許可リスト {self._settings.allowed_namespaces} 外のため人間承認へ降格しました。",
            )

        if proposed_decision == "AUTO_REMEDIATE":
            # 2. 過去インシデント類似度がしきい値未満なら自動復旧を禁止し承認待ちへ降格
            if similarity_score < self._settings.auto_remediate_similarity_threshold:
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"類似度スコア ({similarity_score:.2f}) が自動復旧しきい値 "
                    f"({self._settings.auto_remediate_similarity_threshold:.2f}) 未満のため人間承認へ降格しました。",
                )

            # 3. MANUAL_INTERVENTION 種別は自動実行不可
            if proposed_remediation not in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT"):
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"復旧種別 '{proposed_remediation}' は自動実行対象外のため人間承認へ降格しました。",
                )

            # 4. 構造化一致条件: 一致 Runbook の推奨区分・操作種別および直近デプロイ変更の有無を検証
            if matched_runbook_action is not None and matched_runbook_action != "AUTO_REMEDIATE":
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"一致した過去 Runbook の推奨区分が '{matched_runbook_action}' のため自動実行を禁止し人間承認へ降格しました。",
                )
            if matched_runbook_remediation is not None and matched_runbook_remediation != proposed_remediation:
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"提案された操作 '{proposed_remediation}' が一致 Runbook の実証手順 '{matched_runbook_remediation}' "
                    "と一致しないため人間承認へ降格しました。",
                )
            if proposed_remediation == "ROLLBACK_DEPLOYMENT" and recent_deployment_within_30m is False:
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    "直近 30 分以内の新規デプロイ変更が確認されないため、無関係なロールバックを防止し人間承認へ降格しました。",
                )

            # 5. Runbook 鮮度有効期限チェック: 最終検証日から RUNBOOK_FRESHNESS_TTL_DAYS を超過した手順は人間再評価へ降格
            if (
                matched_runbook_age_days is not None
                and matched_runbook_age_days > float(self._settings.runbook_freshness_ttl_days)
            ):
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"一致した過去 Runbook の最終検証から {matched_runbook_age_days:.1f} 日が経過しており、"
                    f"鮮度有効期限 ({self._settings.runbook_freshness_ttl_days} 日) を超過しているため人間による再評価・承認へ降格しました。",
                )

            # 6. サーキットブレーカー: 同一サービスへの自動復旧が直近 1 時間に上限回数に達していたら停止（複数台間で原子的予約）
            current = _ensure_utc(now or datetime.now(timezone.utc))
            one_hour_ago = current - timedelta(hours=1)
            bq_count = self._backend.count_auto_actions_for_service(namespace, service_name, since=one_hour_ago)
            reserved, effective_count = self._coordinator.try_reserve_auto_action(
                namespace,
                service_name,
                current,
                max_per_hour=self._settings.max_auto_actions_per_hour,
                existing_bq_count=bq_count,
            )
            if not reserved:
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"直近 1 時間の自動復旧回数が上限 ({effective_count}/{self._settings.max_auto_actions_per_hour} 回) "
                    "に到達したため、再起動ループ防止のため人間承認へ降格しました。",
                )

        return proposed_decision, proposed_remediation, ""

    def record_outcome(
        self,
        *,
        alert: AlertEvent,
        decision: str,
        remediation_type: str,
        matched_runbook_id: str,
        similarity_score: float,
        verification_passed: bool,
        summary: str,
        mttr_seconds: float | None = None,
        estimated_manual_mttr_minutes: float | None = None,
        saved_error_budget_minutes: float | None = None,
        slo_budget_remaining_pct: float | None = None,
        now: datetime | None = None,
    ) -> IncidentEventRecord:
        """最終判断と処置結果を追記テーブルへ記録し、クールダウンリースを確定させる。"""
        completed_at = _ensure_utc(now or datetime.now(timezone.utc))
        record = IncidentEventRecord(
            event_id=uuid.uuid4().hex,
            incident_id=alert.incident_id,
            fingerprint=alert.compute_fingerprint(),
            namespace=alert.namespace,
            service_name=alert.service_name,
            error_code=alert.error_code,
            decision=decision,
            remediation_type=remediation_type,
            matched_runbook_id=matched_runbook_id,
            similarity_score=similarity_score,
            verification_passed=verification_passed,
            summary=summary,
            created_at=completed_at,
            mttr_seconds=mttr_seconds,
            estimated_manual_mttr_minutes=estimated_manual_mttr_minutes,
            saved_error_budget_minutes=saved_error_budget_minutes,
            slo_budget_remaining_pct=slo_budget_remaining_pct,
        )
        self._backend.append_event(record)
        self._coordinator.complete_lease(
            record.fingerprint,
            record.incident_id,
            record.decision,
            record.created_at,
            cooldown_minutes=self._settings.cooldown_minutes,
        )
        return record

    def reset_all_cooldowns(self, summary: str = "Hands-on drill cooldown reset", now: datetime | None = None) -> None:
        """監査履歴を削除せずに ADMIN_RESET 境界レコードを追記し、クールダウンと1時間上限を即時解除する。"""
        ts = _ensure_utc(now or datetime.now(timezone.utc))
        reset_record = IncidentEventRecord(
            event_id=uuid.uuid4().hex,
            incident_id=f"RESET-{uuid.uuid4().hex[:8]}",
            fingerprint="ADMIN_RESET",
            namespace="all",
            service_name="all",
            error_code="ADMIN_COOLDOWN_RESET",
            decision="ADMIN_RESET",
            remediation_type="NONE",
            matched_runbook_id="NONE",
            similarity_score=0.0,
            verification_passed=True,
            summary=summary,
            created_at=ts,
        )
        self._backend.append_event(reset_record)
        self._coordinator.reset_all(ts)
