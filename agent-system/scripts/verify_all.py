"""一键全量验证入口：重建规则库与图谱、自检本机模型、校验交付文档、跑三阶段全部用例。"""  # 模块说明：只用标准库，不依赖 pytest
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import subprocess  # 标准库：按固定顺序拉起构建脚本与总验证脚本
import sys  # 标准库：获取当前解释器路径并加入模块搜索路径
import time  # 标准库：记录总耗时
from pathlib import Path  # 标准库：定位项目根目录

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接执行脚本也能导入 app
from app.common import load_config  # 唯一配置入口：只用来确认配置可加载


def run_step(name: str, tolerant: bool = False) -> bool:  # 执行一个步骤
    """以子进程执行 scripts/ 下的脚本并打印单行摘要。
    入参：name 脚本文件名；tolerant 为真时非 0 退出码只提示、不判失败（仅用于本机 Ollama 自检）。
    出参：bool，该步骤是否算通过。
    失败时的行为：不抛异常，打印 stdout / stderr 尾部并返回 False。
    """
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / name)], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")  # 同步执行
    tail = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""  # 取输出末行
    print(f"[STEP] {name:22s} exit={proc.returncode} | {tail}")  # 打印步骤摘要
    if proc.returncode != 0 and not tolerant:  # 失败且不容错
        print(f"[FAIL] {name} 输出尾部：{(proc.stdout or '')[-500:]} {(proc.stderr or '')[-300:]}")  # 打印错误尾部
        return False  # 判定失败
    return True  # 成功或容错放行


def main() -> int:  # 一键验证主流程
    """按依赖顺序重建知识库与图谱、自检本机模型、校验三份交付文档并跑完三阶段全部用例。
    入参：无。
    出参：进程退出码，0 表示全部通过、1 表示有步骤失败。
    失败时的行为：配置不可读或任一步骤失败时打印原因并返回 1；本机 Ollama 未启动只提示、不影响通过，由阶段三自动断言降级路径。
    """
    started = time.time()  # 记录开始时间
    load_config()  # 先确认 config.yaml 可加载，失败直接抛 BizError 而非带着坏配置跑
    steps = [("build_kb.py", False), ("build_graph.py", False), ("check_ollama.py", True), ("verify_docs.py", False), ("verify_stage3.py", False)]  # 固定顺序：先建数据再验代码与文档
    results = [run_step(name, tolerant) for name, tolerant in steps]  # 逐项执行并收集结果
    passed = sum(1 for flag in results if flag)  # 统计通过步骤
    print(f"[RESULT] {passed}/{len(steps)} 步骤通过 | 耗时 {time.time() - started:.1f}s")  # 打印总结果
    if all(results):  # 全部通过
        print("[OK] 三阶段 + 文档校验全部通过：认证 / 权限 / 安全审查 / 人工确认 / 知识库 / 知识图谱 / 多跳关联 / 审计重建 / 本机模型 / 交付文档")  # 能力汇总
        return 0  # 成功
    print("[FAIL] 有步骤未通过，请按上方 [FAIL] 输出定位")  # 给出指引
    return 1  # 失败


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
