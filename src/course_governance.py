"""AI 普惠培训课程治理服务。

以只追加（append-only）事件日志保存课程目标、讲师与机构资质、教材版本、
练习数据来源、模型用途、隐私授权、活动场次与学习证明：

- 课程版本与教材一经发布即冻结，模型升级只能产生新版本；已签发的学习证明
  保存当时的版本、授权与讲师确认快照，不会被后续变更改写。
- 新增风险默认只暂停“尚未开始”的场次，或命令中明确列出的受影响场次；
  已完成场次不能被暂停，只能生成补救任务。
- 学员撤回授权、合作机构退出、教材被发现含不应公开资料时，暂停相关活动、
  保留全部已发生记录，并生成补救任务。
- 例外审批实行职责分离：请求人与利益相关方不得审批自己的例外。
- 事件日志落盘后，重启服务通过重放重建状态，并续办过期提醒与待复核事项。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------

DOMAIN = "ai-training-governance"

# 活动场次状态
SESSION_SCHEDULED = "scheduled"      # 尚未开始
SESSION_ONGOING = "ongoing"          # 进行中
SESSION_COMPLETED = "completed"      # 已完成
SESSION_SUSPENDED = "suspended"      # 已暂停

OPEN_SESSION_STATES = frozenset({SESSION_SCHEDULED, SESSION_ONGOING})

# 风险影响范围
SCOPE_NOT_STARTED = "not_started"    # 仅尚未开始的场次
SCOPE_EXPLICIT = "explicit"          # 明确列出的场次

# 暂停触发原因
REASON_RISK = "risk"
REASON_CONSENT_WITHDRAWN = "consent_withdrawn"
REASON_ORG_WITHDRAWN = "organization_withdrawn"
REASON_NONPUBLIC_MATERIAL = "nonpublic_material"

# 例外类型对应的审批角色
ROLE_PRIVACY_OFFICER = "privacy_officer"
ROLE_OPERATIONS = "operations"
EXCEPTION_APPROVER_ROLES = {
    "data_scope": ROLE_PRIVACY_OFFICER,
    "cross_border": ROLE_PRIVACY_OFFICER,
    "instructor_qualification": ROLE_OPERATIONS,
    "session_arrangement": ROLE_OPERATIONS,
}


class GovernanceError(ValueError):
    """课程治理规则被违反。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime:
    """解析 ISO-8601 时间，缺失时区时按 UTC 处理。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class Event:
    """只追加日志中的一条事件。"""

    seq: int
    id: str
    type: str
    occurred_at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "id": self.id,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        return cls(
            seq=int(value["seq"]),
            id=str(value["id"]),
            type=str(value["type"]),
            occurred_at=str(value["occurred_at"]),
            payload=dict(value["payload"]),
        )


class EventStore(Protocol):
    def append(self, event_type: str, payload: dict[str, Any]) -> Event: ...

    def append_many(self, events: Iterable[tuple[str, dict[str, Any]]]) -> list[Event]: ...

    def load(self) -> list[Event]: ...


class InMemoryEventStore:
    """测试与进程内使用的事件存储。"""

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, event_type: str, payload: dict[str, Any]) -> Event:
        return self.append_many([(event_type, payload)])[0]

    def append_many(self, events: Iterable[tuple[str, dict[str, Any]]]) -> list[Event]:
        appended: list[Event] = []
        for event_type, payload in events:
            event = Event(
                seq=len(self._events) + 1,
                id=uuid.uuid4().hex,
                type=event_type,
                occurred_at=_utcnow().isoformat(),
                payload=dict(payload),
            )
            self._events.append(event)
            appended.append(event)
        return appended

    def load(self) -> list[Event]:
        return list(self._events)


class JsonlEventStore:
    """将事件以 JSONL 形式追加落盘；重放时逐行读取。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_many(self, events: Iterable[tuple[str, dict[str, Any]]]) -> list[Event]:
        next_seq = len(self.load()) + 1
        appended: list[Event] = []
        with self.path.open("a", encoding="utf-8") as handle:
            for event_type, payload in events:
                event = Event(
                    seq=next_seq,
                    id=uuid.uuid4().hex,
                    type=event_type,
                    occurred_at=_utcnow().isoformat(),
                    payload=dict(payload),
                )
                next_seq += 1
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                appended.append(event)
            handle.flush()
            os.fsync(handle.fileno())
        return appended

    def append(self, event_type: str, payload: dict[str, Any]) -> Event:
        return self.append_many([(event_type, payload)])[0]

    def load(self) -> list[Event]:
        if not self.path.exists():
            return []
        events: list[Event] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(Event.from_dict(json.loads(line)))
        seqs = [event.seq for event in events]
        if seqs != list(range(1, len(events) + 1)):
            raise GovernanceError("事件日志序号不连续，无法安全重放")
        return events


# ---------------------------------------------------------------------------
# 状态投影（由事件重放得到，不单独持久化）
# ---------------------------------------------------------------------------


