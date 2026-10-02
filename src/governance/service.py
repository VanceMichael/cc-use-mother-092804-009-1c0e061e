"""课程治理服务：课程版本、资质、授权、场次、证书与补救的核心规则。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .models import (
    Certificate,
    Consent,
    ConsentStatus,
    Course,
    CourseVersion,
    CourseVersionStatus,
    DataSource,
    Enrollment,
    EnrollmentStatus,
    Institution,
    InstitutionStatus,
    Instructor,
    InstructorStatus,
    MaterialStatus,
    MaterialVersion,
    PrivacyOfficer,
    RemediationKind,
    RemediationStatus,
    RemediationTask,
    Reminder,
    ReminderKind,
    ReminderStatus,
    ReviewKind,
    ReviewStatus,
    ReviewTask,
    Session,
    SessionStatus,
)
from .store import JsonStore


class GovernanceError(ValueError):
    """业务规则校验失败。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(moment: str) -> datetime:
    parsed = datetime.fromisoformat(moment)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


@dataclass(frozen=True)
class RecoveryReport:
    """服务重启后的接续结果。"""

    emitted_reminders: tuple[str, ...]
    expired_consents: tuple[str, ...]
    overdue_reviews: tuple[str, ...]
    pending_reviews: tuple[str, ...]
    open_remediations: tuple[str, ...]


