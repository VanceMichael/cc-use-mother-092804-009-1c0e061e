"""课程治理服务的行为测试。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.governance import (
    ConsentStatus,
    EnrollmentStatus,
    GovernanceError,
    GovernanceService,
    InstructorStatus,
    JsonStore,
    MaterialStatus,
    RemediationKind,
    ReviewStatus,
    SessionStatus,
)

BASE = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


class Clock:
    """可手动推进的时钟，便于测试到期与恢复逻辑。"""

    def __init__(self, moment: datetime = BASE):
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment += timedelta(**kwargs)


class GovernanceServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = Clock()
        self.service = self._new_service()
        self.service.register_institution("org-1", "示例机构", ["培训资质"])
        self.service.register_instructor("instructor-1", "讲师甲", "org-1", ["人工智能教学"])
        self.service.register_privacy_officer("officer-1", "审核员甲")
        self.service.register_data_source("ds-1", "练习数据集", "公开资料", "training-data")
        self.service.create_course("course-1", "人工智能入门", ["理解基本概念", "完成课堂练习"])

    def _new_service(self) -> GovernanceService:
        return GovernanceService(JsonStore(Path(self._tmp.name) / "state.json"), now=self.clock)

    def _restart(self) -> GovernanceService:
        self.service = self._new_service()
        return self.service

    def _approved_material(self, content_ref: str = "materials/v1.pdf"):
        material = self.service.submit_material_version("course-1", "instructor-1", content_ref)
        (task,) = [t for t in self.service.pending_reviews() if t.subject_id == material.material_version_id]
        self.service.review_material(task.task_id, "officer-1", approve=True)
        return material

    def _published(self, model_id: str = "model-1", scope=("training-data",)):
        material = self._approved_material(f"materials/{model_id}.pdf")
        return self.service.publish_course_version(
            "course-1",
            material.material_version_id,
            model_id=model_id,
            model_purpose="课堂演示",
            data_source_ids=["ds-1"],
            required_consent_scope=list(scope),
        )

    def _enrolled_session(self, learner_id: str = "learner-1", consent_id: str = "consent-1"):
        session = self.service.schedule_session("course-1", "instructor-1", BASE + timedelta(days=7))
        self.service.grant_consent(consent_id, learner_id, ["training-data"])
        enrollment = self.service.enroll(session.session_id, learner_id, consent_id)
        return session, enrollment

    # -- 主流程与证书可追溯 ----------------------------------------------

    def test_full_flow_issues_traceable_certificate(self) -> None:
        self._published()
        session, enrollment = self._enrolled_session()
        self.service.start_session(session.session_id)
        self.service.complete_session(session.session_id)
        certificate = self.service.issue_certificate(enrollment.enrollment_id, "instructor-1")

        lineage = self.service.certificate_lineage(certificate.certificate_id)
        self.assertEqual(lineage["course_id"], "course-1")
        self.assertEqual(lineage["course_version_no"], 1)
        self.assertEqual(lineage["model_id"], "model-1")
        self.assertEqual(lineage["consent_id"], "consent-1")
        self.assertEqual(lineage["confirmed_by"], "instructor-1")
        self.assertTrue(lineage["verified"])
        self.assertEqual(
            self.service.enrollment(enrollment.enrollment_id).status,
            EnrollmentStatus.COMPLETED,
        )

    def test_certificate_requires_completed_session_and_confirming_instructor(self) -> None:
        self._published()
        session, enrollment = self._enrolled_session()
        with self.assertRaises(GovernanceError):
            self.service.issue_certificate(enrollment.enrollment_id, "instructor-1")
        self.service.start_session(session.session_id)
        self.service.complete_session(session.session_id)
        with self.assertRaises(GovernanceError):
            self.service.issue_certificate(enrollment.enrollment_id, "instructor-2")
        self.service.issue_certificate(enrollment.enrollment_id, "instructor-1")
        with self.assertRaises(GovernanceError):
            self.service.issue_certificate(enrollment.enrollment_id, "instructor-1")

    # -- 模型升级：证书不被改写，新增风险只影响未开始或明确受影响的场次 ------

    def test_model_upgrade_keeps_completed_certificate_and_moves_only_unstarted_sessions(self) -> None:
        self._published(model_id="model-1")
        done, enrollment = self._enrolled_session()
        in_progress = self.service.schedule_session("course-1", "instructor-1", BASE + timedelta(days=3))
        later = self.service.schedule_session("course-1", "instructor-1", BASE + timedelta(days=8))
        self.service.start_session(done.session_id)
        self.service.complete_session(done.session_id)
        certificate = self.service.issue_certificate(enrollment.enrollment_id, "instructor-1")
        self.service.start_session(in_progress.session_id)
        digest_before = certificate.digest

        self._published(model_id="model-2")

        lineage = self.service.certificate_lineage(certificate.certificate_id)
        self.assertEqual(lineage["course_version_no"], 1)
        self.assertEqual(lineage["model_id"], "model-1")
        self.assertEqual(lineage["digest"], digest_before)
        self.assertTrue(lineage["verified"])
        self.assertEqual(self.service.session(done.session_id).course_version_no, 1)
        self.assertEqual(self.service.session(in_progress.session_id).course_version_no, 1)
        self.assertEqual(self.service.session(later.session_id).course_version_no, 2)

        # 进行中的场次不受影响，除非运营明确暂停
        self.service.pause_session(in_progress.session_id, "新模型风险需评估")
        self.assertEqual(self.service.session(in_progress.session_id).status, SessionStatus.PAUSED)

    def test_upgrade_pauses_enrollments_lacking_new_scope(self) -> None:
        self._published()
        session, enrollment = self._enrolled_session()
        material = self._approved_material("materials/v2.pdf")
        self.service.publish_course_version(
            "course-1",
            material.material_version_id,
            model_id="model-2",
            model_purpose="课堂演示",
            data_source_ids=["ds-1"],
            required_consent_scope=["training-data", "learning-record"],
        )
        self.assertEqual(self.service.session(session.session_id).course_version_no, 2)
        self.assertEqual(
            self.service.enrollment(enrollment.enrollment_id).status,
            EnrollmentStatus.PAUSED,
        )
        (task,) = self.service.open_remediations()
        self.assertEqual(task.kind, RemediationKind.CONSENT_GAP)
        self.assertIn(enrollment.enrollment_id, task.enrollment_ids)

    # -- 教材审核与利益冲突回避 --------------------------------------------

    def test_submitter_cannot_review_own_material(self) -> None:
        self.service.register_privacy_officer("instructor-1", "讲师甲")
        self.service.submit_material_version("course-1", "instructor-1", "materials/v1.pdf")
        (task,) = self.service.pending_reviews()
        with self.assertRaises(GovernanceError):
            self.service.review_material(task.task_id, "instructor-1", approve=True)

    def test_same_institution_officer_cannot_review(self) -> None:
        self.service.register_privacy_officer("officer-2", "审核员乙", institution_id="org-1")
        self.service.submit_material_version("course-1", "instructor-1", "materials/v1.pdf")
        (task,) = self.service.pending_reviews()
        with self.assertRaises(GovernanceError):
            self.service.review_material(task.task_id, "officer-2", approve=True)

    def test_review_requires_registered_officer_and_pending_task(self) -> None:
        self.service.submit_material_version("course-1", "instructor-1", "materials/v1.pdf")
        (task,) = self.service.pending_reviews()
        with self.assertRaises(GovernanceError):
            self.service.review_material(task.task_id, "nobody", approve=True)
        self.service.review_material(task.task_id, "officer-1", approve=True)
        with self.assertRaises(GovernanceError):
            self.service.review_material(task.task_id, "officer-1", approve=True)

    def test_exception_cannot_be_approved_by_stakeholder(self) -> None:
        self.service.register_privacy_officer("officer-2", "审核员乙", institution_id="org-1")
        task = self.service.request_exception("instructor-1", "cross_border", "course-1", "需要使用境外模型")
        with self.assertRaises(GovernanceError):
            self.service.decide_exception(task.task_id, "officer-2", approve=True)
        own = self.service.request_exception("officer-1", "cross_border", "course-1", "自审测试")
        with self.assertRaises(GovernanceError):
            self.service.decide_exception(own.task_id, "officer-1", approve=True)
        decided = self.service.decide_exception(task.task_id, "officer-1", approve=True)
        self.assertEqual(decided.status, ReviewStatus.APPROVED)

    # -- 发布校验 ----------------------------------------------------------

    def test_publish_requires_approved_material_and_covering_scope(self) -> None:
        material = self.service.submit_material_version("course-1", "instructor-1", "materials/v1.pdf")
        with self.assertRaises(GovernanceError):
            self.service.publish_course_version(
                "course-1", material.material_version_id, "model-1", "演示", ["ds-1"], ["training-data"]
            )
        (task,) = self.service.pending_reviews()
        self.service.review_material(task.task_id, "officer-1", approve=True)
        with self.assertRaises(GovernanceError):
            self.service.publish_course_version(
                "course-1", material.material_version_id, "model-1", "演示", ["ds-1"], ["other-scope"]
            )

    def test_cross_border_data_source_requires_approved_exception(self) -> None:
        self.service.register_data_source("ds-x", "境外数据", "合作方", "training-data", cross_border=True)
        material = self._approved_material()
        with self.assertRaises(GovernanceError):
            self.service.publish_course_version(
                "course-1", material.material_version_id, "model-1", "演示", ["ds-x"], ["training-data"]
            )
        task = self.service.request_exception("instructor-1", "cross_border", "course-1", "课程需要使用境外数据")
        self.service.decide_exception(task.task_id, "officer-1", approve=True)
        version = self.service.publish_course_version(
            "course-1",
            material.material_version_id,
            "model-1",
            "演示",
            ["ds-x"],
            ["training-data"],
            exception_task_id=task.task_id,
        )
        self.assertEqual(version.version_no, 1)

    # -- 报名与授权 ----------------------------------------------------------

    def test_enroll_requires_matching_active_consent_with_sufficient_scope(self) -> None:
        self._published()
        session = self.service.schedule_session("course-1", "instructor-1", BASE + timedelta(days=7))
        self.service.grant_consent("consent-1", "learner-1", ["other-scope"])
        with self.assertRaises(GovernanceError):
            self.service.enroll(session.session_id, "learner-1", "consent-1")
        with self.assertRaises(GovernanceError):
            self.service.enroll(session.session_id, "learner-2", "consent-1")

    def test_consent_revocation_pauses_enrollment_and_preserves_records(self) -> None:
        self._published()
        session, enrollment = self._enrolled_session()
        task = self.service.revoke_consent("consent-1")
        self.assertEqual(self.service.consent("consent-1").status, ConsentStatus.REVOKED)
        self.assertEqual(
            self.service.enrollment(enrollment.enrollment_id).status,
            EnrollmentStatus.PAUSED,
        )
        self.assertEqual(task.kind, RemediationKind.CONSENT_REVOKED)
        self.assertIn(enrollment.enrollment_id, task.enrollment_ids)
        self.assertIn(session.session_id, task.session_ids)
        with self.assertRaises(GovernanceError):
            self.service.enroll(session.session_id, "learner-1", "consent-1")

    # -- 机构退出与教材标记 --------------------------------------------------

    def test_institution_withdrawal_suspends_instructors_and_pauses_sessions(self) -> None:
        self._published()
        session, _ = self._enrolled_session()
        task = self.service.withdraw_institution("org-1", "合作终止")
        self.assertEqual(self.service.session(session.session_id).status, SessionStatus.PAUSED)
        self.assertEqual(self.service.instructor("instructor-1").status, InstructorStatus.SUSPENDED)
        self.assertEqual(task.kind, RemediationKind.INSTITUTION_WITHDRAWN)
        self.assertIn(session.session_id, task.session_ids)
        with self.assertRaises(GovernanceError):
            self.service.schedule_session("course-1", "instructor-1", BASE + timedelta(days=9))

    def test_flagged_material_pauses_sessions_but_keeps_completed_records(self) -> None:
        material = self._approved_material()
        self.service.publish_course_version(
            "course-1", material.material_version_id, "model-1", "演示", ["ds-1"], ["training-data"]
        )
        done, enrollment = self._enrolled_session()
        future = self.service.schedule_session("course-1", "instructor-1", BASE + timedelta(days=9))
        self.service.start_session(done.session_id)
        self.service.complete_session(done.session_id)
        certificate = self.service.issue_certificate(enrollment.enrollment_id, "instructor-1")

        task = self.service.flag_material(material.material_version_id, "发现未公开个人信息")

        self.assertEqual(self.service.session(future.session_id).status, SessionStatus.PAUSED)
        self.assertEqual(self.service.session(done.session_id).status, SessionStatus.COMPLETED)
        self.assertTrue(self.service.verify_certificate(certificate.certificate_id))
        self.assertEqual(task.kind, RemediationKind.MATERIAL_FLAGGED)
        self.assertIn(future.session_id, task.session_ids)
        with self.assertRaises(GovernanceError):
            self.service.publish_course_version(
                "course-1", material.material_version_id, "model-2", "演示", ["ds-1"], ["training-data"]
            )

    # -- 重启接续 ------------------------------------------------------------

    def test_restart_recovers_expired_consents_and_overdue_reviews(self) -> None:
        self._published()
        session = self.service.schedule_session("course-1", "instructor-1", BASE + timedelta(days=7))
        self.service.grant_consent(
            "consent-1", "learner-1", ["training-data"], expires_at=BASE + timedelta(hours=6)
        )
        enrollment = self.service.enroll(session.session_id, "learner-1", "consent-1")
        self.service.submit_material_version(
            "course-1", "instructor-1", "materials/v2.pdf", due_at=BASE + timedelta(hours=1)
        )

        self.clock.advance(days=1)
        restarted = self._restart()
        report = restarted.recover()

        self.assertIn("consent-1", report.expired_consents)
        self.assertEqual(
            restarted.enrollment(enrollment.enrollment_id).status,
            EnrollmentStatus.PAUSED,
        )
        self.assertTrue(report.emitted_reminders)
        self.assertTrue(report.pending_reviews)
        self.assertTrue(report.overdue_reviews)
        self.assertTrue(report.open_remediations)

        again = restarted.recover()
        self.assertEqual(again.expired_consents, ())
        self.assertEqual(again.emitted_reminders, ())

    def test_state_survives_restart(self) -> None:
        material = self.service.submit_material_version("course-1", "instructor-1", "materials/v1.pdf")
        restarted = self._restart()
        (task,) = restarted.pending_reviews()
        restarted.review_material(task.task_id, "officer-1", approve=True)
        self.assertEqual(
            restarted.material_version(material.material_version_id).status,
            MaterialStatus.APPROVED,
        )

    # -- 审计 ----------------------------------------------------------------

    def test_audit_trail_records_key_actions(self) -> None:
        self._published()
        actions = [entry["action"] for entry in self.service.audit_trail()]
        self.assertIn("material.submitted", actions)
        self.assertIn("material.reviewed", actions)
        self.assertIn("course.version_published", actions)


if __name__ == "__main__":
    unittest.main()
