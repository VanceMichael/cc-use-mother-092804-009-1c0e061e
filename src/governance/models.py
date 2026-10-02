"""课程治理服务的领域模型：实体、状态枚举与 JSON 序列化。"""

from __future__ import annotations

import types
from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints


class InstitutionStatus(str, Enum):
    """合作机构状态。"""

    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


class InstructorStatus(str, Enum):
    """讲师状态。"""

    ACTIVE = "active"
    SUSPENDED = "suspended"


class MaterialStatus(str, Enum):
    """教材版本状态。"""

    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    FLAGGED = "flagged"


class CourseVersionStatus(str, Enum):
    """课程版本状态。"""

    PUBLISHED = "published"
    RETIRED = "retired"


class ConsentStatus(str, Enum):
    """隐私授权状态。"""

    ACTIVE = "active"
    REVOKED = "revoked"


class SessionStatus(str, Enum):
    """活动场次状态。"""

    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PAUSED = "paused"


class EnrollmentStatus(str, Enum):
    """报名状态。"""

    ENROLLED = "enrolled"
    PAUSED = "paused"
    COMPLETED = "completed"


class ReviewKind(str, Enum):
    """审核任务类型。"""

    MATERIAL_REVIEW = "material_review"
    EXCEPTION = "exception"


class ReviewStatus(str, Enum):
    """审核任务状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RemediationKind(str, Enum):
    """补救任务类型。"""

    CONSENT_REVOKED = "consent_revoked"
    INSTITUTION_WITHDRAWN = "institution_withdrawn"
    MATERIAL_FLAGGED = "material_flagged"
    CONSENT_GAP = "consent_gap"


class RemediationStatus(str, Enum):
    """补救任务状态。"""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class ReminderKind(str, Enum):
    """提醒类型。"""

    REVIEW_OVERDUE = "review_overdue"
    CONSENT_EXPIRING = "consent_expiring"


class ReminderStatus(str, Enum):
    """提醒状态。"""

    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"


def _coerce(expected: Any, value: Any) -> Any:
    """把 JSON 还原出的值转回声明的类型（可选、元组、枚举）。"""
    if value is None:
        return None
    origin = get_origin(expected)
    if origin in (Union, types.UnionType):
        (inner,) = [arg for arg in get_args(expected) if arg is not type(None)]
        return _coerce(inner, value)
    if origin is tuple:
        return tuple(value)
    if isinstance(expected, type) and issubclass(expected, Enum):
        return expected(value)
    return value


class Record:
    """冻结 dataclass 的基类，提供与 JSON 互转的 to_dict / from_dict。"""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        hints = get_type_hints(cls)
        values = {field.name: _coerce(hints[field.name], data[field.name]) for field in fields(cls)}
        return cls(**values)


@dataclass(frozen=True)
class Institution(Record):
    """合作机构及其资质。"""

    institution_id: str
    name: str
    qualifications: tuple[str, ...]
    status: InstitutionStatus


@dataclass(frozen=True)
class Instructor(Record):
    """讲师及其所属机构与资质。"""

    instructor_id: str
    name: str
    institution_id: str
    qualifications: tuple[str, ...]
    status: InstructorStatus


@dataclass(frozen=True)
class PrivacyOfficer(Record):
    """隐私审核人员；institution_id 用于利益冲突检查。"""

    officer_id: str
    name: str
    institution_id: str | None


@dataclass(frozen=True)
class DataSource(Record):
    """练习数据来源及其隐私范围与跨境属性。"""

    data_source_id: str
    description: str
    origin: str
    privacy_scope: str
    cross_border: bool


@dataclass(frozen=True)
class Course(Record):
    """课程：目标与当前已发布版本号。"""

    course_id: str
    title: str
    objectives: tuple[str, ...]
    current_version: int | None


@dataclass(frozen=True)
class MaterialVersion(Record):
    """教材版本：由讲师提交，经隐私审核后方可用于发布。"""

    material_version_id: str
    course_id: str
    version_no: int
    submitted_by: str
    content_ref: str
    status: MaterialStatus
    submitted_at: str
    reviewed_by: str | None
    reviewed_at: str | None
    review_note: str


@dataclass(frozen=True)
class CourseVersion(Record):
    """课程版本：固定教材、模型、用途、数据来源与所需授权范围。"""

    course_id: str
    version_no: int
    material_version_id: str
    model_id: str
    model_purpose: str
    data_source_ids: tuple[str, ...]
    required_consent_scope: tuple[str, ...]
    status: CourseVersionStatus
    published_at: str


@dataclass(frozen=True)
class Consent(Record):
    """学员隐私授权：可撤销，可设到期时间。"""

    consent_id: str
    learner_id: str
    scope: tuple[str, ...]
    status: ConsentStatus
    granted_at: str
    revoked_at: str | None
    expires_at: str | None


@dataclass(frozen=True)
class Session(Record):
    """活动场次：排期时固定课程版本。"""

    session_id: str
    course_id: str
    course_version_no: int
    instructor_id: str
    scheduled_at: str
    status: SessionStatus
    paused_reason: str


@dataclass(frozen=True)
class Enrollment(Record):
    """报名：关联学员、场次与所使用的授权。"""

    enrollment_id: str
    session_id: str
    learner_id: str
    consent_id: str
    status: EnrollmentStatus


@dataclass(frozen=True)
class Certificate(Record):
    """学习证明：签发后不可改写，digest 为内容摘要。"""

    certificate_id: str
    enrollment_id: str
    learner_id: str
    course_id: str
    course_version_no: int
    material_version_id: str
    model_id: str
    consent_id: str
    confirmed_by: str
    issued_at: str
    digest: str


@dataclass(frozen=True)
class ReviewTask(Record):
    """审核任务：教材审核或例外审批。"""

    task_id: str
    kind: ReviewKind
    subject_id: str
    submitted_by: str
    status: ReviewStatus
    note: str
    created_at: str
    due_at: str | None
    decided_by: str | None
    decided_at: str | None
    detail: dict[str, Any]


@dataclass(frozen=True)
class RemediationTask(Record):
    """补救任务：事件处置后需要运营跟进的事项。"""

    task_id: str
    kind: RemediationKind
    subject_id: str
    summary: str
    status: RemediationStatus
    created_at: str
    resolved_at: str | None
    session_ids: tuple[str, ...]
    enrollment_ids: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class Reminder(Record):
    """提醒：到期后由 recover() 发出。"""

    reminder_id: str
    kind: ReminderKind
    subject_id: str
    due_at: str
    status: ReminderStatus
    created_at: str
    sent_at: str | None
