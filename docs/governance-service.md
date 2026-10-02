# 课程治理服务

`src/governance/` 实现普惠人工智能培训的课程治理服务，供运营团队保存和追踪课程、授权、场次与证明。

## 保存的内容

- 课程目标与课程版本（教材版本、模型与用途、练习数据来源、所需授权范围）
- 讲师与合作机构资质
- 学员隐私授权（可撤销、可设到期时间）
- 活动场次与报名
- 学习证明（证书，含内容摘要）
- 审核任务、例外审批、补救任务、提醒与审计记录

## 关键规则

1. **教材先审后发布**：讲师提交教材版本，隐私审核人员批准后才能用于发布课程版本。
2. **利益冲突回避**：提交者不得审批自己的教材或例外；与提交者同属一个机构的审核人员同样不得审批。
3. **证书不可改写**：证书记录签发时的课程版本、教材版本、模型、授权与确认讲师，并带内容摘要；课程升级模型只会产生新的课程版本，已完成场次与证书保持原版本不变。
4. **升级只影响未开始或明确受影响的场次**：发布新版本时，尚未开始的场次自动切换到新版本；授权范围不足的报名暂停并生成补救任务；进行中的场次保持原版本，除非运营明确暂停。
5. **事件处置**：学员取消授权、合作机构退出、教材被标记含有不应公开的资料时，相关未完成的场次与报名暂停，已发生记录保留，并生成补救任务。
6. **跨境数据**：使用跨境数据来源发布版本时，必须引用已批准的跨境例外。
7. **重启接续**：状态持久化在 JSON 存储中（原子写入）；服务重启后调用 `recover()` 会撤销到期授权、补发过期提醒，并汇总待复核与未办结的补救事项。

## 主要接口

```python
from pathlib import Path
from src.governance import GovernanceService, JsonStore

service = GovernanceService(JsonStore(Path("state.json")))

service.register_institution("org-1", "示例机构")
service.register_instructor("instructor-1", "讲师甲", "org-1")
service.register_privacy_officer("officer-1", "审核员甲")
service.register_data_source("ds-1", "练习数据集", "公开资料", "training-data")
service.create_course("course-1", "人工智能入门", ["理解基本概念"])

material = service.submit_material_version("course-1", "instructor-1", "materials/v1.pdf")
(task,) = service.pending_reviews()
service.review_material(task.task_id, "officer-1", approve=True)
version = service.publish_course_version(
    "course-1", material.material_version_id,
    model_id="model-1", model_purpose="课堂演示",
    data_source_ids=["ds-1"], required_consent_scope=["training-data"],
)

session = service.schedule_session("course-1", "instructor-1", "2026-04-01T09:00:00+08:00")
service.grant_consent("consent-1", "learner-1", ["training-data"])
enrollment = service.enroll(session.session_id, "learner-1", "consent-1")
service.start_session(session.session_id)
service.complete_session(session.session_id)
certificate = service.issue_certificate(enrollment.enrollment_id, "instructor-1")

# 管理人员追溯：证书使用了哪一版课程、哪项授权、哪位讲师确认
lineage = service.certificate_lineage(certificate.certificate_id)

# 服务重启后接续过期提醒与待复核事项
report = GovernanceService(JsonStore(Path("state.json"))).recover()
```

行为示例与边界场景见 `tests/test_governance_service.py`。
