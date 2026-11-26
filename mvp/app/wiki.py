"""LLM Wiki（白板③）：把知识图谱渲染成可读条目，模型起草、人工确认后才发布进 RAG 语料。"""  # 模块说明：纯本地 JSON 存储，起草不等于发布，不引入维基引擎或站点服务
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import json  # 标准库：读取规则库正文与 Wiki 快照
from typing import Any  # 标准库：条目结构类型标注
from . import graph, llm  # 知识图谱提供节点与关联，本机模型负责起草摘要
from .common import BizError, read_json, resolve_path, write_json  # 统一异常与本地存储


def wiki_conf(cfg: dict) -> dict:  # 读取 Wiki 配置节
    """读取并校验 config.yaml 的 wiki 节。
    入参：cfg 配置字典。
    出参：wiki 配置子字典。
    失败时的行为：缺节抛 BizError(CONFIG_INVALID)，不在代码里补默认路径或阈值。
    """
    conf = cfg.get("wiki")  # Wiki 参数一律从配置读取
    if not isinstance(conf, dict):  # 配置节缺失或类型不符
        raise BizError("CONFIG_INVALID", "config.yaml 缺少 wiki 配置节")  # 明确失败
    return conf  # 返回 Wiki 配置


def _wiki_path(cfg: dict):  # 解析 Wiki 快照路径
    """把配置里的 Wiki 路径解析为绝对路径。
    入参：cfg 配置字典。
    出参：Path 绝对路径对象。
    失败时的行为：无失败分支，路径只从配置取。
    """
    return resolve_path(str(wiki_conf(cfg)["path"]))  # 路径只从配置取


def load_entries(cfg: dict) -> dict:  # 读取 Wiki 快照
    """读取本地 Wiki 条目快照。
    入参：cfg 配置字典。
    出参：含 wiki_version / entries 的快照字典；文件不存在时返回空结构。
    失败时的行为：文件损坏抛 BizError(STORE_READ_FAILED)。
    """
    return read_json(_wiki_path(cfg), {"wiki_version": "", "entries": {}})  # 文件不存在返回空库，不创建隐式状态


def build_drafts(cfg: dict) -> dict:  # 从图谱节点起草全部条目
    """为每个图谱节点生成一条 Wiki 草稿，状态恒为 draft，不进 RAG 语料。
    入参：cfg 配置字典；节点正文与 1 跳关联来自知识图谱，摘要由本机模型起草、失败则回退为正文截断。
    出参：含 wiki_version / entries / count 的快照字典，同时原子写入配置路径。
    失败时的行为：图谱缺失抛 GRAPH_MISSING；模型未就绪时按正文截断兜底，保证草稿仍可生成。
    """
    conf = wiki_conf(cfg)  # 取 Wiki 参数
    g = graph.load(cfg)  # 读取图谱快照，缺图直接失败
    entries: dict[str, Any] = {}  # 条目容器，键为节点编号
    for node in g.get("nodes", []):  # 逐节点起草条目
        nid = str(node.get("id", ""))  # 节点编号
        if not nid:  # 无编号节点跳过
            continue  # 保证条目都可核对
        meta = dict(node.get("meta", {}))  # 复制节点属性，避免改动图谱对象
        text = str(meta.get("text") or node.get("label") or "")  # 正文优先取事实 text，否则用标签
        summary = _draft_summary(cfg, nid, text, node)  # 起草摘要，模型失败时回退截断
        refs = _one_hop_refs(g, nid)  # 取 1 跳关联编号，作为条目引用
        entries[nid] = {"id": nid, "type": str(node.get("type", "")), "title": str(node.get("label", nid)), "text": summary, "refs": refs, "status": "draft", "drafter": "model" if not summary.startswith("[回退]") else "fallback", "signer": "", "version": str(g.get("rule_version", ""))}  # 组装草稿条目
    payload = {"wiki_version": str(g.get("rule_version", "")), "count": len(entries), "entries": entries}  # 组装快照
    write_json(_wiki_path(cfg), payload)  # 原子落盘
    return payload  # 返回草稿快照


