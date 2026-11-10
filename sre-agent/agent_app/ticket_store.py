"""Slack Human-in-the-Loop 承認待ちチケット永続化ストア (`ticket_store.py`)。

ADK セッション本体だけでなく、「どのインシデント ID がどの ADK session_id /
invocation_id / confirmation_call_id / Slack メッセージに対応するのか」という
承認再開用メタデータを Firestore（本番環境）またはインメモリ（単体検証）へ保存します。
これにより Cloud Run instance の停止・再起動・再デプロイ後でも Slack 承認ボタンから
中断中の ADK ワークフローを安全に再開できます。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .config import AgentSettings
from .models import AlertEvent, TriageDecision


class StoredApprovalTicket(BaseModel):
    """永続化可能な Slack 承認待ちチケット。"""

    incident_id: str
    alert: AlertEvent
    decision: TriageDecision
    session_id: str
    invocation_id: str
    confirmation_call_id: str
    channel_id: str = ""
    message_ts: str = ""
    status: Literal["PENDING", "PROCESSING"] = "PENDING"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ApprovalTicketStore(Protocol):
    """本番 (Firestore) とローカル検証 (InMemory) を切り替えるチケットストア境界。"""

    async def save_ticket(self, ticket: StoredApprovalTicket) -> None:
        """承認待ちチケットを新規保存または上書き保存する。"""
        ...

    async def claim_ticket(self, incident_id: str) -> StoredApprovalTicket | None:
        """PENDING 状態のチケットを原子的に PROCESSING 状態へ遷移させ取得する。"""
        ...

    async def release_ticket(self, incident_id: str) -> None:
        """一時失敗時に PROCESSING 状態のチケットを PENDING へ戻す。"""
        ...

    async def delete_ticket(self, incident_id: str) -> None:
        """処理完了または却下済みのチケットを削除する。"""
        ...

    def snapshot_incident_ids(self) -> set[str]:
        """現在メモリ上に把握している PENDING チケットの incident_id 集合を返す。"""
        ...


class InMemoryApprovalTicketStore:
    """ローカル開発・単体テスト用のスレッド/コルーチン安全なインメモリチケットストア。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tickets: dict[str, StoredApprovalTicket] = {}

    async def save_ticket(self, ticket: StoredApprovalTicket) -> None:
        async with self._lock:
            self._tickets[ticket.incident_id] = ticket

    async def claim_ticket(self, incident_id: str) -> StoredApprovalTicket | None:
        async with self._lock:
            ticket = self._tickets.get(incident_id)
            if ticket is None or ticket.status != "PENDING":
                return None
            claimed = ticket.model_copy(update={"status": "PROCESSING"})
            self._tickets[incident_id] = claimed
            return ticket

    async def release_ticket(self, incident_id: str) -> None:
        async with self._lock:
            ticket = self._tickets.get(incident_id)
            if ticket is not None and ticket.status == "PROCESSING":
                self._tickets[incident_id] = ticket.model_copy(update={"status": "PENDING"})

    async def delete_ticket(self, incident_id: str) -> None:
        async with self._lock:
            self._tickets.pop(incident_id, None)

    def snapshot_incident_ids(self) -> set[str]:
        return {k for k, v in self._tickets.items() if v.status == "PENDING"}


class FirestoreApprovalTicketStore:
    """Google Cloud Firestore を用いた承認待ちチケット永続化ストア。

    トランザクションで ``PENDING -> PROCESSING`` への原子的遷移を行うため、
    担当者が承認ボタンを二重クリックした場合の重複再開も防止できます。
    """

    def __init__(
        self,
        settings: AgentSettings,
        *,
        client: Any | None = None,
    ) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.AsyncClient(
                project=settings.project_id,
                database=settings.firestore_database,
            )
        self._client = client
        self._collection_name = settings.firestore_tickets_collection
        self._known_pending: set[str] = set()

    def _doc_ref(self, incident_id: str) -> Any:
        # Firestore ドキュメント ID に '/' が含まれないよう安全にサニタイズ
        safe_id = incident_id.replace("/", "_")
        return self._client.collection(self._collection_name).document(safe_id)

    async def save_ticket(self, ticket: StoredApprovalTicket) -> None:
        await self._doc_ref(ticket.incident_id).set(ticket.model_dump(mode="json"))
        self._known_pending.add(ticket.incident_id)

    async def claim_ticket(self, incident_id: str) -> StoredApprovalTicket | None:
        doc_ref = self._doc_ref(incident_id)
        try:
            from google.cloud import firestore
        except ImportError:
            firestore = None  # type: ignore[assignment]

        # テスト用の軽量フェイククライアントでは通常メソッド経路へフォールバック
        if firestore is not None and isinstance(self._client, firestore.AsyncClient):
            transaction = self._client.transaction()

            @firestore.async_transactional
            async def _claim_in_tx(tx: Any) -> StoredApprovalTicket | None:
                snap = await doc_ref.get(transaction=tx)
                if not snap.exists:
                    return None
                raw = snap.to_dict() or {}
                if raw.get("status") != "PENDING":
                    return None
                tx.update(doc_ref, {"status": "PROCESSING"})
                return StoredApprovalTicket.model_validate(raw)

            claimed = await _claim_in_tx(transaction)
        else:
            snap = await doc_ref.get()
            if not snap.exists:
                return None
            raw = snap.to_dict() or {}
            if raw.get("status") != "PENDING":
                return None
            await doc_ref.update({"status": "PROCESSING"})
            claimed = StoredApprovalTicket.model_validate(raw)

        if claimed is not None:
            self._known_pending.discard(incident_id)
        return claimed

    async def release_ticket(self, incident_id: str) -> None:
        doc_ref = self._doc_ref(incident_id)
        snap = await doc_ref.get()
        if snap.exists and (snap.to_dict() or {}).get("status") == "PROCESSING":
            await doc_ref.update({"status": "PENDING"})
            self._known_pending.add(incident_id)

    async def delete_ticket(self, incident_id: str) -> None:
        await self._doc_ref(incident_id).delete()
        self._known_pending.discard(incident_id)

    def snapshot_incident_ids(self) -> set[str]:
        return set(self._known_pending)
