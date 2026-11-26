"""RAG 三段编排（白板③）：检索 → 增强 → 生成，串起知识库、知识图谱、LLM Wiki 与本机模型。"""  # 模块说明：不引入向量库或 embedding 服务，检索仍用标准库 IDF 余弦
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

from typing import Any  # 标准库：上下文与命中条目类型标注
from . import graph, knowledge, llm, wiki  # 检索依赖的规则库、图谱、模型与条目层
from .common import BizError  # 统一业务异常


def rag_conf(cfg: dict) -> dict:  # 读取 RAG 配置节
    """读取并校验 config.yaml 的 rag 节。
    入参：cfg 配置字典。
    出参：rag 配置子字典。
    失败时的行为：缺节抛 BizError(CONFIG_INVALID)，绝不在代码里补默认检索参数。
    """
    conf = cfg.get("rag")  # RAG 参数一律从配置读取
    if not isinstance(conf, dict):  # 配置节缺失或类型不符
        raise BizError("CONFIG_INVALID", "config.yaml 缺少 rag 配置节")  # 明确失败，不带病编排
    return conf  # 返回 RAG 配置


def retrieve(cfg: dict, question: str) -> list[dict]:  # 第一段：检索
    """RAG 检索段：用标准库相关度检索本地规则库，返回可核对证据。
    入参：cfg 配置字典；question 咨询问题文本。
    出参：按相关度降序的证据条目列表，每条含 short_id 与 score。
    失败时的行为：规则库未生成或结构损坏由 knowledge.search 抛对应 BizError；空问题返回空列表。
    """
    conf = rag_conf(cfg)  # 取 RAG 参数
    k = min(int(conf["top_k"]), int(cfg["knowledge"]["top_k"]))  # 与知识库上限取更小者，避免上下文超量
    return knowledge.search(cfg, question, k)  # 委托标准库检索，阈值由 knowledge.min_score 控制


def augment(cfg: dict, hits: list[dict], can_view: bool, wiki_summary: str = "") -> dict:  # 第二段：增强
    """RAG 增强段：把检索命中、图谱关联编号与 Wiki 摘要拼成固定格式的上下文。
    入参：cfg 配置字典；hits 检索命中；can_view 该角色能否查看证据原文；wiki_summary 已发布条目摘要，可为空。
    出参：含 context / related / refs / truncated 的字典，context 为可直接进提示词的文本块。
    失败时的行为：图谱缺失时自动降级为空关联链，不中断 RAG；无命中返回空上下文。
    """
    conf = rag_conf(cfg)  # 取 RAG 参数
    if not hits:  # 无检索命中
        return {"context": "", "related": [], "refs": [], "hops": [], "truncated": False}  # 空上下文，由上层走兜底提示
    items = hits[:int(conf["max_context_items"])]  # 只取前若干条证据，压住小模型编号漂移
    lines: list[str] = []  # 上下文行容器
    refs: list[str] = []  # 进入提示词的证据编号
    hop_lines: list[dict] = []  # 逐跳关联容器，回传给运行壳供审计与证据链断言使用
    for hit in items:  # 逐条组装证据行
        sid = str(hit.get("short_id", ""))  # 取证据短编号
        body = str(hit.get("text", "")) if can_view else str(hit.get("topic", ""))  # 按权限决定给原文还是仅标题
        lines.append(f"[{sid}] {body}")  # 固定格式：编号 + 正文或标题
        refs.append(sid)  # 记录进入提示词的编号，供审计核对
    chain = {"related": [], "hops": [], "truncated": False}  # 图谱关联链默认空
    try:  # 图谱快照缺失时仍要能生成提示词
        chain = graph.evidence_chain(cfg, refs, int(conf["evidence_depth"]), can_view)  # 沿知识关联扩展多跳证据链
    except BizError:  # 为什么这么卡：材料 f35 要求关键系统不可用时人工流程照常，图谱只读失败只丢关联编号、不阻断 RAG
        chain = {"related": [], "hops": [], "truncated": False}  # 降级为空关联，上层标记 graph_ready
    related = [str(x) for x in chain.get("related", [])[:int(conf["max_graph_refs"])]]  # 只取关联编号，截断到上限
    hop_lines = [dict(h) for h in chain.get("hops", [])[:int(conf["max_hops"])]]  # 逐跳关联按上限截断，回传给运行壳
    if related:  # 存在关联编号
        lines.append(f"关联事实编号（由知识图谱扩展）：{'、'.join(related)}")  # 只把编号交给模型，不塞关联节点正文
    if str(conf["wiki_summary_in_context"]).lower() == "true" and wiki_summary:  # 配置允许且条目摘要存在
        lines.append(f"知识条目摘要（LLM Wiki，已发布）：{wiki_summary}")  # 只放一条已发布条目摘要
    return {"context": "\n".join(lines), "related": related, "refs": refs, "hops": hop_lines, "truncated": bool(chain.get("truncated"))}  # 返回上下文、可核对编号与逐跳链