def _draft_summary(cfg: dict, nid: str, text: str, node: dict) -> str:  # 起草单条摘要
    """调用本机模型依据节点正文起草一条不超过上限的摘要。
    入参：cfg 配置字典；nid 节点编号；text 节点正文；node 完整节点。
    出参：摘要字符串；模型未就绪或返回空时回退为正文截断并加 [回退] 前缀。
    失败时的行为：捕获模型侧 BizError 后回退，不让单条起草失败中断全库构建。
    """
    conf = wiki_conf(cfg)  # 取 Wiki 参数
    limit = int(conf["max_summary_chars"])  # 摘要长度上限来自配置
    if not bool(conf.get("draft_with_model", False)):  # 配置关闭模型起草时，直接用正文截断，避免批量建库拖慢验证
        return ("[回退] " + text)[:limit]  # 回退摘要，明确标记来源
    system = "你是资料整理助手，只依据给定事实写一条不超过字数限制的中文摘要，不得引入事实外信息，不得输出结论性表述。"  # 起草专用系统提示，区别于业务预检提示词
    user = f"事实编号：{nid}\n正文：{text[:300]}\n请写一条不超过 {limit} 字的中文摘要。"  # 组装起草请求，正文截断防止长文本拖慢 CPU 推理
    try:  # 模型不可用时回退，不中断全库
        gen = llm.generate(cfg, system, user)  # 复用本机模型调用，地址与模型名全在配置
        if not gen.get("degraded") and str(gen.get("text", "")).strip():  # 真实生成且非空
            return str(gen["text"]).strip()[:limit]  # 截断到上限后返回
    except BizError:  # 模型强依赖或响应异常
        pass  # 回退到正文截断
    return ("[回退] " + text)[:limit]  # 模型未就绪时用正文截断兜底，明确标记回退来源


def _one_hop_refs(g: dict, nid: str) -> list[str]:  # 取节点的一跳关联编号
    """从图谱中取某节点的一跳邻居编号，作为 Wiki 条目的可核对引用。
    入参：g 图谱字典；nid 节点编号。
    出参：关联编号列表，去重且最多 8 个。
    失败时的行为：无失败分支；孤立节点返回空列表。
    """
    refs: list[str] = []  # 引用容器
    seen: set[str] = {nid}  # 去重集合，排除自身
    for edge in g.get("edges", []):  # 遍历全部关联
        src, dst = str(edge.get("src", "")), str(edge.get("dst", ""))  # 取两端编号
        if nid == src and dst not in seen:  # 出向邻居
            seen.add(dst)  # 标记已收录
            refs.append(dst)  # 收入引用
        elif nid == dst and src not in seen:  # 入向邻居
            seen.add(src)  # 标记已收录
            refs.append(src)  # 收入引用
        if len(refs) >= 8:  # 达到上限
            break  # 提前结束
    return refs  # 返回一跳引用


def list_entries(cfg: dict, role: str | None, admin_roles: list[str]) -> dict:  # 按可见性列条目
    """列出 Wiki 条目，业务岗只见 published，管理员可见 draft。
    入参：cfg 配置字典；role 请求角色；admin_roles 管理员角色清单。
    出参：含 items / total / visible_draft 的字典。
    失败时的行为：快照缺失返回空列表；角色未登记时不额外放行草稿。
    """
    conf = wiki_conf(cfg)  # 取 Wiki 参数
    snap = load_entries(cfg)  # 读取快照
    can_see_draft = str(role) in [str(x) for x in admin_roles]  # 仅管理员可见草稿
    allowed_draft = [str(x) for x in conf.get("draft_visible_roles", [])]  # 配置里的草稿可见角色
    can_see_draft = can_see_draft and str(role) in allowed_draft  # 双重判定：管理员且在配置白名单
    items: list[dict] = []  # 结果容器
    for entry in snap.get("entries", {}).values():  # 逐条筛选
        status = str(entry.get("status", ""))  # 取条目状态
        if status == "published" or (status == "draft" and can_see_draft):  # 已发布或管理员看草稿
            items.append(_public_entry(entry))  # 裁剪后收入结果
    items.sort(key=lambda e: str(e.get("id", "")))  # 按编号排序，便于人工核对
    return {"items": items, "total": len(items), "visible_draft": can_see_draft}  # 返回可见条目


