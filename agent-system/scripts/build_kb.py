"""从合成训练资料构建本地规则库：可直接执行的构建脚本。"""  # 模块说明：唯一规则源是 config.yaml 登记的本地文件
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import sys  # 标准库：把项目根加入模块搜索路径
from pathlib import Path  # 标准库：定位项目根目录
from typing import Any  # 标准库：规则库类型标注

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接 python scripts/build_kb.py 也能导入 app

from app import knowledge  # 规则库构建能力
from app.common import BizError, load_config  # 统一异常与配置加载


def main() -> int:  # 构建入口
    """构建规则库并打印摘要。
    入参：无。
    出参：进程退出码，0 成功、1 失败。
    失败时的行为：配置或源资料出错时捕获 BizError，打印错误码与说明并返回 1，不生成空规则库。
    """
    try:  # 捕获统一业务异常
        cfg = load_config()  # 读取 config.yaml，规则源与输出路径都从这里取
        payload: dict[str, Any] = knowledge.build_rules(cfg)  # 从合成资料抽取事实并落盘
    except BizError as exc:  # 配置、源文件或结构异常
        print(f"[FAIL] {exc.code}: {exc.msg}")  # 打印可操作的错误信息
        return 1  # 非零退出码，便于脚本判定
    print(f"[OK] 规则库已生成：{cfg['knowledge']['kb_path']}")  # 打印落盘路径
    print(f"[OK] 资料版本 {payload['rule_version']} | 条目 {payload['count']} 条 | 红线跳过 {payload['skipped']} 条")  # 打印可核对摘要
    return 0  # 成功退出


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
