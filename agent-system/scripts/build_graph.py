"""从本地规则库构建知识图谱：可直接执行的构建脚本。"""  # 模块说明：唯一规则源仍是 config.yaml 登记的合成资料
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import sys  # 标准库：把项目根加入模块搜索路径
from pathlib import Path  # 标准库：定位项目根目录

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接 python scripts/build_graph.py 也能导入 app

from app import graph  # 知识图谱构建与查询能力
from app.common import BizError, load_config  # 统一异常与配置加载


def main() -> int:  # 构建入口
    """构建知识图谱并打印可核对的统计摘要。
    入参：无。
    出参：进程退出码，0 成功、1 失败。
    失败时的行为：配置、规则库或源资料出错时捕获 BizError，打印错误码与说明并返回 1，不生成空图谱。
    """
    try:  # 捕获统一业务异常
        cfg = load_config()  # 读取 config.yaml，图谱路径与阈值都从这里取
        payload = graph.build(cfg)  # 从 kb/rules.json 建节点与知识关联并落盘
    except BizError as exc:  # 配置、规则库或结构异常
        print(f"[FAIL] {exc.code}: {exc.msg}")  # 打印可操作的错误信息
        return 1  # 非零退出码，便于脚本判定
    stat = payload["stats"]  # 取图谱统计
    kinds = ", ".join(f"{k}={v}" for k, v in sorted(stat["relations"].items()))  # 拼出关系类型清单
    print(f"[OK] 知识图谱已生成：{cfg['graph']['path']}")  # 打印落盘路径
    print(f"[OK] 资料版本 {payload['rule_version']} | 节点 {stat['nodes']} 个 | 关联 {stat['edges']} 条 | 关系类型 {len(stat['relations'])} 类")  # 打印可核对摘要
    print(f"[OK] 关系清单：{kinds}")  # 打印逐类条数
    return 0  # 成功退出


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
