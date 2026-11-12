"""本机 Ollama 调用：只用标准库 urllib，不引入任何模型 SDK。"""  # 模块说明：地址与模型名全部来自 config.yaml
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import json  # 标准库：请求体与响应体序列化
import urllib.request  # 标准库：向本机 Ollama 发 HTTP 请求
from typing import Any  # 标准库：返回结果类型标注
from .common import BizError  # 统一业务异常


def generate(cfg: dict, system_prompt: str, user_prompt: str) -> dict:  # 本机模型生成
    """调用本机 Ollama 的 chat 接口生成提示文本。
    入参：cfg 配置字典（base_url / model / timeout_seconds / required / max_tokens / think 全从配置取）；system_prompt 系统提示；user_prompt 用户提示。
    出参：dict，含 text、degraded、reason、model 四个字段。
    失败时的行为：required=false 时服务不可达降级返回 degraded=True 且 text 为空；required=true 时抛 OLLAMA_UNAVAILABLE；响应结构异常抛 OLLAMA_BAD_RESPONSE；模型返回空正文同样按降级处理，不把空建议当推理成功。
    """
    conf = cfg["ollama"]  # 模型地址与名称只从配置取，代码里不写死任何地址
    url = str(conf["base_url"]).rstrip("/") + "/api/chat"  # 拼接本机 chat 接口，不访问任何云端地址
    options: dict[str, Any] = {"temperature": float(conf["temperature"]), "num_predict": int(conf["max_tokens"])}  # 低温度加生成上限，防止思考型小模型无限生成
    body: dict[str, Any] = {"model": conf["model"], "stream": False, "think": bool(conf["think"]), "options": options, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]}  # 组装请求体；think=false 让思考型模型把结果写进 content 而不是 thinking
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")  # 构造 POST 请求
    try:  # 捕获本机服务不可达、超时或非 2xx
        with urllib.request.urlopen(req, timeout=float(conf["timeout_seconds"])) as resp:  # 发起到本机 Ollama 的请求
            raw = resp.read().decode("utf-8")  # 读取并解码响应体
    except Exception as exc:  # 连接拒绝、超时、HTTP 错误等
        if bool(conf["required"]):  # 配置声明 Ollama 为强依赖
            raise BizError("OLLAMA_UNAVAILABLE", f"本机 Ollama 不可用：{exc}") from exc  # 直接失败，不静默降级
        # 为什么这么卡：材料 f35 要求关键系统不可用时沿用只读快照、人工流程照常，这里降级为仅检索提示而不中断链路
        return {"text": "", "degraded": True, "reason": f"Ollama 未就绪，已降级为仅检索提示：{type(exc).__name__}", "model": conf["model"]}  # 降级结果
    try:  # 解析响应结构
        data = json.loads(raw)  # 标准库解析 JSON
        text = str(data.get("message", {}).get("content", ""))  # 取 chat 接口的文本内容
    except Exception as exc:  # 结构异常
        raise BizError("OLLAMA_BAD_RESPONSE", f"Ollama 响应解析失败：{exc}") from exc  # 转成统一业务错误
    if not text.strip():  # 思考型模型只回 thinking 不回正文时，不能把空建议算作推理成功
        return {"text": "", "degraded": True, "reason": "模型返回空正文，已降级为仅检索提示", "model": conf["model"]}  # 明确降级，输出审查与人工确认链照常继续
    return {"text": text, "degraded": False, "reason": "", "model": conf["model"]}  # 正常生成结果