def get_entry(cfg: dict, node_id: str, role: str | None, admin_roles: list[str]) -> dict:  # 取单条条目
    """按节点编号取一条 Wiki 条目，并做可见性校验。
    入参：cfg 配置字典；node_id 节点编号；role 请求角色；admin_roles 管理员角色清单。
    出参：裁剪后的条目字典。
    失败时的行为：编号不存在抛 WIKI_ENTRY_NOT_FOUND；业务岗查询 draft 抛 WIKI_DRAFT_NOT_PUBLISHED。
    """
    conf = wiki_conf(cfg)  # 取 Wiki 参数
    snap = load_entries(cfg)  # 读取快照
    entry = snap.get("entries", {}).get(str(node_id))  # 按编号取条目
    if entry is None:  # 编号不存在
        raise BizError("WIKI_ENTRY_NOT_FOUND", f"Wiki 条目不存在：{node_id}")  # 拒绝凭空查询
    status = str(entry.get("status", ""))  # 取状态
    admin_ok = str(role) in [str(x) for x in admin_roles] and str(role) in [str(x) for x in conf.get("draft_visible_roles", [])]  # 是否管理员且配置允许
    if status == "draft" and not admin_ok:  # 草稿对非管理员不可见
        # 为什么这么卡：draft 是模型未签核的自写摘要，业务岗若能看到会误当成已核实口径，因此发布前只开放给管理员
        raise BizError("WIKI_DRAFT_NOT_PUBLISHED", "该条目尚未发布，业务岗不可见")  # 明确拒绝越权读取草稿
    return _public_entry(entry)  # 返回裁剪后的条目


def submit_entry(cfg: dict, node_id: str, text: str, actor: str) -> dict:  # 提交起草或修订
    """提交一条 Wiki 条目的起草或修订，落 draft 并交由上层生成待确认单。
    入参：cfg 配置字典；node_id 节点编号；text 条目摘要；actor 提交人标识。
    出参：含 node_id / status / text / drafter 的字典，状态恒为 draft。
    失败时的行为：编号不在图谱抛 GRAPH_NODE_NOT_FOUND；摘要超长抛 WIKI_SUMMARY_TOO_LONG；快照损坏抛 STORE_READ_FAILED。
    """
    conf = wiki_conf(cfg)  # 取 Wiki 参数
    limit = int(conf["max_summary_chars"])  # 摘要长度上限
    body = str(text).strip()  # 规整摘要文本
    if not body:  # 空摘要
        raise BizError("WIKI_SUMMARY_EMPTY", "条目摘要不能为空")  # 拒绝空条目
    if len(body) > limit:  # 超长
        raise BizError("WIKI_SUMMARY_TOO_LONG", f"条目摘要超过 {limit} 字上限")  # 拒绝超长摘要
    g = graph.load(cfg)  # 读取图谱，确认节点存在
    table = {str(n.get("id", "")): n for n in g.get("nodes", [])}  # 建节点索引
    if str(node_id) not in table:  # 节点不存在
        raise BizError("GRAPH_NODE_NOT_FOUND", f"知识节点不存在：{node_id}")  # 拒绝悬空条目
    snap = load_entries(cfg)  # 读取 Wiki 快照
    entries = snap.get("entries", {})  # 取条目表
    old = entries.get(str(node_id), {})  # 取旧条目，修订时保留引用
    entries[str(node_id)] = {"id": str(node_id), "type": str(table[node_id].get("type", "")), "title": str(table[node_id].get("label", node_id)), "text": body, "refs": list(old.get("refs", [])), "status": "draft", "drafter": actor, "signer": "", "version": str(g.get("rule_version", ""))}  # 写入草稿，发布权交给确认门
    snap["entries"] = entries  # 写回快照
    snap["wiki_version"] = str(g.get("rule_version", ""))  # 同步版本
    write_json(_wiki_path(cfg), snap)  # 原子落盘，写入后仍是 draft
    return {"node_id": str(node_id), "status": "draft", "text": body, "drafter": actor}  # 返回草稿状态，供上层挂确认单


