"""从知识图谱构建 LLM Wiki 草稿：可直接执行的构建脚本。"""  # 模块说明：只生成 draft，发布必须经人工签核，脚本不提供一键发布
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import sys  # 标准库：把项目根加入模块搜索路径
from pathlib import Path  # 标准库：定位项目根目录

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接 python scripts/build_wiki.py 也能导入 app

from app import wiki  # LLM Wiki 条目层
from app.common import BizError, load_config  # 统一异常与配置加载


def main() -> int:  # 构建入口
    """从图谱为每个节点生成 Wiki 草稿并打印可核对摘要。
    入参：无。
    出参：进程退出码，0 成功、1 失败。
    失败时的行为：配置或图谱异常时捕获 BizError，打印错误码与说明并返回 1，不生成空条目库。
    """
    try:  # 捕获统一业务异常
        cfg = load_config()  # 读取 config.yaml，Wiki 路径与起草策略都从这里取
        payload = wiki.build_drafts(cfg)  # 从图谱节点建草稿并落盘
    except BizError as exc:  # 配置或图谱异常
        print(f"[FAIL] {exc.code}: {exc.msg}")  # 打印可操作错误信息
        return 1  # 非零退出码，便于脚本判定
    entries = payload.get("entries", {})  # 取条目表
    draft_n = sum(1 for e in entries.values() if str(e.get("status", "")) == "draft")  # 统计草稿数
    print(f"[OK] LLM Wiki 草稿已生成：{cfg['wiki']['path']}")  # 打印落盘路径
    print(f"[OK] 资料版本 {payload['wiki_version']} | 草稿 {draft_n} 条 | 状态均为 draft（未发布，不进 RAG 语料）")  # 打印可核对摘要
    print("[OK] 发布须经 POST /wiki/entry 挂确认单并由 L2 及以上签核；脚本不提供一键发布开关")  # 明确发布边界
    return 0  # 成功退出


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
