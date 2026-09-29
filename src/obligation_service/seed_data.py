"""演示/种子数据：校企合作协议三项义务场景。

- EQUIP-DELIVERY：一次性设备交付（问题场景：逾期后责任人离任并移交）；
- FACULTY-TRAIN：季度师资培训合格证明，前置依赖设备交付；
- ANNUAL-REPORT：年度合作报告。
"""
from __future__ import annotations

from typing import Any

SEED_AGREEMENT = {"code": "XY-2026-007", "title": "校企合作协议（实训基地）"}


def seed_plan_payload() -> dict[str, Any]:
    return {
        "verification": {
            "verifier_role": "核验人员",
            "allowed_verifier_parties": ["quality-office", "audit-office"],
        },
        "duties": [
            {
                "code": "EQUIP-DELIVERY",
                "title": "实训设备交付与安装验收",
                "description": "按清单交付实训设备并完成安装",
                "owner_party": "houqinchu",
                "owner_contact": "张主任（离任）",
                "channel": "calendar",
                "time_window": {
                    "first_due": "2026-08-31",
                    "recurrence": "once",
                    "remind_before_days": [7, 1, 0],
                    "grace_days": 0,
                },
                "evidence": {
                    "expected_types": ["设备签收单", "安装验收报告"],
                    "require_original": True,
                },
                "prerequisites": [],
            },
            {
                "code": "FACULTY-TRAIN",
                "title": "师资培训完成证明",
                "description": "每季度提交师资培训合格证明",
                "owner_party": "college",
                "owner_contact": "教学秘书",
                "channel": "mail",
                "time_window": {
                    "first_due": "2026-09-15",
                    "recurrence": "quarterly",
                    "remind_before_days": [7, 1],
                    "grace_days": 3,
                },
                "evidence": {"expected_types": ["培训签到表", "考核合格名单"]},
                "prerequisites": [{"duty_code": "EQUIP-DELIVERY", "same_window": False}],
            },
            {
                "code": "ANNUAL-REPORT",
                "title": "年度合作报告",
                "description": "每年度提交合作进展报告",
                "owner_party": "college",
                "owner_contact": "院办",
                "channel": "mail",
                "time_window": {
                    "first_due": "2026-10-31",
                    "recurrence": "yearly",
                    "remind_before_days": [14, 7],
                    "grace_days": 7,
                },
                "evidence": {"expected_types": ["年度报告正文", "经费决算表"]},
                "prerequisites": [],
            },
        ]
    }
