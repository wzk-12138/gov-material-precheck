"""本机 Ollama 自检：探测服务与模型是否就绪，只给可操作命令，不安装任何软件。"""  # 模块说明：只用标准库，地址与模型名全部来自 config.yaml
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import json  # 标准库：解析本机模型列表响应
import sys  # 标准库：回传退出码并加入模块搜索路径
import urllib.request  # 标准库：请求本机 Ollama 的模型列表接口
from pathlib import Path  # 标准库：定位项目根目录

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接执行脚本也能导入 app
from app.common import load_config  # 唯一配置入口：地址与模型名都不写死在代码里


def probe(cfg: dict) -> dict:  # 探测本机 Ollama 状态
    """请求本机模型列表并判断配置指定的模型是否已拉取。
    入参：cfg 配置字典，取 ollama.base_url / model / timeout_seconds。
    出参：dict，含 online、model、models、matched、reason 五个字段。
    失败时的行为：不抛异常；连接拒绝、超时、响应损坏都记为 online=False 并写明原因，供调用方决定是否降级。
    """
    conf = cfg["ollama"]  # 模型配置节
    url = str(conf["base_url"]).rstrip("/") + "/api/tags"  # 拼接本机模型列表接口，不访问任何云端地址
    wanted = str(conf["model"])  # 配置指定的模型名
    base = {"online": False, "model": wanted, "models": [], "matched": False, "reason": ""}  # 默认离线结果
    try:  # 捕获服务未启动、超时与非 2xx
        with urllib.request.urlopen(url, timeout=float(conf.get("probe_timeout_seconds", conf["timeout_seconds"]))) as resp:  # 用独立探测超时，服务离线时快速返回而不拖慢验证
            raw = resp.read().decode("utf-8")  # 读取响应体
    except Exception as exc:  # 连接拒绝或超时
        base["reason"] = f"本机 Ollama 未就绪：{type(exc).__name__}"  # 只记录异常类型，不回显地址细节
        return base  # 返回离线结果
    try:  # 捕获响应结构异常
        data = json.loads(raw)  # 解析模型列表
        names = [str(x.get("name", "")) for x in data.get("models", [])]  # 取全部已拉取模型名
    except Exception as exc:  # 不是合法 JSON 或结构不同
        base["reason"] = f"模型列表解析失败：{type(exc).__name__}"  # 记录原因
        return base  # 返回离线结果
    base["online"] = True  # 服务在线
    base["models"] = names  # 已拉取模型清单
    base["matched"] = any(n == wanted or n.split(":")[0] == wanted.split(":")[0] for n in names)  # 名称精确匹配或主干一致
    base["reason"] = "服务在线，模型已就绪" if base["matched"] else "服务在线，但配置模型未拉取"  # 给出可核对结论
    return base  # 返回完整状态


def main() -> int:  # 自检主流程
    """打印本机 Ollama 状态与下一步命令，并回传可直接用于脚本判断的退出码。
    入参：无，配置从 config.yaml 读取。
    出参：进程退出码，0 在线且模型就绪、1 服务未启动或响应异常、2 在线但模型未拉取。
    失败时的行为：配置缺失由 load_config 抛 BizError 并以非零码退出；绝不尝试自动下载模型或改动配置。
    """
    cfg = load_config()  # 读唯一配置
    state = probe(cfg)  # 探测服务与模型
    print(f"[OLLAMA] online={state['online']} model={state['model']} matched={state['matched']}")  # 打印状态
    print(f"[OLLAMA] {state['reason']}")  # 打印原因
    if state["online"] and state["models"]:  # 服务在线才列已拉取模型
        print("[OLLAMA] 本机已拉取：" + "、".join(state["models"][:8]))  # 最多列八个，避免刷屏
    if not state["online"]:  # 服务未启动或响应异常
        print("[NEXT] 启动本机 Ollama 服务后重跑本脚本；服务与模型权重由使用者本机安装，本项目不自动下载")  # 明确边界
        print("[NEXT] 未启动期间系统按 config.yaml 的 required=false 降级为仅检索提示，人工确认链路照常")  # 说明降级行为
        return 1  # 未就绪
    if not state["matched"]:  # 在线但缺模型
        print(f"[NEXT] 拉取模型：ollama pull {state['model']}；或把 config.yaml 的 ollama.model 改成本机已有模型")  # 可操作命令
        return 2  # 缺模型
    print("[NEXT] 可执行 scripts/verify.py 与 scripts/verify_all.py 验证完整推理链路")  # 就绪后的下一步
    return 0  # 全部就绪


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