class GovernanceState:
    def __init__(self) -> None:
        self.orgs: dict[str, dict[str, Any]] = {}
        self.instructors: dict[str, dict[str, Any]] = {}
        self.privacy_officers: dict[str, str] = {}
        self.operations_staff: dict[str, str] = {}
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.scope_reviews: dict[str, dict[str, Any]] = {}
        self.courses: dict[str, dict[str, Any]] = {}
        self.materials: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.consents: dict[str, dict[str, Any]] = {}
        self.risks: list[dict[str, Any]] = []
        self.exceptions: dict[str, dict[str, Any]] = {}
        self.certificates: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.pending_reviews: dict[str, dict[str, Any]] = {}
        self.notified: set[str] = set()

    # -- 机构与人员 --------------------------------------------------------

    def require_active_org(self, org_id: str) -> dict[str, Any]:
        org = self.orgs.get(org_id)
        if org is None:
            raise GovernanceError(f"合作机构不存在：{org_id}")
        if org["status"] != "active":
            raise GovernanceError(f"合作机构已退出，不能开展新活动：{org_id}")
        return org

    def require_instructor(self, instructor_id: str) -> dict[str, Any]:
        instructor = self.instructors.get(instructor_id)
        if instructor is None:
            raise GovernanceError(f"讲师未登记：{instructor_id}")
        return instructor

    def current_qualification(self, instructor_id: str) -> dict[str, Any] | None:
        instructor = self.instructors.get(instructor_id)
        if not instructor or not instructor["qualifications"]:
            return None
        return max(instructor["qualifications"], key=lambda item: item["valid_until"])

    def qualification_valid_at(self, instructor_id: str, at: datetime) -> bool:
        qualification = self.current_qualification(instructor_id)
        return qualification is not None and parse_ts(qualification["valid_until"]) >= at

    # -- 教材与版本 --------------------------------------------------------

    def require_course(self, course_id: str) -> dict[str, Any]:
        course = self.courses.get(course_id)
        if course is None:
            raise GovernanceError(f"课程不存在：{course_id}")
        return course

    def get_version(self, course_id: str, version_no: int) -> dict[str, Any]:
        course = self.require_course(course_id)
        version = course["versions"].get(version_no)
        if version is None:
            raise GovernanceError(f"课程版本未发布：{course_id} v{version_no}")
        return version

    def approved_scope(self, data_source_id: str) -> dict[str, Any] | None:
        approved = [
            review
            for review in self.scope_reviews.values()
            if review["data_source_id"] == data_source_id and review["status"] == "approved"
        ]
        if not approved:
            return None
        return max(approved, key=lambda review: review["decided_at"])

    # -- 场次 --------------------------------------------------------------

    def sessions_for_version(self, course_id: str, version_no: int) -> list[dict[str, Any]]:
        return [
            session
            for session in self.sessions.values()
            if session["course_id"] == course_id and session["version_no"] == version_no
        ]

    def sessions_for_org(self, org_id: str) -> list[dict[str, Any]]:
        return [
            session
            for session in self.sessions.values()
            if self.instructors.get(session["instructor_id"], {}).get("org_id") == org_id
        ]

    def sessions_using_data_source(self, data_source_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for session in self.sessions.values():
            version = self.courses[session["course_id"]]["versions"].get(session["version_no"])
            if version and data_source_id in version["data_source_ids"]:
                result.append(session)
        return result


# ---------------------------------------------------------------------------
# 事件应用
# ---------------------------------------------------------------------------


def _apply(state: GovernanceState, event: Event) -> None:
    p = event.payload
    t = event.type

    if t == "OrganizationRegistered":
        state.orgs[p["org_id"]] = {"name": p["name"], "status": "active"}
    elif t == "OrganizationWithdrawn":
        state.orgs[p["org_id"]]["status"] = "withdrawn"

    elif t == "InstructorRegistered":
        state.instructors[p["instructor_id"]] = {
            "name": p["name"],
            "org_id": p["org_id"],
            "qualifications": [],
        }
    elif t == "InstructorQualificationVerified":
        state.instructors[p["instructor_id"]]["qualifications"].append(
            {
                "qualification": p["qualification"],
                "valid_from": p["valid_from"],
                "valid_until": p["valid_until"],
                "verifier_id": p["verifier_id"],
                "verified_event_seq": event.seq,
            }
        )
    elif t == "PrivacyOfficerRegistered":
        state.privacy_officers[p["officer_id"]] = p["name"]
    elif t == "OperationsStaffRegistered":
        state.operations_staff[p["staff_id"]] = p["name"]

    elif t == "DataSourceRegistered":
        state.data_sources[p["data_source_id"]] = {
            "description": p["description"],
            "cross_border": bool(p["cross_border"]),
            "org_id": p["org_id"],
        }
    elif t == "DataScopeSubmitted":
        state.scope_reviews[p["review_id"]] = {
            "data_source_id": p["data_source_id"],
            "scope_summary": p["scope_summary"],
            "submitted_by": p["submitted_by"],
            "status": "pending",
            "decided_at": None,
            "officer_id": None,
            "allows_cross_border": False,
            "reason": None,
        }
    elif t in ("DataScopeApproved", "DataScopeRejected"):
        review = state.scope_reviews[p["review_id"]]
        review["status"] = "approved" if t == "DataScopeApproved" else "rejected"
        review["decided_at"] = event.occurred_at
        review["officer_id"] = p["officer_id"]
        review["reason"] = p.get("reason")
        review["allows_cross_border"] = bool(p.get("allows_cross_border", False))

    elif t == "CourseRegistered":
        state.courses[p["course_id"]] = {
            "objective": p["objective"],
            "org_id": p["org_id"],
            "next_version": 1,
            "versions": {},
        }
    elif t == "MaterialSubmitted":
        state.materials[p["material_id"]] = {
            "course_id": p["course_id"],
            "version_no": p["version_no"],
            "title": p["title"],
            "fingerprint": p["fingerprint"],
            "data_source_ids": list(p["data_source_ids"]),
            "instructor_id": p["instructor_id"],
            "status": "submitted",
        }
    elif t == "CourseVersionPublished":
        course = state.courses[p["course_id"]]
        course["versions"][p["version_no"]] = {
            "material_id": p["material_id"],
            "title": p["title"],
            "material_fingerprint": p["material_fingerprint"],
            "data_source_ids": list(p["data_source_ids"]),
            "model_provider": p["model_provider"],
            "model_id": p["model_id"],
            "model_version": p["model_version"],
            "model_purpose": p["model_purpose"],
            "instructor_id": p["instructor_id"],
            "published_by": p["published_by"],
            "published_event_seq": event.seq,
            "quarantined": False,
        }
        course["next_version"] = max(course["next_version"], p["version_no"] + 1)
        state.materials[p["material_id"]]["status"] = "published"
    elif t == "MaterialQuarantined":
        material = state.materials[p["material_id"]]
        material["status"] = "quarantined"
        version = state.courses[material["course_id"]]["versions"].get(material["version_no"])
        if version is not None:
            version["quarantined"] = True

    elif t == "SessionScheduled":
        state.sessions[p["session_id"]] = {
            "id": p["session_id"],
            "course_id": p["course_id"],
            "version_no": p["version_no"],
            "instructor_id": p["instructor_id"],
            "starts_at": p["starts_at"],
            "ends_at": p["ends_at"],
            "status": SESSION_SCHEDULED,
            "enrollments": set(),
            "suspension": None,
            "instructor_confirmed_event_seq": None,
        }
    elif t == "LearnerEnrolled":
        state.sessions[p["session_id"]]["enrollments"].add(p["learner_id"])
    elif t == "SessionStarted":
        state.sessions[p["session_id"]]["status"] = SESSION_ONGOING
    elif t == "SessionCompleted":
        session = state.sessions[p["session_id"]]
        session["status"] = SESSION_COMPLETED
        session["completed_event_seq"] = event.seq
    elif t == "InstructorConfirmedSession":
        state.sessions[p["session_id"]]["instructor_confirmed_event_seq"] = event.seq
        state.sessions[p["session_id"]]["instructor_confirmed_by"] = p["instructor_id"]
    elif t == "SessionsSuspended":
        for session_id in p["session_ids"]:
            session = state.sessions[session_id]
            session["status"] = SESSION_SUSPENDED
            session["suspension"] = {
                "reason": p["reason"],
                "trigger_event_id": p.get("trigger_event_id"),
                "detail": p.get("detail"),
                "suspended_event_seq": event.seq,
            }

    elif t == "ConsentGranted":
        state.consents[p["consent_id"]] = {
            "learner_id": p["learner_id"],
            "data_source_id": p["data_source_id"],
            "scope": p["scope"],
            "valid_until": p["valid_until"],
            "status": "granted",
            "granted_event_seq": event.seq,
        }
    elif t == "ConsentWithdrawn":
        state.consents[p["consent_id"]]["status"] = "withdrawn"
        state.consents[p["consent_id"]]["withdrawn_event_seq"] = event.seq

    elif t == "RiskRegistered":
        state.risks.append({**p, "event_seq": event.seq})
    elif t == "ExceptionRequested":
        state.exceptions[p["request_id"]] = {**p, "status": "pending", "decision_event_seq": None}
    elif t in ("ExceptionApproved", "ExceptionRejected"):
        request = state.exceptions[p["request_id"]]
        request["status"] = "approved" if t == "ExceptionApproved" else "rejected"
        request["approver_id"] = p["approver_id"]
        request["decision_event_seq"] = event.seq
        request["note"] = p.get("note")

    elif t == "CertificateIssued":
        state.certificates[p["cert_id"]] = {**p, "event_seq": event.seq}

    elif t == "RemediationTaskCreated":
        state.tasks[p["task_id"]] = {
            **p,
            "status": "open",
            "created_event_seq": event.seq,
            "resolved_event_seq": None,
        }
    elif t == "RemediationTaskResolved":
        task = state.tasks[p["task_id"]]
        task["status"] = "resolved"
        task["resolved_event_seq"] = event.seq
        task["resolution_note"] = p.get("note", "")

    elif t == "ReviewRequested":
        state.pending_reviews[p["review_id"]] = {**p, "status": "open", "completed_event_seq": None}
    elif t == "ReviewCompleted":
        review = state.pending_reviews[p["review_id"]]
        review["status"] = "closed"
        review["completed_event_seq"] = event.seq
        review["outcome"] = p["outcome"]

    elif t == "ReminderNotified":
        state.notified.add(p["reminder_key"])

    else:
        raise GovernanceError(f"未知事件类型：{t}")


# ---------------------------------------------------------------------------
# 治理服务
# ---------------------------------------------------------------------------


class CourseGovernanceService:
    """课程治理应用服务；所有写操作都表现为事件追加。"""

    def __init__(
        self,
        store: EventStore,
        clock: Callable[[], datetime] = _utcnow,
        reminder_horizon: timedelta = timedelta(days=30),
    ) -> None:
        self.store = store
        self.clock = clock
        self.reminder_horizon = reminder_horizon
        self.state = GovernanceState()
        for event in store.load():
            _apply(self.state, event)

    def _now(self) -> datetime:
        return self.clock()

    def _append(self, event_type: str, payload: dict[str, Any]) -> Event:
        event = self.store.append(event_type, payload)
        _apply(self.state, event)
        return event

    def _append_many(self, events: list[tuple[str, dict[str, Any]]]) -> list[Event]:
        appended = self.store.append_many(events)
        for event in appended:
            _apply(self.state, event)
        return appended

    # -- 机构与人员登记 ----------------------------------------------------

    def register_organization(self, org_id: str, name: str) -> Event:
        if org_id in self.state.orgs:
            raise GovernanceError(f"机构已登记：{org_id}")
        return self._append("OrganizationRegistered", {"org_id": org_id, "name": name})

    def withdraw_organization(self, org_id: str, reason: str) -> list[Event]:
        """合作机构退出：暂停其未完成场次，记录保留，生成补救任务。"""
        org = self.state.orgs.get(org_id)
        if org is None:
            raise GovernanceError(f"合作机构不存在：{org_id}")
        if org["status"] == "withdrawn":
            raise GovernanceError("合作机构已处于退出状态")
        at = self._now()
        open_sessions = [
            session for session in self.state.sessions_for_org(org_id)
            if session["status"] in OPEN_SESSION_STATES
        ]
        events: list[tuple[str, dict[str, Any]]] = [
            ("OrganizationWithdrawn", {"org_id": org_id, "reason": reason, "at": at.isoformat()})
        ]
        if open_sessions:
            events.append(self._suspend_payload(
                [session["id"] for session in open_sessions],
                reason=REASON_ORG_WITHDRAWN,
                detail=f"合作机构退出：{org_id}",
            ))
        affected_certs = [
            cert_id for cert_id, cert in self.state.certificates.items()
            if self.state.instructors[
                self.state.sessions[cert["session_id"]]["instructor_id"]
            ]["org_id"] == org_id
        ]
        events.append((
            "RemediationTaskCreated",
            {
                "task_id": f"RM-{uuid.uuid4().hex[:8]}",
                "reason": REASON_ORG_WITHDRAWN,
                "subject_ref": org_id,
                "description": f"合作机构 {org_id} 退出后的学员通知、证明复核与转班安排",
                "related_session_ids": [s["id"] for s in open_sessions],
                "related_certificate_ids": affected_certs,
                "created_at": at.isoformat(),
                "due_at": (at + timedelta(days=14)).isoformat(),
            },
        ))
        return self._append_many(events)

    def register_instructor(self, instructor_id: str, name: str, org_id: str) -> Event:
        self.state.require_active_org(org_id)
        if instructor_id in self.state.instructors:
            raise GovernanceError(f"讲师已登记：{instructor_id}")
        return self._append(
            "InstructorRegistered",
            {"instructor_id": instructor_id, "name": name, "org_id": org_id},
        )

    def verify_instructor_qualification(
        self,
        instructor_id: str,
        qualification: str,
        valid_from: str,
        valid_until: str,
        verifier_id: str,
    ) -> Event:
        self.state.require_instructor(instructor_id)
        if parse_ts(valid_until) <= parse_ts(valid_from):
            raise GovernanceError("资质有效期截止时间必须晚于生效时间")
        return self._append(
            "InstructorQualificationVerified",
            {
                "instructor_id": instructor_id,
                "qualification": qualification,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "verifier_id": verifier_id,
            },
        )

    def register_privacy_officer(self, officer_id: str, name: str) -> Event:
        if officer_id in self.state.privacy_officers:
            raise GovernanceError(f"隐私负责人已登记：{officer_id}")
        return self._append(
            "PrivacyOfficerRegistered", {"officer_id": officer_id, "name": name}
        )

    def register_operations_staff(self, staff_id: str, name: str) -> Event:
        if staff_id in self.state.operations_staff:
            raise GovernanceError(f"运营人员已登记：{staff_id}")
        return self._append(
            "OperationsStaffRegistered", {"staff_id": staff_id, "name": name}
        )

    # -- 数据来源与隐私授权 ------------------------------------------------

    def register_data_source(
        self, data_source_id: str, description: str, cross_border: bool, org_id: str
    ) -> Event:
        if data_source_id in self.state.data_sources:
            raise GovernanceError(f"练习数据来源已登记：{data_source_id}")
        self.state.require_active_org(org_id)
        return self._append(
            "DataSourceRegistered",
            {
                "data_source_id": data_source_id,
                "description": description,
                "cross_border": cross_border,
                "org_id": org_id,
            },
        )

    def submit_data_scope(
        self, review_id: str, data_source_id: str, scope_summary: str, submitted_by: str
    ) -> Event:
        """讲师或合作方提交数据范围，等待隐私负责人审核。"""
        if data_source_id not in self.state.data_sources:
            raise GovernanceError(f"练习数据来源不存在：{data_source_id}")
        if review_id in self.state.scope_reviews:
            raise GovernanceError(f"数据范围审核已存在：{review_id}")
        return self._append(
            "DataScopeSubmitted",
            {
                "review_id": review_id,
                "data_source_id": data_source_id,
                "scope_summary": scope_summary,
                "submitted_by": submitted_by,
            },
        )

    def approve_data_scope(
        self,
        review_id: str,
        officer_id: str,
        allows_cross_border: bool = False,
    ) -> Event:
        """隐私负责人审核数据范围；提交人不得审批自己提交的范围。"""
        review = self.state.scope_reviews.get(review_id)
        if review is None:
            raise GovernanceError(f"数据范围审核不存在：{review_id}")
        if review["status"] != "pending":
            raise GovernanceError("数据范围已审核，不能重复审批")
        if officer_id not in self.state.privacy_officers:
            raise GovernanceError("只有隐私负责人可以审核数据范围")
        if officer_id == review["submitted_by"]:
            raise GovernanceError("提交人不得审核自己提交的数据范围")
        data_source = self.state.data_sources[review["data_source_id"]]
        if data_source["cross_border"] and not allows_cross_border:
            raise GovernanceError("跨境练习数据必须获得明确的跨境使用批准")
        return self._append(
            "DataScopeApproved",
            {
                "review_id": review_id,
                "officer_id": officer_id,
                "allows_cross_border": allows_cross_border,
            },
        )

    def reject_data_scope(self, review_id: str, officer_id: str, reason: str) -> Event:
        review = self.state.scope_reviews.get(review_id)
        if review is None:
            raise GovernanceError(f"数据范围审核不存在：{review_id}")
        if review["status"] != "pending":
            raise GovernanceError("数据范围已审核，不能重复审批")
        if officer_id not in self.state.privacy_officers:
            raise GovernanceError("只有隐私负责人可以审核数据范围")
        return self._append(
            "DataScopeRejected",
            {"review_id": review_id, "officer_id": officer_id, "reason": reason},
        )

    def grant_consent(
        self,
        consent_id: str,
        learner_id: str,
        data_source_id: str,
        scope: str,
        valid_until: str,
    ) -> Event:
        if data_source_id not in self.state.data_sources:
            raise GovernanceError(f"练习数据来源不存在：{data_source_id}")
        if consent_id in self.state.consents:
            raise GovernanceError(f"授权已存在：{consent_id}")
        if parse_ts(valid_until) <= self._now():
            raise GovernanceError("授权截止时间必须晚于当前时间")
        return self._append(
            "ConsentGranted",
            {
                "consent_id": consent_id,
                "learner_id": learner_id,
                "data_source_id": data_source_id,
                "scope": scope,
                "valid_until": valid_until,
            },
        )

    def withdraw_consent(self, consent_id: str, reason: str) -> list[Event]:
        """学员取消授权：暂停其报名且使用该数据的未完成场次并生成补救任务。"""
        consent = self.state.consents.get(consent_id)
        if consent is None:
            raise GovernanceError(f"授权不存在：{consent_id}")
        if consent["status"] == "withdrawn":
            raise GovernanceError("授权已撤回")
        at = self._now()
        learner_id = consent["learner_id"]
        data_source_id = consent["data_source_id"]
        affected = [
            session
            for session in self.state.sessions_using_data_source(data_source_id)
            if learner_id in session["enrollments"] and session["status"] in OPEN_SESSION_STATES
        ]
        events: list[tuple[str, dict[str, Any]]] = [
            ("ConsentWithdrawn", {
                "consent_id": consent_id,
                "learner_id": learner_id,
                "reason": reason,
                "at": at.isoformat(),
            })
        ]
        if affected:
            events.append(self._suspend_payload(
                [session["id"] for session in affected],
                reason=REASON_CONSENT_WITHDRAWN,
                detail=f"学员 {learner_id} 撤回授权 {consent_id}",
            ))
        events.append((
            "RemediationTaskCreated",
            {
                "task_id": f"RM-{uuid.uuid4().hex[:8]}",
                "reason": REASON_CONSENT_WITHDRAWN,
                "subject_ref": consent_id,
                "description": f"撤回授权后删除练习痕迹、调整练习分组并通知学员 {learner_id}",
                "related_session_ids": [session["id"] for session in affected],
                "related_certificate_ids": [],
                "created_at": at.isoformat(),
                "due_at": (at + timedelta(days=7)).isoformat(),
            },
        ))
        return self._append_many(events)

    # -- 课程、教材与版本 --------------------------------------------------

    def register_course(self, course_id: str, objective: str, org_id: str) -> Event:
        self.state.require_active_org(org_id)
        if course_id in self.state.courses:
            raise GovernanceError(f"课程已登记：{course_id}")
        if not objective.strip():
            raise GovernanceError("课程目标不能为空")
        return self._append(
            "CourseRegistered",
            {"course_id": course_id, "objective": objective, "org_id": org_id},
        )

    def submit_material(
        self,
        material_id: str,
        course_id: str,
        title: str,
        fingerprint: str,
        data_source_ids: list[str],
        instructor_id: str,
    ) -> Event:
        """讲师提交教材；版本号必须按课程严格递增，教材指纹不可为空。"""
        course = self.state.require_course(course_id)
        instructor = self.state.require_instructor(instructor_id)
        self.state.require_active_org(instructor["org_id"])
        if material_id in self.state.materials:
            raise GovernanceError(f"教材已存在：{material_id}")
        if not fingerprint.strip():
            raise GovernanceError("教材指纹不能为空")
        for data_source_id in data_source_ids:
            if data_source_id not in self.state.data_sources:
                raise GovernanceError(f"练习数据来源不存在：{data_source_id}")
        version_no = course["next_version"]
        return self._append(
            "MaterialSubmitted",
            {
                "material_id": material_id,
                "course_id": course_id,
                "version_no": version_no,
                "title": title,
                "fingerprint": fingerprint,
                "data_source_ids": list(data_source_ids),
                "instructor_id": instructor_id,
            },
        )

    def publish_course_version(
        self,
        course_id: str,
        material_id: str,
        model_provider: str,
        model_id: str,
        model_version: str,
        model_purpose: str,
        published_by: str,
    ) -> Event:
        """发布冻结一个课程版本：资质有效、数据范围经隐私审核、跨境使用已批准。"""
        course = self.state.require_course(course_id)
        material = self.state.materials.get(material_id)
        if material is None or material["course_id"] != course_id:
            raise GovernanceError(f"教材不属于该课程：{material_id}")
        if material["status"] == "published":
            raise GovernanceError("教材已发布，版本一经发布即冻结")
        if material["status"] == "quarantined":
            raise GovernanceError("教材已被隔离，不能发布")
        version_no = material["version_no"]
        if version_no != course["next_version"]:
            raise GovernanceError("课程版本只能按顺序递增发布")
        instructor = self.state.require_instructor(material["instructor_id"])
        self.state.require_active_org(instructor["org_id"])
        if not self.state.qualification_valid_at(material["instructor_id"], self._now()):
            raise GovernanceError("讲师资质不存在或已过期，不能发布课程版本")
        for data_source_id in material["data_source_ids"]:
            data_source = self.state.data_sources[data_source_id]
            review = self.state.approved_scope(data_source_id)
            if review is None:
                raise GovernanceError(f"练习数据范围未经隐私负责人审核：{data_source_id}")
            if data_source["cross_border"] and not review["allows_cross_border"]:
                raise GovernanceError(f"跨境使用未获批准：{data_source_id}")
        return self._append(
            "CourseVersionPublished",
            {
                "course_id": course_id,
                "version_no": version_no,
                "material_id": material_id,
                "title": material["title"],
                "material_fingerprint": material["fingerprint"],
                "data_source_ids": list(material["data_source_ids"]),
                "model_provider": model_provider,
                "model_id": model_id,
                "model_version": model_version,
                "model_purpose": model_purpose,
                "instructor_id": material["instructor_id"],
                "published_by": published_by,
            },
        )

    # -- 活动场次 ----------------------------------------------------------

    def schedule_session(
        self,
        session_id: str,
        course_id: str,
        version_no: int,
        instructor_id: str,
        starts_at: str,
        ends_at: str,
    ) -> Event:
        version = self.state.get_version(course_id, version_no)
        if version["quarantined"]:
            raise GovernanceError("该版本教材已被隔离，不能安排新场次")
        if version["instructor_id"] != instructor_id:
            raise GovernanceError("排课讲师与课程版本确认的讲师不一致")
        instructor = self.state.require_instructor(instructor_id)
        self.state.require_active_org(instructor["org_id"])
        start = parse_ts(starts_at)
        if parse_ts(ends_at) <= start:
            raise GovernanceError("场次结束时间必须晚于开始时间")
        if not self.state.qualification_valid_at(instructor_id, start):
            raise GovernanceError("讲师资质在场次开始前已过期")
        if session_id in self.state.sessions:
            raise GovernanceError(f"场次已存在：{session_id}")
        return self._append(
            "SessionScheduled",
            {
                "session_id": session_id,
                "course_id": course_id,
                "version_no": version_no,
                "instructor_id": instructor_id,
                "starts_at": starts_at,
                "ends_at": ends_at,
            },
        )

    def enroll_learner(self, session_id: str, learner_id: str, consent_ids: list[str]) -> Event:
        session = self.state.sessions.get(session_id)
        if session is None:
            raise GovernanceError(f"场次不存在：{session_id}")
        if session["status"] != SESSION_SCHEDULED:
            raise GovernanceError("只能报名尚未开始的场次")
        version = self.state.get_version(session["course_id"], session["version_no"])
        required = set(version["data_source_ids"])
        for consent_id in consent_ids:
            consent = self.state.consents.get(consent_id)
            if consent is None or consent["learner_id"] != learner_id:
                raise GovernanceError(f"授权不存在或不属于该学员：{consent_id}")
            if consent["status"] != "granted":
                raise GovernanceError(f"授权已失效：{consent_id}")
            if parse_ts(consent["valid_until"]) < parse_ts(session["ends_at"]):
                raise GovernanceError(f"授权在场次结束前到期：{consent_id}")
            required.discard(consent["data_source_id"])
        if required:
            raise GovernanceError(f"学员缺少练习数据授权：{sorted(required)}")
        return self._append(
            "LearnerEnrolled", {"session_id": session_id, "learner_id": learner_id}
        )

    def start_session(self, session_id: str) -> Event:
        session = self._require_open_session(session_id)
        if session["status"] != SESSION_SCHEDULED:
            raise GovernanceError("只有尚未开始的场次可以开始")
        return self._append("SessionStarted", {"session_id": session_id})

    def confirm_and_complete_session(self, session_id: str, instructor_id: str) -> list[Event]:
        """讲师确认并结课；确认事件序号会进入学习证明快照。"""
        session = self._require_open_session(session_id)
        if session["status"] != SESSION_ONGOING:
            raise GovernanceError("只有进行中的场次可以确认结课")
        if session["instructor_id"] != instructor_id:
            raise GovernanceError("只有排课讲师本人可以确认该场次")
        if not self.state.qualification_valid_at(instructor_id, self._now()):
            raise GovernanceError("讲师资质已过期，不能确认结课")
        return self._append_many([
            ("InstructorConfirmedSession", {
                "session_id": session_id,
                "instructor_id": instructor_id,
            }),
            ("SessionCompleted", {"session_id": session_id}),
        ])

    def _require_open_session(self, session_id: str) -> dict[str, Any]:
        session = self.state.sessions.get(session_id)
        if session is None:
            raise GovernanceError(f"场次不存在：{session_id}")
        if session["status"] == SESSION_SUSPENDED:
            raise GovernanceError("场次已暂停")
        if session["status"] == SESSION_COMPLETED:
            raise GovernanceError("场次已完成")
        return session

    # -- 风险、隔离与补救 --------------------------------------------------

    def _suspend_payload(
        self, session_ids: list[str], reason: str, detail: str
    ) -> tuple[str, dict[str, Any]]:
        return (
            "SessionsSuspended",
            {
                "session_ids": sorted(session_ids),
                "reason": reason,
                "detail": detail,
                "trigger_event_id": None,
            },
        )

    def register_risk(
        self,
        risk_id: str,
        description: str,
        scope: str,
        course_id: str | None = None,
        version_no: int | None = None,
        material_id: str | None = None,
        explicit_session_ids: list[str] | None = None,
    ) -> list[Event]:
        """登记新增风险。

        not_started：暂停该版本所有尚未开始的场次；
        explicit：暂停明确列出的未完成场次；已完成场次不能暂停，只生成补救任务。
        模型升级产生的新版本风险不会触及旧版本的任何场次与证明。
        """
        if scope not in (SCOPE_NOT_STARTED, SCOPE_EXPLICIT):
            raise GovernanceError("未知风险影响范围")
        at = self._now()
        target_sessions: list[dict[str, Any]] = []
        if scope == SCOPE_NOT_STARTED:
            if course_id is None or version_no is None:
                raise GovernanceError("not_started 风险必须指明课程版本")
            self.state.get_version(course_id, version_no)
            target_sessions = [
                session
                for session in self.state.sessions_for_version(course_id, version_no)
                if session["status"] == SESSION_SCHEDULED
            ]
        else:
            for session_id in explicit_session_ids or []:
                session = self.state.sessions.get(session_id)
                if session is None:
                    raise GovernanceError(f"明确受影响的场次不存在：{session_id}")
                if course_id is not None and (
                    session["course_id"] != course_id
                    or (version_no is not None and session["version_no"] != version_no)
                ):
                    raise GovernanceError(f"场次不属于指明的课程版本：{session_id}")
                target_sessions.append(session)

        suspendable = [s for s in target_sessions if s["status"] in OPEN_SESSION_STATES]
        already_done = [s for s in target_sessions if s["status"] == SESSION_COMPLETED]
        already_suspended = [s for s in target_sessions if s["status"] == SESSION_SUSPENDED]
        affected_certs = [
            cert_id
            for cert_id, cert in self.state.certificates.items()
            if cert["session_id"] in {s["id"] for s in already_done}
        ]

        events: list[tuple[str, dict[str, Any]]] = [
            (
                "RiskRegistered",
                {
                    "risk_id": risk_id,
                    "description": description,
                    "scope": scope,
                    "course_id": course_id,
                    "version_no": version_no,
                    "material_id": material_id,
                    "explicit_session_ids": list(explicit_session_ids or []),
                    "suspended_session_ids": [s["id"] for s in suspendable + already_suspended],
                    "completed_session_ids": [s["id"] for s in already_done],
                    "created_at": at.isoformat(),
                },
            )
        ]
        to_suspend = [s["id"] for s in suspendable]
        if to_suspend:
            events.append(self._suspend_payload(
                to_suspend, reason=REASON_RISK, detail=f"风险 {risk_id}：{description}"
            ))
        if already_done or affected_certs:
            events.append((
                "RemediationTaskCreated",
                {
                    "task_id": f"RM-{uuid.uuid4().hex[:8]}",
                    "reason": REASON_RISK,
                    "subject_ref": risk_id,
                    "description": "复核已完成场次与已签发学习证明，通知学员并安排补修",
                    "related_session_ids": [s["id"] for s in already_done],
                    "related_certificate_ids": affected_certs,
                    "created_at": at.isoformat(),
                    "due_at": (at + timedelta(days=14)).isoformat(),
                },
            ))
        return self._append_many(events)

    def report_nonpublic_material(self, material_id: str, detail: str) -> list[Event]:
        """发现教材含不应公开的资料：隔离版本、暂停未完成场次，记录全部保留。"""
        material = self.state.materials.get(material_id)
        if material is None:
            raise GovernanceError(f"教材不存在：{material_id}")
        if material["status"] == "quarantined":
            raise GovernanceError("教材已被隔离")
        at = self._now()
        course_id = material["course_id"]
        version_no = material["version_no"]
        sessions = self.state.sessions_for_version(course_id, version_no)
        suspendable = [s for s in sessions if s["status"] in OPEN_SESSION_STATES]
        completed = [s for s in sessions if s["status"] == SESSION_COMPLETED]
        affected_certs = [
            cert_id
            for cert_id, cert in self.state.certificates.items()
            if cert["session_id"] in {s["id"] for s in completed}
        ]
        events: list[tuple[str, dict[str, Any]]] = [
            ("MaterialQuarantined", {
                "material_id": material_id,
                "course_id": course_id,
                "version_no": version_no,
                "detail": detail,
                "at": at.isoformat(),
            })
        ]
        if suspendable:
            events.append(self._suspend_payload(
                [s["id"] for s in suspendable],
                reason=REASON_NONPUBLIC_MATERIAL,
                detail=f"教材 {material_id} 含不应公开资料",
            ))
        events.append((
            "RemediationTaskCreated",
            {
                "task_id": f"RM-{uuid.uuid4().hex[:8]}",
                "reason": REASON_NONPUBLIC_MATERIAL,
                "subject_ref": material_id,
                "description": "回收不应公开资料、排查传播范围、通知已完成场次学员",
                "related_session_ids": [s["id"] for s in sessions],
                "related_certificate_ids": affected_certs,
                "created_at": at.isoformat(),
                "due_at": (at + timedelta(days=7)).isoformat(),
            },
        ))
        return self._append_many(events)

    # -- 例外审批（职责分离）----------------------------------------------

    def request_exception(
        self,
        request_id: str,
        exception_type: str,
        target_ref: str,
        reason: str,
        requester_id: str,
        stakeholder_ids: list[str] | None = None,
    ) -> Event:
        if exception_type not in EXCEPTION_APPROVER_ROLES:
            raise GovernanceError(f"未知例外类型：{exception_type}")
        if request_id in self.state.exceptions:
            raise GovernanceError(f"例外申请已存在：{request_id}")
        return self._append(
            "ExceptionRequested",
            {
                "request_id": request_id,
                "exception_type": exception_type,
                "target_ref": target_ref,
                "reason": reason,
                "requester_id": requester_id,
                "stakeholder_ids": list(stakeholder_ids or []),
            },
        )

    def _decide_exception(
        self, request_id: str, approver_id: str, approve: bool, note: str
    ) -> Event:
        request = self.state.exceptions.get(request_id)
        if request is None:
            raise GovernanceError(f"例外申请不存在：{request_id}")
        if request["status"] != "pending":
            raise GovernanceError("例外申请已审批")
        required_role = EXCEPTION_APPROVER_ROLES[request["exception_type"]]
        role_members = (
            self.state.privacy_officers
            if required_role == ROLE_PRIVACY_OFFICER
            else self.state.operations_staff
        )
        if approver_id not in role_members:
            raise GovernanceError("审批人不具备该例外类型所需角色")
        if approver_id == request["requester_id"]:
            raise GovernanceError("不得审批自己提出的例外申请")
        if approver_id in request["stakeholder_ids"]:
            raise GovernanceError("利益相关方不得审批该例外申请")
        event_type = "ExceptionApproved" if approve else "ExceptionRejected"
        return self._append(
            event_type,
            {"request_id": request_id, "approver_id": approver_id, "note": note},
        )

    def approve_exception(self, request_id: str, approver_id: str, note: str = "") -> Event:
        return self._decide_exception(request_id, approver_id, True, note)

    def reject_exception(self, request_id: str, approver_id: str, note: str = "") -> Event:
        return self._decide_exception(request_id, approver_id, False, note)

    # -- 学习证明 ----------------------------------------------------------

    def issue_certificate(self, cert_id: str, learner_id: str, session_id: str) -> Event:
        """签发不可变学习证明，快照课程版本、授权与讲师确认。"""
        if cert_id in self.state.certificates:
            raise GovernanceError(f"学习证明已存在：{cert_id}")
        session = self.state.sessions.get(session_id)
        if session is None:
            raise GovernanceError(f"场次不存在：{session_id}")
        if session["status"] != SESSION_COMPLETED:
            raise GovernanceError("只能为已完成场次签发学习证明")
        if learner_id not in session["enrollments"]:
            raise GovernanceError("学员未报名该场次")
        if not session.get("instructor_confirmed_event_seq"):
            raise GovernanceError("场次缺少讲师确认")
        version = self.state.get_version(session["course_id"], session["version_no"])

        consent_snapshot = []
        for data_source_id in version["data_source_ids"]:
            matching = [
                (consent_id, consent)
                for consent_id, consent in self.state.consents.items()
                if consent["learner_id"] == learner_id
                and consent["data_source_id"] == data_source_id
                and consent["status"] == "granted"
                and parse_ts(consent["valid_until"]) >= parse_ts(session["ends_at"])
            ]
            if not matching:
                raise GovernanceError(f"学员缺少有效授权，无法签发证明：{data_source_id}")
            consent_id, consent = max(matching, key=lambda item: item[1]["granted_event_seq"])
            scope_review = self.state.approved_scope(data_source_id)
            review_id = next(
                (
                    key
                    for key, review in self.state.scope_reviews.items()
                    if review is scope_review
                ),
                None,
            )
            consent_snapshot.append({
                "consent_id": consent_id,
                "data_source_id": data_source_id,
                "scope": consent["scope"],
                "valid_until": consent["valid_until"],
                "granted_event_seq": consent["granted_event_seq"],
                "scope_review_id": review_id,
            })

        snapshot = {
            "course_id": session["course_id"],
            "version_no": session["version_no"],
            "material_id": version["material_id"],
            "material_fingerprint": version["material_fingerprint"],
            "model_provider": version["model_provider"],
            "model_id": version["model_id"],
            "model_version": version["model_version"],
            "model_purpose": version["model_purpose"],
            "data_source_ids": list(version["data_source_ids"]),
            "consents": consent_snapshot,
            "instructor_id": session["instructor_id"],
            "instructor_confirmed_event_seq": session["instructor_confirmed_event_seq"],
            "session_completed_event_seq": session["completed_event_seq"],
            "session_starts_at": session["starts_at"],
            "session_ends_at": session["ends_at"],
        }
        return self._append(
            "CertificateIssued",
            {
                "cert_id": cert_id,
                "learner_id": learner_id,
                "session_id": session_id,
                "issued_at": self._now().isoformat(),
                "snapshot": snapshot,
            },
        )

    def certificate_trace(self, cert_id: str) -> dict[str, Any]:
        """回答“一张证明用了哪一版课程、哪项授权、哪位讲师确认”。"""
        cert = self.state.certificates.get(cert_id)
        if cert is None:
            raise GovernanceError(f"学习证明不存在：{cert_id}")
        return {
            "cert_id": cert_id,
            "event_seq": cert["event_seq"],
            "issued_at": cert["issued_at"],
            "learner_id": cert["learner_id"],
            "session_id": cert["session_id"],
            "snapshot": cert["snapshot"],
        }

    # -- 待复核事项与补救任务 ----------------------------------------------

    def request_review(self, review_id: str, subject_ref: str, kind: str, due_at: str) -> Event:
        if review_id in self.state.pending_reviews:
            raise GovernanceError(f"待复核事项已存在：{review_id}")
        return self._append(
            "ReviewRequested",
            {
                "review_id": review_id,
                "subject_ref": subject_ref,
                "kind": kind,
                "due_at": due_at,
                "requested_at": self._now().isoformat(),
            },
        )

    def complete_review(self, review_id: str, outcome: str) -> Event:
        review = self.state.pending_reviews.get(review_id)
        if review is None:
            raise GovernanceError(f"待复核事项不存在：{review_id}")
        if review["status"] != "open":
            raise GovernanceError("待复核事项已关闭")
        return self._append(
            "ReviewCompleted",
            {"review_id": review_id, "outcome": outcome},
        )

    def resolve_task(self, task_id: str, note: str = "") -> Event:
        task = self.state.tasks.get(task_id)
        if task is None:
            raise GovernanceError(f"补救任务不存在：{task_id}")
        if task["status"] != "open":
            raise GovernanceError("补救任务已解决")
        return self._append(
            "RemediationTaskResolved", {"task_id": task_id, "note": note}
        )

    # -- 重启续办：提醒 ----------------------------------------------------

    def due_reminders(self) -> list[dict[str, Any]]:
        """计算到期/临期提醒（资质、授权），已通知的不重复返回。"""
        now = self._now()
        horizon = now + self.reminder_horizon
        reminders: list[dict[str, Any]] = []

        for instructor_id, instructor in self.state.instructors.items():
            qualification = self.state.current_qualification(instructor_id)
            if qualification is None:
                continue
            valid_until = parse_ts(qualification["valid_until"])
            if valid_until <= horizon:
                key = f"qualification:{instructor_id}:{qualification['valid_until']}"
                if key not in self.state.notified:
                    reminders.append({
                        "key": key,
                        "kind": "instructor_qualification_expiry",
                        "ref": instructor_id,
                        "due_at": qualification["valid_until"],
                        "overdue": valid_until < now,
                    })

        for consent_id, consent in self.state.consents.items():
            if consent["status"] != "granted":
                continue
            valid_until = parse_ts(consent["valid_until"])
            if valid_until <= horizon:
                key = f"consent:{consent_id}:{consent['valid_until']}"
                if key not in self.state.notified:
                    reminders.append({
                        "key": key,
                        "kind": "consent_expiry",
                        "ref": consent_id,
                        "learner_id": consent["learner_id"],
                        "due_at": consent["valid_until"],
                        "overdue": valid_until < now,
                    })
        return sorted(reminders, key=lambda item: item["due_at"])

    def open_reviews(self) -> list[dict[str, Any]]:
        """待复核事项（含已过期），重启后由事件重放恢复。"""
        now = self._now()
        reviews = []
        for review_id, review in self.state.pending_reviews.items():
            if review["status"] != "open":
                continue
            reviews.append({
                "review_id": review_id,
                "subject_ref": review["subject_ref"],
                "kind": review["kind"],
                "due_at": review["due_at"],
                "overdue": parse_ts(review["due_at"]) < now,
            })
        return sorted(reviews, key=lambda item: item["due_at"])

    def open_tasks(self) -> list[dict[str, Any]]:
        return [
            dict(task, task_id=task_id)
            for task_id, task in self.state.tasks.items()
            if task["status"] == "open"
        ]

    def mark_reminders_notified(self, keys: list[str]) -> list[Event]:
        """记录已通知的提醒键，保证重启后不重复通知。"""
        events = [
            ("ReminderNotified", {"reminder_key": key, "notified_at": self._now().isoformat()})
            for key in keys
            if key not in self.state.notified
        ]
        return self._append_many(events)

    def resume_after_restart(self) -> dict[str, Any]:
        """服务重启后一次性取回需要接续的全部事项。"""
        return {
            "replayed_events": len(self.store.load()),
            "due_reminders": self.due_reminders(),
            "open_reviews": self.open_reviews(),
            "open_tasks": self.open_tasks(),
        }