class GovernanceService:
    """面向普惠培训运营团队的课程治理服务。

    所有状态经 JsonStore 持久化；进程重启后重新构造服务并调用
    recover()，即可接续到期授权、过期提醒与待复核事项。
    """

    def __init__(self, store: JsonStore, now: Callable[[], datetime] | None = None):
        self._store = store
        self._now = now or _utcnow
        self._state = store.load()

    # -- 基础设施 ---------------------------------------------------

    def _timestamp(self) -> str:
        return self._now().isoformat()

    def _next_id(self, prefix: str) -> str:
        counters = self._state["counters"]
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    def _audit(self, actor: str, action: str, **detail: Any) -> None:
        self._state["audit"].append(
            {
                "seq": len(self._state["audit"]) + 1,
                "at": self._timestamp(),
                "actor": actor,
                "action": action,
                "detail": detail,
            }
        )

    def _save(self) -> None:
        self._store.save(self._state)

    def _find(self, collection: str, key: str, model: type) -> Any:
        raw = self._state[collection].get(key)
        if raw is None:
            raise GovernanceError(f"未找到记录 {collection}/{key}")
        return model.from_dict(raw)

    def _put(self, collection: str, key: str, record: Any) -> None:
        self._state[collection][key] = record.to_dict()

    def _all(self, collection: str, model: type) -> list:
        return [model.from_dict(raw) for raw in self._state[collection].values()]

    @staticmethod
    def _version_key(course_id: str, version_no: int) -> str:
        return f"{course_id}@{version_no}"

    def _get_version(self, course_id: str, version_no: int) -> CourseVersion:
        return self._find("course_versions", self._version_key(course_id, version_no), CourseVersion)

    # -- 注册 -------------------------------------------------------

    def register_institution(
        self, institution_id: str, name: str, qualifications: Iterable[str] = ()
    ) -> Institution:
        if institution_id in self._state["institutions"]:
            raise GovernanceError("机构已存在")
        record = Institution(
            institution_id=institution_id,
            name=name,
            qualifications=tuple(qualifications),
            status=InstitutionStatus.ACTIVE,
        )
        self._put("institutions", institution_id, record)
        self._audit("operator", "institution.registered", institution_id=institution_id)
        self._save()
        return record

    def register_instructor(
        self,
        instructor_id: str,
        name: str,
        institution_id: str,
        qualifications: Iterable[str] = (),
    ) -> Instructor:
        institution = self._find("institutions", institution_id, Institution)
        if institution.status != InstitutionStatus.ACTIVE:
            raise GovernanceError("机构已退出，无法登记讲师")
        if instructor_id in self._state["instructors"]:
            raise GovernanceError("讲师已存在")
        record = Instructor(
            instructor_id=instructor_id,
            name=name,
            institution_id=institution_id,
            qualifications=tuple(qualifications),
            status=InstructorStatus.ACTIVE,
        )
        self._put("instructors", instructor_id, record)
        self._audit("operator", "instructor.registered", instructor_id=instructor_id)
        self._save()
        return record

    def register_privacy_officer(
        self, officer_id: str, name: str, institution_id: str | None = None
    ) -> PrivacyOfficer:
        if officer_id in self._state["officers"]:
            raise GovernanceError("审核人员已存在")
        record = PrivacyOfficer(officer_id=officer_id, name=name, institution_id=institution_id)
        self._put("officers", officer_id, record)
        self._audit("operator", "officer.registered", officer_id=officer_id)
        self._save()
        return record

    def register_data_source(
        self,
        data_source_id: str,
        description: str,
        origin: str,
        privacy_scope: str,
        cross_border: bool = False,
    ) -> DataSource:
        if data_source_id in self._state["data_sources"]:
            raise GovernanceError("数据来源已存在")
        record = DataSource(
            data_source_id=data_source_id,
            description=description,
            origin=origin,
            privacy_scope=privacy_scope,
            cross_border=bool(cross_border),
        )
        self._put("data_sources", data_source_id, record)
        self._audit("operator", "data_source.registered", data_source_id=data_source_id)
        self._save()
        return record

    def create_course(self, course_id: str, title: str, objectives: Iterable[str]) -> Course:
        if course_id in self._state["courses"]:
            raise GovernanceError("课程已存在")
        if not tuple(objectives):
            raise GovernanceError("课程目标不能为空")
        record = Course(
            course_id=course_id,
            title=title,
            objectives=tuple(objectives),
            current_version=None,
        )
        self._put("courses", course_id, record)
        self._audit("operator", "course.created", course_id=course_id)
        self._save()
        return record

    # -- 教材提交与审核 ----------------------------------------------

    def submit_material_version(
        self,
        course_id: str,
        instructor_id: str,
        content_ref: str,
        due_at: datetime | str | None = None,
    ) -> MaterialVersion:
        """讲师提交教材版本，同时生成待审核任务。"""
        self._find("courses", course_id, Course)
        instructor = self._find("instructors", instructor_id, Instructor)
        if instructor.status != InstructorStatus.ACTIVE:
            raise GovernanceError("讲师状态不可用")
        version_no = (
            sum(1 for raw in self._state["material_versions"].values() if raw["course_id"] == course_id) + 1
        )
        record = MaterialVersion(
            material_version_id=self._next_id("matv"),
            course_id=course_id,
            version_no=version_no,
            submitted_by=instructor_id,
            content_ref=content_ref,
            status=MaterialStatus.SUBMITTED,
            submitted_at=self._timestamp(),
            reviewed_by=None,
            reviewed_at=None,
            review_note="",
        )
        self._put("material_versions", record.material_version_id, record)
        task = ReviewTask(
            task_id=self._next_id("review"),
            kind=ReviewKind.MATERIAL_REVIEW,
            subject_id=record.material_version_id,
            submitted_by=instructor_id,
            status=ReviewStatus.PENDING,
            note="",
            created_at=self._timestamp(),
            due_at=_iso(due_at),
            decided_by=None,
            decided_at=None,
            detail={},
        )
        self._put("review_tasks", task.task_id, task)
        self._audit(
            instructor_id,
            "material.submitted",
            material_version_id=record.material_version_id,
            course_id=course_id,
        )
        self._save()
        return record

    def _ensure_independent(self, task: ReviewTask, officer: PrivacyOfficer) -> None:
        """利益冲突回避：提交者本人及同机构人员不得审批。"""
        if task.status != ReviewStatus.PENDING:
            raise GovernanceError("任务已处理，不能重复审批")
        if task.submitted_by == officer.officer_id:
            raise GovernanceError("利益相关者不得审批自己的提交或例外")
        submitter = self._state["instructors"].get(task.submitted_by)
        if (
            submitter
            and officer.institution_id
            and submitter["institution_id"] == officer.institution_id
        ):
            raise GovernanceError("审批人与提交者同属一个机构，存在利益冲突")

    def review_material(self, task_id: str, officer_id: str, approve: bool, note: str = "") -> ReviewTask:
        """隐私审核人员决定教材版本是否可用于发布。"""
        task = self._find("review_tasks", task_id, ReviewTask)
        if task.kind != ReviewKind.MATERIAL_REVIEW:
            raise GovernanceError("该任务不是教材审核")
        officer = self._find("officers", officer_id, PrivacyOfficer)
        self._ensure_independent(task, officer)
        material = self._find("material_versions", task.subject_id, MaterialVersion)
        decided_at = self._timestamp()
        task = replace(
            task,
            status=ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED,
            decided_by=officer.officer_id,
            decided_at=decided_at,
            note=note,
        )
        material = replace(
            material,
            status=MaterialStatus.APPROVED if approve else MaterialStatus.REJECTED,
            reviewed_by=officer.officer_id,
            reviewed_at=decided_at,
            review_note=note,
        )
        self._put("review_tasks", task.task_id, task)
        self._put("material_versions", material.material_version_id, material)
        self._cancel_reminders(ReminderKind.REVIEW_OVERDUE, task.task_id)
        self._audit(
            officer_id,
            "material.reviewed",
            task_id=task.task_id,
            material_version_id=material.material_version_id,
            approved=approve,
        )
        self._save()
        return task

    # -- 课程版本 ----------------------------------------------------

    def publish_course_version(
        self,
        course_id: str,
        material_version_id: str,
        model_id: str,
        model_purpose: str,
        data_source_ids: Iterable[str] = (),
        required_consent_scope: Iterable[str] = (),
        exception_task_id: str | None = None,
    ) -> CourseVersion:
        """发布新的课程版本。

        已完成的场次与证书保持原版本不变；尚未开始的场次切换到新版本，
        授权范围不足的报名暂停并生成补救任务。
        """
        course = self._find("courses", course_id, Course)
        material = self._find("material_versions", material_version_id, MaterialVersion)
        if material.course_id != course_id:
            raise GovernanceError("教材不属于该课程")
        if material.status != MaterialStatus.APPROVED:
            raise GovernanceError("教材尚未通过隐私审核")
        sources = [self._find("data_sources", item, DataSource) for item in data_source_ids]
        required = tuple(required_consent_scope)
        for source in sources:
            if source.privacy_scope not in required:
                raise GovernanceError(f"授权范围未覆盖数据来源 {source.data_source_id}")
        if any(source.cross_border for source in sources):
            self._ensure_cross_border_exception(course_id, exception_task_id)
        version_no = (course.current_version or 0) + 1
        record = CourseVersion(
            course_id=course_id,
            version_no=version_no,
            material_version_id=material_version_id,
            model_id=model_id,
            model_purpose=model_purpose,
            data_source_ids=tuple(data_source_ids),
            required_consent_scope=required,
            status=CourseVersionStatus.PUBLISHED,
            published_at=self._timestamp(),
        )
        self._put("course_versions", self._version_key(course_id, version_no), record)
        self._put("courses", course_id, replace(course, current_version=version_no))
        self._audit(
            "operator",
            "course.version_published",
            course_id=course_id,
            version_no=version_no,
            model_id=model_id,
        )
        self._migrate_scheduled_sessions(course_id, record)
        self._save()
        return record

    def _ensure_cross_border_exception(self, course_id: str, exception_task_id: str | None) -> None:
        if exception_task_id is None:
            raise GovernanceError("使用跨境数据来源需要已批准的例外")
        task = self._find("review_tasks", exception_task_id, ReviewTask)
        if task.kind != ReviewKind.EXCEPTION or task.status != ReviewStatus.APPROVED:
            raise GovernanceError("跨境例外未获批准")
        if task.detail.get("exception_kind") != "cross_border" or task.detail.get("course_id") != course_id:
            raise GovernanceError("跨境例外与课程不匹配")

    def _migrate_scheduled_sessions(self, course_id: str, version: CourseVersion) -> None:
        """未开始的场次切换到新版本；进行中和已完成的场次保持原版本。"""
        for session in self._all("sessions", Session):
            if session.course_id != course_id or session.status != SessionStatus.SCHEDULED:
                continue
            if session.course_version_no == version.version_no:
                continue
            session = replace(session, course_version_no=version.version_no)
            self._put("sessions", session.session_id, session)
            self._audit(
                "system",
                "session.migrated",
                session_id=session.session_id,
                course_id=course_id,
                version_no=version.version_no,
            )
            self._pause_enrollments_without_coverage(session, version)

    def _pause_enrollments_without_coverage(self, session: Session, version: CourseVersion) -> None:
        gaps: list[str] = []
        for enrollment in self._all("enrollments", Enrollment):
            if enrollment.session_id != session.session_id or enrollment.status != EnrollmentStatus.ENROLLED:
                continue
            consent = self._find("consents", enrollment.consent_id, Consent)
            covered = consent.status == ConsentStatus.ACTIVE and set(
                version.required_consent_scope
            ) <= set(consent.scope)
            if covered:
                continue
            enrollment = replace(enrollment, status=EnrollmentStatus.PAUSED)
            self._put("enrollments", enrollment.enrollment_id, enrollment)
            self._audit(
                "system",
                "enrollment.paused",
                enrollment_id=enrollment.enrollment_id,
                reason="授权范围不足",
            )
            gaps.append(enrollment.enrollment_id)
        if gaps:
            self._create_remediation(
                RemediationKind.CONSENT_GAP,
                subject_id=self._version_key(version.course_id, version.version_no),
                summary="课程版本升级后部分学员授权范围不足，报名已暂停",
                session_ids=(session.session_id,),
                enrollment_ids=tuple(gaps),
            )

    # -- 场次 ---------------------------------------------------------

    def schedule_session(
        self,
        course_id: str,
        instructor_id: str,
        scheduled_at: datetime | str,
        version_no: int | None = None,
    ) -> Session:
        course = self._find("courses", course_id, Course)
        if version_no is None:
            version_no = course.current_version
        if version_no is None:
            raise GovernanceError("课程尚未发布版本")
        version = self._get_version(course_id, version_no)
        if version.status != CourseVersionStatus.PUBLISHED:
            raise GovernanceError("课程版本未发布")
        self._ensure_instructor_available(instructor_id)
        if scheduled_at is None:
            raise GovernanceError("场次时间不能为空")
        record = Session(
            session_id=self._next_id("session"),
            course_id=course_id,
            course_version_no=version_no,
            instructor_id=instructor_id,
            scheduled_at=_iso(scheduled_at) or "",
            status=SessionStatus.SCHEDULED,
            paused_reason="",
        )
        self._put("sessions", record.session_id, record)
        self._audit(
            "operator",
            "session.scheduled",
            session_id=record.session_id,
            course_id=course_id,
            version_no=version_no,
        )
        self._save()
        return record

    def _ensure_instructor_available(self, instructor_id: str) -> Instructor:
        instructor = self._find("instructors", instructor_id, Instructor)
        if instructor.status != InstructorStatus.ACTIVE:
            raise GovernanceError("讲师状态不可用")
        institution = self._find("institutions", instructor.institution_id, Institution)
        if institution.status != InstitutionStatus.ACTIVE:
            raise GovernanceError("讲师所属机构已退出")
        return instructor

    def start_session(self, session_id: str) -> Session:
        session = self._find("sessions", session_id, Session)
        if session.status != SessionStatus.SCHEDULED:
            raise GovernanceError("场次不在待开始状态")
        self._ensure_instructor_available(session.instructor_id)
        session = replace(session, status=SessionStatus.IN_PROGRESS)
        self._put("sessions", session_id, session)
        self._audit("operator", "session.started", session_id=session_id)
        self._save()
        return session

    def complete_session(self, session_id: str) -> Session:
        session = self._find("sessions", session_id, Session)
        if session.status != SessionStatus.IN_PROGRESS:
            raise GovernanceError("场次不在进行状态")
        session = replace(session, status=SessionStatus.COMPLETED)
        self._put("sessions", session_id, session)
        self._audit("operator", "session.completed", session_id=session_id)
        self._save()
        return session

    def pause_session(self, session_id: str, reason: str, actor: str = "operator") -> Session:
        """明确暂停一个场次（新增风险只影响尚未开始或明确受影响的场次）。"""
        session = self._find("sessions", session_id, Session)
        if session.status not in (SessionStatus.SCHEDULED, SessionStatus.IN_PROGRESS):
            raise GovernanceError("场次状态不可暂停")
        session = replace(session, status=SessionStatus.PAUSED, paused_reason=reason)
        self._put("sessions", session_id, session)
        self._audit(actor, "session.paused", session_id=session_id, reason=reason)
        self._save()
        return session

    def resume_session(self, session_id: str, actor: str = "operator") -> Session:
        session = self._find("sessions", session_id, Session)
        if session.status != SessionStatus.PAUSED:
            raise GovernanceError("场次不在暂停状态")
        self._ensure_instructor_available(session.instructor_id)
        session = replace(session, status=SessionStatus.SCHEDULED, paused_reason="")
        self._put("sessions", session_id, session)
        self._audit(actor, "session.resumed", session_id=session_id)
        self._save()
        return session

    # -- 授权与报名 ----------------------------------------------------

    def grant_consent(
        self,
        consent_id: str,
        learner_id: str,
        scope: Iterable[str],
        expires_at: datetime | str | None = None,
    ) -> Consent:
        if consent_id in self._state["consents"]:
            raise GovernanceError("授权已存在")
        if not tuple(scope):
            raise GovernanceError("授权范围不能为空")
        record = Consent(
            consent_id=consent_id,
            learner_id=learner_id,
            scope=tuple(scope),
            status=ConsentStatus.ACTIVE,
            granted_at=self._timestamp(),
            revoked_at=None,
            expires_at=_iso(expires_at),
        )
        self._put("consents", consent_id, record)
        if record.expires_at:
            self._create_reminder(ReminderKind.CONSENT_EXPIRING, consent_id, record.expires_at)
        self._audit(learner_id, "consent.granted", consent_id=consent_id, scope=list(record.scope))
        self._save()
        return record

    def revoke_consent(self, consent_id: str, actor: str = "learner") -> RemediationTask:
        """学员取消授权：暂停相关报名、保留已发生记录、生成补救任务。"""
        consent = self._find("consents", consent_id, Consent)
        if consent.status != ConsentStatus.ACTIVE:
            raise GovernanceError("授权已撤销")
        consent = replace(consent, status=ConsentStatus.REVOKED, revoked_at=self._timestamp())
        self._put("consents", consent_id, consent)
        self._cancel_reminders(ReminderKind.CONSENT_EXPIRING, consent_id)
        paused_enrollments: list[str] = []
        session_ids: list[str] = []
        for enrollment in self._all("enrollments", Enrollment):
            if enrollment.consent_id != consent_id or enrollment.status != EnrollmentStatus.ENROLLED:
                continue
            enrollment = replace(enrollment, status=EnrollmentStatus.PAUSED)
            self._put("enrollments", enrollment.enrollment_id, enrollment)
            paused_enrollments.append(enrollment.enrollment_id)
            session_ids.append(enrollment.session_id)
            self._audit(
                "system",
                "enrollment.paused",
                enrollment_id=enrollment.enrollment_id,
                reason="学员取消授权",
            )
        task = self._create_remediation(
            RemediationKind.CONSENT_REVOKED,
            subject_id=consent_id,
            summary=f"学员 {consent.learner_id} 取消授权，相关报名已暂停，已发生记录保留",
            session_ids=tuple(sorted(set(session_ids))),
            enrollment_ids=tuple(paused_enrollments),
        )
        self._audit(actor, "consent.revoked", consent_id=consent_id)
        self._save()
        return task

    def enroll(self, session_id: str, learner_id: str, consent_id: str) -> Enrollment:
        session = self._find("sessions", session_id, Session)
        if session.status != SessionStatus.SCHEDULED:
            raise GovernanceError("场次不接受报名")
        consent = self._find("consents", consent_id, Consent)
        if consent.learner_id != learner_id:
            raise GovernanceError("授权与学员不匹配")
        if consent.status != ConsentStatus.ACTIVE:
            raise GovernanceError("授权已撤销")
        if consent.expires_at and _parse(consent.expires_at) <= self._now():
            raise GovernanceError("授权已过期")
        version = self._get_version(session.course_id, session.course_version_no)
        missing = set(version.required_consent_scope) - set(consent.scope)
        if missing:
            raise GovernanceError(f"授权范围不足: {sorted(missing)}")
        for existing in self._all("enrollments", Enrollment):
            if (
                existing.session_id == session_id
                and existing.learner_id == learner_id
                and existing.status in (EnrollmentStatus.ENROLLED, EnrollmentStatus.COMPLETED)
            ):
                raise GovernanceError("学员已报名该场次")
        record = Enrollment(
            enrollment_id=self._next_id("enrollment"),
            session_id=session_id,
            learner_id=learner_id,
            consent_id=consent_id,
            status=EnrollmentStatus.ENROLLED,
        )
        self._put("enrollments", record.enrollment_id, record)
        self._audit(learner_id, "enrollment.created", enrollment_id=record.enrollment_id, session_id=session_id)
        self._save()
        return record

    # -- 学习证明 ------------------------------------------------------

    @staticmethod
    def _certificate_digest(certificate: Certificate) -> str:
        payload = certificate.to_dict()
        payload.pop("digest", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def issue_certificate(self, enrollment_id: str, instructor_id: str) -> Certificate:
        """由场次讲师确认后签发；证书固定当时的课程版本、授权与确认人。"""
        enrollment = self._find("enrollments", enrollment_id, Enrollment)
        session = self._find("sessions", enrollment.session_id, Session)
        if session.status != SessionStatus.COMPLETED:
            raise GovernanceError("场次尚未完成，不能签发证明")
        if session.instructor_id != instructor_id:
            raise GovernanceError("需由该场次的讲师确认")
        if enrollment.status != EnrollmentStatus.ENROLLED:
            raise GovernanceError("报名状态不可签发证明")
        for existing in self._all("certificates", Certificate):
            if existing.enrollment_id == enrollment_id:
                raise GovernanceError("证明已签发，不能重复或改写")
        version = self._get_version(session.course_id, session.course_version_no)
        certificate = Certificate(
            certificate_id=self._next_id("cert"),
            enrollment_id=enrollment_id,
            learner_id=enrollment.learner_id,
            course_id=session.course_id,
            course_version_no=session.course_version_no,
            material_version_id=version.material_version_id,
            model_id=version.model_id,
            consent_id=enrollment.consent_id,
            confirmed_by=instructor_id,
            issued_at=self._timestamp(),
            digest="",
        )
        certificate = replace(certificate, digest=self._certificate_digest(certificate))
        self._put("certificates", certificate.certificate_id, certificate)
        self._put(
            "enrollments",
            enrollment_id,
            replace(enrollment, status=EnrollmentStatus.COMPLETED),
        )
        self._audit(
            instructor_id,
            "certificate.issued",
            certificate_id=certificate.certificate_id,
            enrollment_id=enrollment_id,
        )
        self._save()
        return certificate

    def verify_certificate(self, certificate_id: str) -> bool:
        certificate = self._find("certificates", certificate_id, Certificate)
        return certificate.digest == self._certificate_digest(certificate)

    def certificate_lineage(self, certificate_id: str) -> dict[str, Any]:
        """管理人员视角：证书使用了哪一版课程、哪项授权、哪位讲师确认。"""
        certificate = self._find("certificates", certificate_id, Certificate)
        version = self._get_version(certificate.course_id, certificate.course_version_no)
        return {
            "certificate_id": certificate.certificate_id,
            "learner_id": certificate.learner_id,
            "course_id": certificate.course_id,
            "course_version_no": certificate.course_version_no,
            "material_version_id": certificate.material_version_id,
            "model_id": certificate.model_id,
            "model_purpose": version.model_purpose,
            "consent_id": certificate.consent_id,
            "confirmed_by": certificate.confirmed_by,
            "issued_at": certificate.issued_at,
            "digest": certificate.digest,
            "verified": self.verify_certificate(certificate_id),
        }

    # -- 例外审批 ------------------------------------------------------

    def request_exception(
        self,
        requester_id: str,
        exception_kind: str,
        course_id: str,
        justification: str,
        data_source_ids: Iterable[str] = (),
    ) -> ReviewTask:
        self._find("courses", course_id, Course)
        task = ReviewTask(
            task_id=self._next_id("review"),
            kind=ReviewKind.EXCEPTION,
            subject_id=course_id,
            submitted_by=requester_id,
            status=ReviewStatus.PENDING,
            note="",
            created_at=self._timestamp(),
            due_at=None,
            decided_by=None,
            decided_at=None,
            detail={
                "exception_kind": exception_kind,
                "course_id": course_id,
                "justification": justification,
                "data_source_ids": list(data_source_ids),
            },
        )
        self._put("review_tasks", task.task_id, task)
        self._audit(requester_id, "exception.requested", task_id=task.task_id, exception_kind=exception_kind)
        self._save()
        return task

    def decide_exception(self, task_id: str, approver_id: str, approve: bool, note: str = "") -> ReviewTask:
        """例外只能由无利益关联的隐私审核人员批准。"""
        task = self._find("review_tasks", task_id, ReviewTask)
        if task.kind != ReviewKind.EXCEPTION:
            raise GovernanceError("该任务不是例外审批")
        officer = self._find("officers", approver_id, PrivacyOfficer)
        self._ensure_independent(task, officer)
        task = replace(
            task,
            status=ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED,
            decided_by=approver_id,
            decided_at=self._timestamp(),
            note=note,
        )
        self._put("review_tasks", task.task_id, task)
        self._audit(approver_id, "exception.decided", task_id=task.task_id, approved=approve)
        self._save()
        return task

    # -- 事件处置与补救 --------------------------------------------------

    def _create_remediation(
        self,
        kind: RemediationKind,
        subject_id: str,
        summary: str,
        session_ids: Iterable[str] = (),
        enrollment_ids: Iterable[str] = (),
        note: str = "",
    ) -> RemediationTask:
        task = RemediationTask(
            task_id=self._next_id("remediation"),
            kind=kind,
            subject_id=subject_id,
            summary=summary,
            status=RemediationStatus.OPEN,
            created_at=self._timestamp(),
            resolved_at=None,
            session_ids=tuple(session_ids),
            enrollment_ids=tuple(enrollment_ids),
            note=note,
        )
        self._put("remediation_tasks", task.task_id, task)
        self._audit("system", "remediation.created", task_id=task.task_id, kind=kind.value)
        return task

    def resolve_remediation(self, task_id: str, note: str = "", actor: str = "operator") -> RemediationTask:
        task = self._find("remediation_tasks", task_id, RemediationTask)
        if task.status == RemediationStatus.RESOLVED:
            raise GovernanceError("补救任务已办结")
        task = replace(
            task,
            status=RemediationStatus.RESOLVED,
            resolved_at=self._timestamp(),
            note=note or task.note,
        )
        self._put("remediation_tasks", task_id, task)
        self._audit(actor, "remediation.resolved", task_id=task_id)
        self._save()
        return task

    def withdraw_institution(self, institution_id: str, reason: str = "") -> RemediationTask:
        """合作机构退出：讲师停职、未完成场次暂停、生成补救任务。"""
        institution = self._find("institutions", institution_id, Institution)
        if institution.status != InstitutionStatus.ACTIVE:
            raise GovernanceError("机构已退出")
        self._put(
            "institutions",
            institution_id,
            replace(institution, status=InstitutionStatus.WITHDRAWN),
        )
        suspended: list[str] = []
        for instructor in self._all("instructors", Instructor):
            if instructor.institution_id == institution_id and instructor.status == InstructorStatus.ACTIVE:
                self._put(
                    "instructors",
                    instructor.instructor_id,
                    replace(instructor, status=InstructorStatus.SUSPENDED),
                )
                suspended.append(instructor.instructor_id)
        paused_sessions, affected_enrollments = self._pause_sessions(
            lambda session: session.instructor_id in suspended,
            reason or "合作机构退出",
        )
        task = self._create_remediation(
            RemediationKind.INSTITUTION_WITHDRAWN,
            subject_id=institution_id,
            summary=f"合作机构退出：{reason or '未说明原因'}，相关场次已暂停",
            session_ids=paused_sessions,
            enrollment_ids=affected_enrollments,
        )
        self._audit("operator", "institution.withdrawn", institution_id=institution_id)
        self._save()
        return task

    def flag_material(self, material_version_id: str, reason: str, reporter_id: str = "operator") -> RemediationTask:
        """教材被发现含有不应公开的资料：标记教材、暂停相关场次、保留已发生记录。"""
        material = self._find("material_versions", material_version_id, MaterialVersion)
        if material.status == MaterialStatus.FLAGGED:
            raise GovernanceError("教材已被标记")
        self._put(
            "material_versions",
            material_version_id,
            replace(material, status=MaterialStatus.FLAGGED),
        )
        version_keys = {
            key
            for key, raw in self._state["course_versions"].items()
            if raw["material_version_id"] == material_version_id
        }
        paused_sessions, affected_enrollments = self._pause_sessions(
            lambda session: self._version_key(session.course_id, session.course_version_no) in version_keys,
            f"教材被标记：{reason}",
        )
        completed = [
            session.session_id
            for session in self._all("sessions", Session)
            if self._version_key(session.course_id, session.course_version_no) in version_keys
            and session.status == SessionStatus.COMPLETED
        ]
        note = f"已发生场次保留记录: {', '.join(completed)}" if completed else ""
        task = self._create_remediation(
            RemediationKind.MATERIAL_FLAGGED,
            subject_id=material_version_id,
            summary=f"教材含有不应公开的资料：{reason}",
            session_ids=paused_sessions,
            enrollment_ids=affected_enrollments,
            note=note,
        )
        self._audit(reporter_id, "material.flagged", material_version_id=material_version_id, reason=reason)
        self._save()
        return task

    def _pause_sessions(self, matches: Callable[[Session], bool], reason: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """暂停符合条件的未完成场次，返回受影响场次与报名编号。"""
        paused: list[str] = []
        enrollments: list[str] = []
        for session in self._all("sessions", Session):
            if session.status not in (SessionStatus.SCHEDULED, SessionStatus.IN_PROGRESS):
                continue
            if not matches(session):
                continue
            self._put(
                "sessions",
                session.session_id,
                replace(session, status=SessionStatus.PAUSED, paused_reason=reason),
            )
            paused.append(session.session_id)
            self._audit("system", "session.paused", session_id=session.session_id, reason=reason)
            for enrollment in self._all("enrollments", Enrollment):
                if enrollment.session_id == session.session_id and enrollment.status == EnrollmentStatus.ENROLLED:
                    enrollments.append(enrollment.enrollment_id)
        return tuple(paused), tuple(enrollments)

    # -- 提醒与重启接续 --------------------------------------------------

    def _create_reminder(self, kind: ReminderKind, subject_id: str, due_at: str) -> Reminder:
        reminder = Reminder(
            reminder_id=self._next_id("reminder"),
            kind=kind,
            subject_id=subject_id,
            due_at=due_at,
            status=ReminderStatus.PENDING,
            created_at=self._timestamp(),
            sent_at=None,
        )
        self._put("reminders", reminder.reminder_id, reminder)
        self._audit(
            "system",
            "reminder.created",
            reminder_id=reminder.reminder_id,
            kind=kind.value,
            subject_id=subject_id,
        )
        return reminder

    def _cancel_reminders(self, kind: ReminderKind, subject_id: str) -> None:
        for key, raw in self._state["reminders"].items():
            reminder = Reminder.from_dict(raw)
            if (
                reminder.kind == kind
                and reminder.subject_id == subject_id
                and reminder.status == ReminderStatus.PENDING
            ):
                self._put("reminders", key, replace(reminder, status=ReminderStatus.CANCELLED))

    def _has_open_reminder(self, kind: ReminderKind, subject_id: str) -> bool:
        return any(
            reminder.kind == kind
            and reminder.subject_id == subject_id
            and reminder.status != ReminderStatus.CANCELLED
            for reminder in self._all("reminders", Reminder)
        )

    def recover(self) -> RecoveryReport:
        """重启后接续：撤销到期授权、补发过期提醒、汇总待复核与补救事项。"""
        now = self._now()
        expired: list[str] = []
        for consent in self._all("consents", Consent):
            if (
                consent.status == ConsentStatus.ACTIVE
                and consent.expires_at
                and _parse(consent.expires_at) <= now
            ):
                self.revoke_consent(consent.consent_id, actor="system")
                expired.append(consent.consent_id)
        for task in self._all("review_tasks", ReviewTask):
            if task.status == ReviewStatus.PENDING and task.due_at and _parse(task.due_at) <= now:
                if not self._has_open_reminder(ReminderKind.REVIEW_OVERDUE, task.task_id):
                    self._create_reminder(ReminderKind.REVIEW_OVERDUE, task.task_id, task.due_at)
        emitted: list[str] = []
        for reminder in self._all("reminders", Reminder):
            if reminder.status == ReminderStatus.PENDING and _parse(reminder.due_at) <= now:
                self._put(
                    "reminders",
                    reminder.reminder_id,
                    replace(reminder, status=ReminderStatus.SENT, sent_at=self._timestamp()),
                )
                emitted.append(reminder.reminder_id)
                self._audit(
                    "system",
                    "reminder.emitted",
                    reminder_id=reminder.reminder_id,
                    kind=reminder.kind.value,
                )
        pending = [task for task in self._all("review_tasks", ReviewTask) if task.status == ReviewStatus.PENDING]
        report = RecoveryReport(
            emitted_reminders=tuple(emitted),
            expired_consents=tuple(expired),
            overdue_reviews=tuple(
                task.task_id for task in pending if task.due_at and _parse(task.due_at) <= now
            ),
            pending_reviews=tuple(task.task_id for task in pending),
            open_remediations=tuple(
                task.task_id
                for task in self._all("remediation_tasks", RemediationTask)
                if task.status != RemediationStatus.RESOLVED
            ),
        )
        self._save()
        return report

    # -- 查询 ------------------------------------------------------------

    def course(self, course_id: str) -> Course:
        return self._find("courses", course_id, Course)

    def course_version(self, course_id: str, version_no: int) -> CourseVersion:
        return self._get_version(course_id, version_no)

    def institution(self, institution_id: str) -> Institution:
        return self._find("institutions", institution_id, Institution)

    def instructor(self, instructor_id: str) -> Instructor:
        return self._find("instructors", instructor_id, Instructor)

    def material_version(self, material_version_id: str) -> MaterialVersion:
        return self._find("material_versions", material_version_id, MaterialVersion)

    def consent(self, consent_id: str) -> Consent:
        return self._find("consents", consent_id, Consent)

    def session(self, session_id: str) -> Session:
        return self._find("sessions", session_id, Session)

    def enrollment(self, enrollment_id: str) -> Enrollment:
        return self._find("enrollments", enrollment_id, Enrollment)

    def certificate(self, certificate_id: str) -> Certificate:
        return self._find("certificates", certificate_id, Certificate)

    def review_task(self, task_id: str) -> ReviewTask:
        return self._find("review_tasks", task_id, ReviewTask)

    def remediation_task(self, task_id: str) -> RemediationTask:
        return self._find("remediation_tasks", task_id, RemediationTask)

    def pending_reviews(self) -> list[ReviewTask]:
        return [
            task
            for task in self._all("review_tasks", ReviewTask)
            if task.status == ReviewStatus.PENDING
        ]

    def open_remediations(self) -> list[RemediationTask]:
        return [
            task
            for task in self._all("remediation_tasks", RemediationTask)
            if task.status != RemediationStatus.RESOLVED
        ]

    def audit_trail(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._state["audit"]]
