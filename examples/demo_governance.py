"""端到端演示：一节 AI 通识课从发布、升级、暂停到签发学习证明的治理过程。

运行：

    python3 examples/demo_governance.py

脚本只使用标准库，会在 fixtures/ 下生成示例事件日志（不含真实身份信息）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.course_governance import (  # noqa: E402
    CourseGovernanceService,
    JsonlEventStore,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "governance-events.jsonl"


def main() -> None:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.unlink(missing_ok=True)

    current = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)

    def clock() -> datetime:
        return current

    svc = CourseGovernanceService(JsonlEventStore(FIXTURE_PATH), clock=clock)

    def iso(days: int = 0, hours: int = 0) -> str:
        return (current + timedelta(days=days, hours=hours)).isoformat()

    # 1. 合作机构、角色与讲师资质。
    svc.register_organization("ORG-DEMO", "示例社区培训机构")
    svc.register_privacy_officer("PO-DEMO", "示例隐私负责人")
    svc.register_operations_staff("OPS-DEMO", "示例运营主管")
    svc.register_instructor("INS-DEMO", "示例讲师", "ORG-DEMO")
    svc.verify_instructor_qualification(
        "INS-DEMO", "AI 通识讲师认证 L2", iso(), iso(365), "OPS-DEMO"
    )

    # 2. 练习数据来源：讲师提交范围，隐私负责人审核（含跨境批准）。
    svc.register_data_source("DS-DEMO", "匿名化课堂问答样本", True, "ORG-DEMO")
    svc.submit_data_scope(
        "SCOPE-DEMO", "DS-DEMO", "仅课堂演示，跨境处理仅限脱敏统计", "INS-DEMO"
    )
    # 若讲师本人尝试自审会被拒绝：approve_data_scope("SCOPE-DEMO", "INS-DEMO")
    svc.approve_data_scope("SCOPE-DEMO", "PO-DEMO", allows_cross_border=True)

    # 3. 发布课程 v1（模型 2025-09）。
    svc.register_course("CRS-DEMO", "让市民理解生成式 AI 的用途与边界", "ORG-DEMO")
    svc.submit_material(
        "MAT-DEMO-1", "CRS-DEMO", "AI 通识教材 v1", "sha256:demo-fp-v1",
        ["DS-DEMO"], "INS-DEMO",
    )
    svc.publish_course_version(
        "CRS-DEMO", "MAT-DEMO-1",
        model_provider="示例模型合作方",
        model_id="general-chat",
        model_version="2025-09",
        model_purpose="课堂演示问答",
        published_by="OPS-DEMO",
    )

    # 4. 排课、授权、报名、结课、签发证明。
    svc.schedule_session(
        "SES-A", "CRS-DEMO", 1, "INS-DEMO", iso(7), iso(7, 3)
    )
    svc.grant_consent(
        "CON-DEMO", "LRN-DEMO", "DS-DEMO", "课堂练习用途", iso(60)
    )
    svc.enroll_learner("SES-A", "LRN-DEMO", ["CON-DEMO"])

    current += timedelta(days=7, hours=3)
    svc.start_session("SES-A")
    current += timedelta(hours=3)
    svc.confirm_and_complete_session("SES-A", "INS-DEMO")
    svc.issue_certificate("CERT-A", "LRN-DEMO", "SES-A")

    # 5. 模型升级发布 v2；旧证明不受影响。
    svc.submit_material(
        "MAT-DEMO-2", "CRS-DEMO", "AI 通识教材 v2", "sha256:demo-fp-v2",
        ["DS-DEMO"], "INS-DEMO",
    )
    svc.publish_course_version(
        "CRS-DEMO", "MAT-DEMO-2",
        model_provider="示例模型合作方",
        model_id="general-chat",
        model_version="2026-06",
        model_purpose="课堂演示问答",
        published_by="OPS-DEMO",
    )
    svc.schedule_session(
        "SES-B", "CRS-DEMO", 2, "INS-DEMO", iso(14), iso(14, 3)
    )

    # 6. v2 出现新风险：只暂停尚未开始的 SES-B，SES-A 与 CERT-A 保持不变。
    svc.register_risk(
        "RISK-DEMO", "新模型版本在特定提问下出现误导性回答",
        scope="not_started", course_id="CRS-DEMO", version_no=2,
    )

    # 7. 重启后取回待办。
    current += timedelta(days=35)
    resumed = CourseGovernanceService(JsonlEventStore(FIXTURE_PATH), clock=clock)
    summary = resumed.resume_after_restart()

    trace = resumed.certificate_trace("CERT-A")
    print(f"事件已写入：{FIXTURE_PATH.relative_to(Path.cwd())}")
    print(f"重放事件数：{summary['replayed_events']}")
    print(f"证明 CERT-A 指向版本：v{trace['snapshot']['version_no']} "
          f"模型 {trace['snapshot']['model_version']}")
    print(f"证明中的授权：{[c['consent_id'] for c in trace['snapshot']['consents']]}")
    print(f"讲师确认事件序号：{trace['snapshot']['instructor_confirmed_event_seq']}")
    print(f"SES-B 状态：{resumed.state.sessions['SES-B']['status']}")
    print(f"重启后待办提醒：{len(summary['due_reminders'])} 条，"
          f"补救任务：{len(summary['open_tasks'])} 个")


if __name__ == "__main__":
    main()
