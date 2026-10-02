"""课程治理服务的领域规则测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.course_governance import (
    GovernanceError,
    InMemoryEventStore,
    JsonlEventStore,
    CourseGovernanceService,
    SESSION_COMPLETED,
    SESSION_SCHEDULED,
    SESSION_SUSPENDED,
    SCOPE_EXPLICIT,
    SCOPE_NOT_STARTED,
    _apply,
)


def iso(dt: datetime) -> str:
    return dt.isoformat()


class GovernanceFixture:
    """搭建一条可开课的完整链路。"""

    def __init__(self, now: datetime | None = None) -> None:
        self.current = now or datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
        self.service = CourseGovernanceService(InMemoryEventStore(), clock=lambda: self.current)

    def advance(self, days: int = 0, hours: int = 0) -> None:
        self.current += timedelta(days=days, hours=hours)

    def bootstrap(
        self,
        org_id: str = "ORG-HKAI",
        instructor_id: str = "INS-01",
        cross_border: bool = False,
        qualification_days: int = 365,
        approve_scope: bool = True,
    ) -> None:
        s = self.service
        s.register_organization(org_id, "香港智慧城市培训机构")
        s.register_privacy_officer("PO-01", "隐私负责人黄女士")
        s.register_operations_staff("OPS-01", "运营主管李先生")
        s.register_instructor(instructor_id, "讲师陈老师", org_id)
        s.verify_instructor_qualification(
            instructor_id,
            "AI 通识讲师认证 L2",
            iso(self.current),
            iso(self.current + timedelta(days=qualification_days)),
            verifier_id="OPS-01",
        )
        s.register_data_source("DS-01", "匿名化练习问答样本", cross_border, org_id)
        s.submit_data_scope("SCOPE-01", "DS-01", "仅课堂练习，不保存原始输入", instructor_id)
        if approve_scope:
            s.approve_data_scope("SCOPE-01", "PO-01", allows_cross_border=cross_border)
        s.register_course("CRS-01", "让市民理解生成式 AI 的用途与边界", org_id)

    def publish_v1(self) -> None:
        self.service.submit_material(
            "MAT-01", "CRS-01", "AI 通识教材 v1", "fp-v1-aaaa", ["DS-01"], "INS-01"
        )
        self.service.publish_course_version(
            "CRS-01", "MAT-01",
            model_provider="合作方甲",
            model_id="general-chat",
            model_version="2025-09",
            model_purpose="课堂演示问答",
            published_by="OPS-01",
        )

    def grant_consent(self, learner: str = "LRN-01", until_days: int = 60) -> None:
        self.service.grant_consent(
            f"CON-{learner}", learner, "DS-01", "课堂练习用途",
            iso(self.current + timedelta(days=until_days)),
        )

    def schedule_session(self, session_id: str = "SES-01", start_offset: int = 7) -> None:
        start = self.current + timedelta(days=start_offset)
        self.service.schedule_session(
            session_id, "CRS-01", 1, "INS-01",
            iso(start), iso(start + timedelta(hours=3)),
        )

    def finish_session_with_certificate(
        self,
        learner: str = "LRN-01",
        session_id: str = "SES-01",
        cert_id: str = "CERT-01",
        enroll: bool = True,
    ) -> None:
        if enroll:
            self.service.enroll_learner(session_id, learner, [f"CON-{learner}"])
        self.advance(days=7, hours=1)
        self.service.start_session(session_id)
        self.advance(hours=3)
        self.service.confirm_and_complete_session(session_id, "INS-01")
        self.service.issue_certificate(cert_id, learner, session_id)


class VersioningTest(unittest.TestCase):
    def test_model_upgrade_creates_new_version_without_touching_certificate(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        f.grant_consent()
        f.schedule_session()
        f.finish_session_with_certificate()

        trace = f.service.certificate_trace("CERT-01")
        self.assertEqual(trace["snapshot"]["version_no"], 1)
        self.assertEqual(trace["snapshot"]["model_version"], "2025-09")
        confirmed_seq = trace["snapshot"]["instructor_confirmed_event_seq"]

        # 升级模型 = 新教材、新版本；旧版本保持冻结。
        f.service.submit_material(
            "MAT-02", "CRS-01", "AI 通识教材 v2", "fp-v2-bbbb", ["DS-01"], "INS-01"
        )
        f.service.publish_course_version(
            "CRS-01", "MAT-02",
            model_provider="合作方甲",
            model_id="general-chat",
            model_version="2026-06",
            model_purpose="课堂演示问答",
            published_by="OPS-01",
        )

        trace_after = f.service.certificate_trace("CERT-01")
        self.assertEqual(trace_after, trace, "已签发证明不得被模型升级改写")
        self.assertEqual(trace_after["snapshot"]["instructor_confirmed_event_seq"], confirmed_seq)

    def test_version_numbers_must_increase_and_published_version_is_frozen(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        # MAT-01 已发布，不能再次发布同一教材。
        with self.assertRaisesRegex(GovernanceError, "版本一经发布即冻结"):
            f.service.publish_course_version(
                "CRS-01", "MAT-01", "合作方甲", "general-chat", "2025-10", "演示", "OPS-01"
            )
        course = f.service.state.courses["CRS-01"]
        self.assertEqual(course["next_version"], 2)


class GatekeepingTest(unittest.TestCase):
    def test_cannot_publish_without_privacy_scope_review(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.service.register_data_source("DS-02", "未经审核的练习数据", False, "ORG-HKAI")
        f.service.submit_material(
            "MAT-0X", "CRS-01", "未审核教材", "fp-x", ["DS-02"], "INS-01"
        )
        with self.assertRaisesRegex(GovernanceError, "未经隐私负责人审核"):
            f.service.publish_course_version(
                "CRS-01", "MAT-0X", "甲", "m", "1", "演示", "OPS-01"
            )

    def test_cross_border_data_requires_explicit_cross_border_approval(self) -> None:
        f = GovernanceFixture()
        f.bootstrap(cross_border=True, approve_scope=False)
        # 批准时未给出跨境许可必须被拒绝。
        with self.assertRaisesRegex(GovernanceError, "跨境练习数据必须获得明确的跨境使用批准"):
            f.service.approve_data_scope("SCOPE-01", "PO-01", allows_cross_border=False)
        f.service.approve_data_scope("SCOPE-01", "PO-01", allows_cross_border=True)

    def test_submitter_cannot_approve_own_data_scope(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.service.register_data_source("DS-09", "新样本", False, "ORG-HKAI")
        f.service.submit_data_scope("SCOPE-09", "DS-09", "范围", "PO-01")
        with self.assertRaisesRegex(GovernanceError, "不得审核自己提交"):
            f.service.approve_data_scope("SCOPE-09", "PO-01")

    def test_expired_qualification_blocks_publish(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.advance(days=400)  # 资质已过期
        f.service.submit_material(
            "MAT-02", "CRS-01", "过期资质教材", "fp-2", ["DS-01"], "INS-01"
        )
        with self.assertRaisesRegex(GovernanceError, "讲师资质"):
            f.service.publish_course_version(
                "CRS-01", "MAT-02", "甲", "m", "2", "演示", "OPS-01"
            )

    def test_expired_consent_blocks_enrollment(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        f.grant_consent(until_days=3)
        f.schedule_session(start_offset=7)
        with self.assertRaisesRegex(GovernanceError, "在场次结束前到期"):
            f.service.enroll_learner("SES-01", "LRN-01", ["CON-LRN-01"])


class ExceptionSeparationTest(unittest.TestCase):
    def _request(self, f: GovernanceFixture, requester: str, stakeholders: list[str]) -> str:
        f.service.request_exception(
            "EX-01", "data_scope", "DS-01", "紧急场次需扩大练习数据范围",
            requester_id=requester, stakeholder_ids=stakeholders,
        )
        return "EX-01"

    def test_requester_cannot_approve_own_exception(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        # 让隐私负责人本人成为申请人
        f.service.register_data_source("DS-03", "样本", False, "ORG-HKAI")
        f.service.submit_data_scope("SCOPE-03", "DS-03", "范围", "INS-01")
        request_id = self._request(f, requester="PO-01", stakeholders=[])
        with self.assertRaisesRegex(GovernanceError, "不得审批自己提出的例外"):
            f.service.approve_exception(request_id, "PO-01")

    def test_stakeholder_cannot_approve(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.service.register_privacy_officer("PO-02", "利益相关隐私主任")
        request_id = self._request(f, requester="INS-01", stakeholders=["PO-02"])
        with self.assertRaisesRegex(GovernanceError, "利益相关方不得审批"):
            f.service.approve_exception(request_id, "PO-02")

    def test_independent_officer_can_approve(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        request_id = self._request(f, requester="INS-01", stakeholders=[])
        event = f.service.approve_exception(request_id, "PO-01", note="限定单次活动")
        self.assertEqual(event.type, "ExceptionApproved")


class RiskScopeTest(unittest.TestCase):
    def test_risk_only_suspends_not_started_sessions_of_named_version(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        f.grant_consent("LRN-01")
        f.schedule_session("SES-01")
        f.service.enroll_learner("SES-01", "LRN-01", ["CON-LRN-01"])
        f.finish_session_with_certificate("LRN-01", "SES-01", "CERT-01", enroll=False)

        # 再排两个未开始场次。
        f.schedule_session("SES-02", start_offset=14)
        f.schedule_session("SES-03", start_offset=21)

        events = f.service.register_risk(
            "RISK-01", "2025-09 模型在某类问题上出现误导",
            scope=SCOPE_NOT_STARTED, course_id="CRS-01", version_no=1,
        )
        types = [e.type for e in events]
        self.assertIn("SessionsSuspended", types)
        self.assertEqual(f.service.state.sessions["SES-01"]["status"], SESSION_COMPLETED)
        self.assertEqual(f.service.state.sessions["SES-02"]["status"], SESSION_SUSPENDED)
        self.assertEqual(f.service.state.sessions["SES-03"]["status"], SESSION_SUSPENDED)
        # 已完成场次不暂停、证明不变。
        self.assertEqual(
            f.service.certificate_trace("CERT-01")["snapshot"]["model_version"], "2025-09"
        )

    def test_explicit_scope_cannot_suspend_completed_session(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        f.grant_consent()
        f.schedule_session()
        f.service.enroll_learner("SES-01", "LRN-01", ["CON-LRN-01"])
        f.finish_session_with_certificate()

        events = f.service.register_risk(
            "RISK-02", "教材某案例需复核",
            scope=SCOPE_EXPLICIT, course_id="CRS-01", version_no=1,
            explicit_session_ids=["SES-01"],
        )
        self.assertEqual(f.service.state.sessions["SES-01"]["status"], SESSION_COMPLETED)
        # 已完成场次只能进入补救任务，不能暂停。
        task_descriptions = [
            task["description"] for task in f.service.state.tasks.values()
        ]
        self.assertTrue(any("已签发学习证明" in d for d in task_descriptions))
        self.assertNotIn("SessionsSuspended", [e.type for e in events])

    def test_risk_on_v2_does_not_touch_v1_sessions(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        f.grant_consent()
        f.schedule_session("SES-V1")
        # 发布 v2 并排一场 v2（helper 固定版本 1，直接调用服务）。
        f.service.submit_material(
            "MAT-02", "CRS-01", "v2", "fp-v2", ["DS-01"], "INS-01"
        )
        f.service.publish_course_version(
            "CRS-01", "MAT-02", "甲", "m", "2026-06", "演示", "OPS-01"
        )
        start = f.current + timedelta(days=30)
        f.service.schedule_session(
            "SES-V2", "CRS-01", 2, "INS-01", iso(start), iso(start + timedelta(hours=3))
        )

        f.service.register_risk(
            "RISK-V2", "新模型存在注入风险",
            scope=SCOPE_NOT_STARTED, course_id="CRS-01", version_no=2,
        )
        self.assertEqual(f.service.state.sessions["SES-V2"]["status"], SESSION_SUSPENDED)
        self.assertEqual(f.service.state.sessions["SES-V1"]["status"], SESSION_SCHEDULED)


class TriggerResponseTest(unittest.TestCase):
    def _one_finished_and_one_open(self) -> GovernanceFixture:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        f.grant_consent("LRN-01")
        f.grant_consent("LRN-02")
        f.schedule_session("SES-01")
        f.service.enroll_learner("SES-01", "LRN-01", ["CON-LRN-01"])
        f.finish_session_with_certificate("LRN-01", "SES-01", "CERT-01", enroll=False)
        f.schedule_session("SES-02", start_offset=14)
        f.service.enroll_learner("SES-02", "LRN-02", ["CON-LRN-02"])
        return f

    def test_consent_withdrawal_suspends_related_open_sessions_and_keeps_records(self) -> None:
        f = self._one_finished_and_one_open()
        events = f.service.withdraw_consent("CON-LRN-02", "学员不再授权练习数据使用")
        self.assertIn("ConsentWithdrawn", [e.type for e in events])
        self.assertEqual(f.service.state.sessions["SES-02"]["status"], SESSION_SUSPENDED)
        self.assertEqual(f.service.state.sessions["SES-02"]["suspension"]["reason"],
                         "consent_withdrawn")
        # SES-01 与 CERT-01 记录原样保留。
        self.assertEqual(f.service.state.sessions["SES-01"]["status"], SESSION_COMPLETED)
        self.assertIn("CERT-01", f.service.state.certificates)
        # 授权记录保留但状态变更。
        self.assertEqual(f.service.state.consents["CON-LRN-02"]["status"], "withdrawn")
        self.assertTrue(any(
            task["reason"] == "consent_withdrawn"
            for task in f.service.state.tasks.values()
        ))

    def test_organization_withdrawal_suspends_and_creates_remediation(self) -> None:
        f = self._one_finished_and_one_open()
        f.service.withdraw_organization("ORG-HKAI", "合作协议终止")
        self.assertEqual(f.service.state.orgs["ORG-HKAI"]["status"], "withdrawn")
        self.assertEqual(f.service.state.sessions["SES-02"]["status"], SESSION_SUSPENDED)
        self.assertEqual(f.service.state.sessions["SES-01"]["status"], SESSION_COMPLETED)
        task = next(
            task for task in f.service.state.tasks.values()
            if task["reason"] == "organization_withdrawn"
        )
        self.assertIn("CERT-01", task["related_certificate_ids"])
        # 退出后不能再安排活动。
        with self.assertRaisesRegex(GovernanceError, "已退出"):
            f.service.schedule_session(
                "SES-03", "CRS-01", 1, "INS-01",
                iso(f.current + timedelta(days=40)),
                iso(f.current + timedelta(days=40, hours=3)),
            )

    def test_nonpublic_material_quarantines_version_and_suspends_sessions(self) -> None:
        f = self._one_finished_and_one_open()
        events = f.service.report_nonpublic_material("MAT-01", "教材附录含内部通讯录")
        self.assertIn("MaterialQuarantined", [e.type for e in events])
        self.assertEqual(f.service.state.materials["MAT-01"]["status"], "quarantined")
        self.assertEqual(f.service.state.sessions["SES-02"]["status"], SESSION_SUSPENDED)
        # 已完成场次保留，进入补救任务；不能再用该版本排新场次。
        with self.assertRaisesRegex(GovernanceError, "已被隔离"):
            f.service.schedule_session(
                "SES-03", "CRS-01", 1, "INS-01",
                iso(f.current + timedelta(days=40)),
                iso(f.current + timedelta(days=40, hours=3)),
            )
        self.assertEqual(f.service.state.sessions["SES-01"]["status"], SESSION_COMPLETED)


class CertificateTraceTest(unittest.TestCase):
    def test_trace_identifies_version_consent_scope_review_and_instructor(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        f.grant_consent()
        f.schedule_session()
        f.service.enroll_learner("SES-01", "LRN-01", ["CON-LRN-01"])
        f.finish_session_with_certificate()

        trace = f.service.certificate_trace("CERT-01")
        snap = trace["snapshot"]
        self.assertEqual(snap["course_id"], "CRS-01")
        self.assertEqual(snap["version_no"], 1)
        self.assertEqual(snap["material_fingerprint"], "fp-v1-aaaa")
        self.assertEqual(snap["model_version"], "2025-09")
        self.assertEqual(snap["instructor_id"], "INS-01")
        self.assertTrue(snap["instructor_confirmed_event_seq"] > 0)
        self.assertEqual(snap["consents"][0]["consent_id"], "CON-LRN-01")
        self.assertEqual(snap["consents"][0]["scope_review_id"], "SCOPE-01")

    def test_only_confirmed_completed_session_can_be_certified(self) -> None:
        f = GovernanceFixture()
        f.bootstrap()
        f.publish_v1()
        f.grant_consent()
        f.schedule_session()
        f.service.enroll_learner("SES-01", "LRN-01", ["CON-LRN-01"])
        f.advance(days=7, hours=1)
        f.service.start_session("SES-01")
        # 正常流程下结课必带讲师确认；直接注入“完成但未确认”事件验证防线。
        f.advance(hours=3)
        rogue = f.service.store.append("SessionCompleted", {"session_id": "SES-01"})
        _apply(f.service.state, rogue)
        with self.assertRaisesRegex(GovernanceError, "讲师确认"):
            f.service.issue_certificate("CERT-X", "LRN-01", "SES-01")


class RestartTest(unittest.TestCase):
    def test_jsonl_store_replays_and_resumes_reminders_reviews_and_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            base_now = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)

            def clock_at(dt: datetime) -> CourseGovernanceService:
                return CourseGovernanceService(
                    JsonlEventStore(path), clock=lambda: dt
                )

            svc = clock_at(base_now)
            fx = GovernanceFixture.__new__(GovernanceFixture)
            fx.current = base_now
            fx.service = svc
            fx.bootstrap(qualification_days=60)
            fx.publish_v1()
            # 授权 20 天后到期，与 60 天后到期的资质一起制造临期/过期提醒。
            svc.grant_consent(
                "CON-SHORT", "LRN-09", "DS-01", "课堂练习",
                iso(base_now + timedelta(days=20)),
            )
            svc.request_review(
                "RV-01", "CRS-01", "suspension_followup",
                iso(base_now + timedelta(days=10)),
            )
            total_events = len(svc.store.load())

            # 重启：35 天后，两条提醒都应到期且待复核事项已过期。
            restart_now = base_now + timedelta(days=35)
            restarted = CourseGovernanceService(
                JsonlEventStore(path), clock=lambda: restart_now
            )
            summary = restarted.resume_after_restart()
            self.assertEqual(summary["replayed_events"], total_events)
            kinds = {r["kind"] for r in summary["due_reminders"]}
            self.assertIn("consent_expiry", kinds)
            self.assertIn("instructor_qualification_expiry", kinds)
            consent_reminder = next(
                r for r in summary["due_reminders"] if r["kind"] == "consent_expiry"
            )
            self.assertTrue(consent_reminder["overdue"])
            self.assertEqual(summary["open_reviews"][0]["review_id"], "RV-01")
            self.assertTrue(summary["open_reviews"][0]["overdue"])

            # 标记已通知后再重启，不重复提醒。
            restarted.mark_reminders_notified(
                [r["key"] for r in summary["due_reminders"]]
            )
            again = CourseGovernanceService(
                JsonlEventStore(path), clock=lambda: restart_now
            )
            self.assertEqual(again.due_reminders(), [])

    def test_event_log_with_gap_is_rejected_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.jsonl"
            path.write_text(
                '{"seq": 1, "id": "a", "type": "OrganizationRegistered",'
                ' "occurred_at": "2026-10-02T09:00:00+00:00",'
                ' "payload": {"org_id": "O", "name": "n"}}\n'
                '{"seq": 3, "id": "b", "type": "OrganizationWithdrawn",'
                ' "occurred_at": "2026-10-03T09:00:00+00:00",'
                ' "payload": {"org_id": "O", "reason": "x"}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GovernanceError, "序号不连续"):
                CourseGovernanceService(JsonlEventStore(path))


if __name__ == "__main__":
    unittest.main()
