"""红测乙（页面）：库里已有新尝试时，流水页不能继续渲染首轮旧成败。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE_PATHS = [
    ROOT / "backend" / "app" / "static" / "投递流水.html",
    ROOT / "static" / "投递流水.html",
]


def _script(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.S)
    return "\n".join(blocks)


def test_流水页每次刷新都渲染本轮接口响应():
    for path in PAGE_PATHS:
        assert path.is_file(), path
        script = _script(path)

        # 缺陷本体：模块级旧态只在第一次赋值，之后刷新拿到新 rows 也会被丢弃。
        assert "_流水旧态" not in script, f"{path} 仍保留冻结旧票"
        assert "let _旧" not in script

        # 刷新函数必须直接遍历本次 await 得到的 rows（或等价的本轮 show=rows）。
        assert "await 投递台.getJSON('/api/投递')" in script
        uses_fresh_response = (
            re.search(r"for\s*\(\s*const\s+\w+\s+of\s+rows\s*\)", script) is not None
            or re.search(r"const\s+show\s*=\s*rows\b", script) is not None
        )
        assert uses_fresh_response, f"{path} 未把本轮 /api/投递 响应交给渲染循环"

        # 状态列必须继续由真实行数据生成，不能只改静态标题或芯片。
        assert "投递台.徽章(r.状态)" in script
        assert "r.尝试号" in script


def test_尝试详情与队列卡片仍直接展示库内状态():
    detail = (ROOT / "backend" / "app" / "static" / "事件详情.html").read_text(encoding="utf-8")
    queue = (ROOT / "backend" / "app" / "static" / "事件队列.html").read_text(encoding="utf-8")

    assert "/api/事件/" in detail
    assert "await 投递台.getJSON('/api/事件/'" in detail
    assert "JSON.stringify(data" in detail
    assert "投递台.徽章(r.状态)" in detail
    assert "投递台.徽章(r.状态)" in queue
    assert "_流水旧态" not in detail
    assert "_流水旧态" not in queue