def generate(cfg: dict, system_prompt: str, business_id: str, question: str, materials: list[str], context: str) -> dict:  # 第三段：生成
    """RAG 生成段：把组装好的上下文交给本机模型，返回建议与降级状态。
    入参：cfg 配置字典；system_prompt 系统提示；business_id 业务编号；question 问题；materials 材料名列表；context 增强段上下文。
    出参：dict，含 text / degraded / reason / model，与 app.llm.generate 同构。
    失败时的行为：无上下文时直接返回降级结果不调模型；模型未就绪按 required 配置降级或抛 OLLAMA_UNAVAILABLE。
    """
    conf = rag_conf(cfg)  # 取 RAG 参数
    if not context.strip():  # 无依据上下文
        # 为什么这么卡：无命中时调模型只会凭空生成，RAG 要求每条结论指回编号，因此直接兜底不调模型
        return {"text": "", "degraded": True, "reason": "未检索到可核对依据，未调用模型", "model": str(cfg["ollama"]["model"])}  # 明确降级原因
    mats = "、".join(str(m) for m in (materials or [])) or "（无）"  # 规整材料名
    user_prompt = f"业务编号：{business_id}\n咨询问题：{question}\n已提交材料：{mats}\n可引用规则：\n{context}"  # 组装用户提示
    return llm.generate(cfg, system_prompt, user_prompt)  # 委托本机模型生成，地址与模型名全在配置


def pick_wiki_summary(cfg: dict, refs: list[str]) -> str:  # 选取一条已发布条目摘要
    """从 LLM Wiki 已发布条目中取一条与命中编号重合度最高的摘要。
    入参：cfg 配置字典；refs 命中证据编号列表。
    出参：摘要字符串；无已发布条目或配置关闭时返回空串。
    失败时的行为：Wiki 快照缺失或结构损坏时返回空串，不中断 RAG。
    """
    try:  # Wiki 快照未生成时静默降级
        return wiki.pick_published(cfg, refs)  # 委托条目层按重合度挑选
    except BizError:  # 条目层缺失或损坏
        return ""  # 返回空摘要，RAG 仍可用事实编号生成提示词


def build_context(cfg: dict, question: str, can_view: bool) -> dict:  # 一次串起检索与增强
    """组合 RAG 检索段与增强段，供运行壳在主链路中一次调用。
    入参：cfg 配置字典；question 咨询问题；can_view 该角色能否查看证据原文。
    出参：含 hits / context / related / refs / truncated / rule_version 的字典。
    失败时的行为：检索失败由 retrieve 抛出 BizError；图谱或 Wiki 缺失按降级处理，不影响返回结构。
    """
    hits = retrieve(cfg, question)  # 第一段：检索本地规则库
    summary = pick_wiki_summary(cfg, [str(h.get("short_id", "")) for h in hits])  # 取一条已发布条目摘要
    aug = augment(cfg, hits, can_view, summary)  # 第二段：增强为上下文
    return {**aug, "hits": hits, "wiki_summary": summary, "rule_version": _rule_version(cfg)}  # 返回完整上下文与版本


def _rule_version(cfg: dict) -> str:  # 读取规则库版本
    """读取当前规则库的资料版本，供上下文返回体带版本。
    入参：cfg 配置字典。
    出参：版本字符串；未建库返回 UNBUILT，损坏返回 UNKNOWN。
    失败时的行为：不抛异常，避免版本读取失败中断主链路。
    """
    import json  # 标准库：读取规则库版本，局部导入保持模块职责清晰
    from .common import resolve_path  # 路径解析，局部导入避免循环依赖
    path = resolve_path(cfg["knowledge"]["kb_path"])  # 规则库路径只从配置取
    if not path.exists():  # 尚未建库
        return "UNBUILT"  # 明确标记
    try:  # 捕获损坏
        return str(json.loads(path.read_text(encoding="utf-8")).get("rule_version", "UNKNOWN"))  # 取版本号
    except Exception:  # 文件损坏
        return "UNKNOWN"  # 降级标记
