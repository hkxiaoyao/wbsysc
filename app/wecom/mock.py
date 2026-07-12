"""
Mock 数据源 - PoC 阶段用，不接企微不接生产库
覆盖：汇报、审批、智能表格三类
"""
from __future__ import annotations

from typing import Any, Dict, List

# 脱敏 mock：汇报（journaluuid 列表 + 详情）
MOCK_REPORT_LIST: List[Dict[str, Any]] = [
    {"journaluuid": "mock-report-001", "create_time": 1762502400, "creator": "user001"},
    {"journaluuid": "mock-report-002", "create_time": 1762588800, "creator": "user002"},
]

MOCK_REPORT_DETAIL: Dict[str, Dict[str, Any]] = {
    "mock-report-001": {
        "journaluuid": "mock-report-001",
        "template_id": "TPL_DAILY",
        "creator": "user001",
        "create_time": 1762502400,
        "content": "【脱敏】今日完成需求评审，明日进入开发。",
    },
    "mock-report-002": {
        "journaluuid": "mock-report-002",
        "template_id": "TPL_DAILY",
        "creator": "user002",
        "create_time": 1762588800,
        "content": "【脱敏】今日处理客户工单 3 个，已全部关闭。",
    },
}

# 脱敏 mock：审批（sp_no 列表 + 详情）
MOCK_APPROVAL_LIST: List[Dict[str, Any]] = [
    {"sp_no": "mock-approval-001", "sp_status": 2, "apply_time": 1762502400},
    {"sp_no": "mock-approval-002", "sp_status": 1, "apply_time": 1762588800},
]

MOCK_APPROVAL_DETAIL: Dict[str, Dict[str, Any]] = {
    "mock-approval-001": {
        "sp_no": "mock-approval-001",
        "sp_name": "【脱敏】请假审批",
        "sp_status": 2,
        "template_id": "TPL_LEAVE",
        "apply_time": 1762502400,
        "applyer": {"userid": "user001"},
    },
    "mock-approval-002": {
        "sp_no": "mock-approval-002",
        "sp_name": "【脱敏】报销审批",
        "sp_status": 1,
        "template_id": "TPL_EXPENSE",
        "apply_time": 1762588800,
        "applyer": {"userid": "user002"},
    },
}

# 脱敏 mock：智能表格记录
MOCK_SMARTTABLE_RECORDS: List[Dict[str, Any]] = [
    {
        "record_id": "mock-rec-001",
        "create_time": 1762502400,
        "update_time": 1762588800,
        "values": {"任务名称": "【脱敏】需求调研", "状态": "已完成"},
    },
    {
        "record_id": "mock-rec-002",
        "create_time": 1762588800,
        "update_time": 1762588800,
        "values": {"任务名称": "【脱敏】接口联调", "状态": "进行中"},
    },
]