def publish_entry(cfg: dict, node_id: str, signer: str, tier: str, ticket_id: str) -> dict:  # 人工确认后发布
    """在已确认单据前提下把草稿转为 published，使其进入 RAG 语料。
    入参：cfg 配置字典；node_id 节点编号；signer 签核人；tier 签核人分层；ticket_id 已确认单据编号。
    出参：含 node_id / status / signer / version 的字典。
    失败时的行为：条目不存在抛 WIKI_ENTRY_NOT_FOUND；分层不足抛 WIKI_SIGNER_TIER_LOW；单据未确认或签核人不符抛对应错误。
    """
    from . import permissions  # 局部导入：分层判定，避免模块循环依赖
    conf = wiki_conf(cfg)  # 取 Wiki 参数
    need = str(conf["publish_min_tier"])  # 发布所需最低分层
    if permissions.tier_rank(cfg, tier) < permissions.tier_rank(cfg, need):  # 签核人分层不足
        # 为什么这么卡：白板④要求用户分层决定能否签核；Wiki 条目会进 RAG 语料，低层级发布等于让未授权岗位改规则口径
        raise BizError("WIKI_SIGNER_TIER_LOW", f"发布条目需 {need} 及以上分层，当前为 {tier}")  # 拒绝低层级签核
    snap = load_entries(cfg)  # 读取 Wiki 快照
    entry = snap.get("entries", {}).get(str(node_id))  # 按编号取条目
    if entry is None:  # 条目不存在
        raise BizError("WIKI_ENTRY_NOT_FOUND", f"Wiki 条目不存在：{node_id}")  # 拒绝凭空发布
    tickets = read_json(resolve_path(cfg["confirmation"]["store_path"]), {"tickets": {}}).get("tickets", {})  # 读取待确认单
    ticket = tickets.get(str(ticket_id))  # 取本次发布的确认单
    # 为什么这么卡：条目发布会改变后续所有预检的检索语料，必须由接手岗位本人凭已确认单据签核，不能自写自发布
    if not ticket or str(ticket.get("status", "")) != "confirmed":  # 单据未确认
        raise BizError("WIKI_CONFIRM_REQUIRED", "须提供已人工确认的待确认单编号")  # 未确认一律拒绝
    if str(ticket.get("confirmed_by", "")) != str(signer):  # 签核人不是单据确认人
        raise BizError("WIKI_SIGNER_MISMATCH", "签核人必须是该单据的确认人，不得借用账号")  # 防止冒用签核
    entry["status"] = "published"  # 转已发布
    entry["signer"] = str(signer)  # 记录签核人
    write_json(_wiki_path(cfg), snap)  # 原子落盘，发布后即可被 RAG 检索命中
    return {"node_id": str(node_id), "status": "published", "signer": str(signer), "version": str(entry.get("version", ""))}  # 返回发布结果


def pick_published(cfg: dict, refs: list[str]) -> str:  # 供 RAG 挑选一条已发布摘要
    """从已发布条目中取一条与命中编号重合度最高的摘要。
    入参：cfg 配置字典；refs 命中证据编号列表。
    出参：摘要字符串；无已发布条目返回空串。
    失败时的行为：快照缺失返回空串，不中断 RAG。
    """
    snap = load_entries(cfg)  # 读取快照
    published = [e for e in snap.get("entries", {}).values() if str(e.get("status", "")) == "published"]  # 只取已发布条目
    if not published:  # 无已发布条目
        return ""  # 返回空摘要
    refset = set(str(x) for x in refs)  # 命中编号集合
    def overlap(entry: dict) -> int:  # 计算条目引用与命中编号的重合数
        """计算一个条目与命中编号的重合数量。
        入参：entry Wiki 条目字典。
        出参：重合编号个数，int。
        失败时的行为：无失败分支，refs 缺失按空列表处理。
        """
        return len(refset & set(str(x) for x in entry.get("refs", [])))  # 交集大小即重合度
    best = max(published, key=overlap)  # 取重合度最高的一条
    return str(best.get("text", ""))  # 返回摘要文本


def _public_entry(entry: dict) -> dict:  # 裁剪条目对外字段
    """把 Wiki 条目裁成可对外返回的形态。
    入参：entry Wiki 条目字典。
    出参：含 id / title / text / refs / status / version / signer 的字典。
    失败时的行为：无失败分支，缺字段用空值兜底。
    """
    return {"id": str(entry.get("id", "")), "title": str(entry.get("title", "")), "text": str(entry.get("text", "")), "refs": [str(x) for x in entry.get("refs", [])], "status": str(entry.get("status", "")), "version": str(entry.get("version", "")), "signer": str(entry.get("signer", ""))}  # 只回可核对字段，不回 drafter 之外的内部信息
