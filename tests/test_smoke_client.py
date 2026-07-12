"""MCP 客户端冒烟测试 - 模拟 workbuddy 走真实协议调用

运行前：必须先启动 app.main（默认 localhost:8000）
设置 MCP_TOKENS="test-token:tenant1" WECOM_USE_MOCK=true

注意：httpx 在 Windows 会读系统代理，localhost 请求需禁代理。
本脚本默认强制禁代理。
"""
import asyncio
import os
import sys

# 强制禁代理（否则 localhost 走系统代理会 502）
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER = "http://localhost:8000/mcp"
TOKEN = "test-token"
BAD_TOKEN = "wrong-token"


async def test_auth_rejected():
    """无 token / 错 token 应被拒"""
    try:
        async with streamablehttp_client(
            SERVER, headers={"Authorization": f"Bearer {BAD_TOKEN}"}
        ) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                await s.list_tools()
        print("[X] 错误 token 竟然通过了，鉴权失效！")
        return False
    except Exception as e:
        print(f"[OK] 错误 token 被拒: {type(e).__name__}: {str(e)[:80]}")
        return True


async def test_list_tools_and_call():
    """正确 token：列工具 + 调用 wecom_list_reports + get_detail"""
    async with streamablehttp_client(
        SERVER, headers={"Authorization": f"Bearer {TOKEN}"}
    ) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print(f"[OK] 列出工具({len(names)}): {names}")
            expected = {
                "wecom_list_reports",
                "wecom_get_report",
                "wecom_list_approvals",
                "wecom_get_approval_detail",
                "wecom_list_smart_table_records",
            }
            assert expected.issubset(set(names)), f"缺少工具: {expected - set(names)}"
            print("[OK] 5 个工具齐全")

            # 调汇报列表
            res = await s.call_tool(
                "wecom_list_reports",
                {"starttime": 1762502400, "endtime": 1762588800, "limit": 10},
            )
            print(f"[OK] wecom_list_reports 返回: {res.content[0].text[:120]}")

            # 调审批详情
            res = await s.call_tool(
                "wecom_get_approval_detail", {"sp_no": "mock-approval-001"}
            )
            print(f"[OK] wecom_get_approval_detail 返回: {res.content[0].text[:120]}")

            # 调智能表格
            res = await s.call_tool(
                "wecom_list_smart_table_records",
                {"docid": "d1", "sheet_id": "s1", "limit": 100},
            )
            print(f"[OK] wecom_list_smart_table_records 返回: {res.content[0].text[:120]}")
    return True


async def main():
    ok1 = await test_auth_rejected()
    ok2 = await test_list_tools_and_call()
    print("\n=== 结果 ===")
    print(f"鉴权拒绝: {'PASS' if ok1 else 'FAIL'}")
    print(f"工具调用: {'PASS' if ok2 else 'FAIL'}")
    sys.exit(0 if (ok1 and ok2) else 1)


asyncio.run(main())