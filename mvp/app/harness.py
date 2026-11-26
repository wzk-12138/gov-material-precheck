"""agent-harness：本地智能体运行壳，按固定顺序编排三道关卡与三项能力。"""  # 模块说明：不引入任何第三方智能体框架
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import json  # 标准库：读取规则库版本
import time  # 标准库：审计时间戳
from typing import Any  # 标准库：业务数据类型标注
from . import confirmation, graph, guards, knowledge, llm, permissions, rag, wiki  # 关卡、确认门、图谱、知识库、模型、权限、RAG、Wiki
from .common import BizError, append_audit, read_audit, read_json, resolve_path  # 统一异常与本地存储


class AgentHarness:  # 智能体运行壳：所有能力都从这里串起来
    """智能体运行壳：安全审查 → 权限校验 → 本地检索 → 本机推理 → 输出审查 → 人工确认 → 审计。
    入参：cfg 由 load_config 得到的配置字典。
    出参：对象本身；业务结果由 search / precheck / confirm / audit 返回。
    失败时的行为：配置缺 agent 节抛 CONFIG_INVALID；任一关卡抛 BizError，由 API 层统一转响应体。
    """

    def __init__(self, cfg: dict) -> None:  # 构造器：只校验配置并保存引用
        """初始化智能体运行壳。
        入参：cfg 由 load_config 读取的配置字典。
        出参：无，实例内保存 cfg 供全部下游能力取参数。
        失败时的行为：配置缺 agent 节抛 BizError(CONFIG_INVALID)，拒绝带病运行。
        """
        if not isinstance(cfg, dict) or "agent" not in cfg:  # 配置不完整不允许带病运行
            raise BizError("CONFIG_INVALID", "缺少 agent 配置节")  # 明确失败原因
        self.cfg = cfg  # 保存配置引用，所有下游能力都从这里取参数

    def rule_version(self) -> str:  # 读取当前资料版本
        """读取当前规则库的资料版本。
        入参：无，使用实例内的配置。
        出参：版本字符串；未建库返回 UNBUILT，文件损坏返回 UNKNOWN。
        失败时的行为：不抛异常，避免版本读取失败中断主链路。
        """
        path = resolve_path(self.cfg["knowledge"]["kb_path"])  # 规则库路径只从配置取
        if not path.exists():  # 尚未执行 build_kb.py
            return "UNBUILT"  # 明确标记，不伪装成已核验版本
        try:  # 捕获规则库损坏
            return str(json.loads(path.read_text(encoding="utf-8")).get("rule_version", "UNKNOWN"))  # 取版本号
        except Exception:  # 文件损坏
            return "UNKNOWN"  # 降级标记，主链路继续

    def search(self, role: str | None, query: str) -> dict:  # 知识库只读检索入口
        """只读检索：已登记角色按问题查本地规则库。
        入参：role 角色键；query 查询文本。
        出参：含 query / hits / count / rule_version 的业务数据 dict。
        失败时的行为：匿名或越权抛 ROLE_REQUIRED / ROLE_UNKNOWN；查询命中红线抛对应 BizError；规则库未生成抛 KB_MISSING。
        """
        guards.check_role(self.cfg, role, "audit")  # 先卡权限：已登记角色才能拉取规则库
        guards.review_input(self.cfg, query)  # 再卡安全：防止借检索接口塞入真实个人材料
        hits = knowledge.search(self.cfg, query)  # 本地标准库检索
        return {"query": query, "hits": hits, "count": len(hits), "rule_version": self.rule_version()}  # 返回带版本的结果

    def precheck(self, role: str | None, actor: str, business_id: str, question: str, materials: list[str]) -> dict:  # 清单预检主链路
        """清单预检主链路，按固定顺序执行八步（含知识图谱多跳证据链）。
        入参：role 角色键；actor 操作者标识；business_id 业务编号；question 咨询问题；materials 已提交材料名称列表。
        出参：业务数据 dict（非 HTTP 响应体），含建议、证据、图谱关联链、待确认单、模型状态与降级标记。
        失败时的行为：任一关卡抛 BizError；Ollama 不可用时按配置降级为仅检索提示，不中断人工流程；无依据时强制转人工。
        """
        permissions.forbid_admin_precheck(self.cfg, role)  # 第零步：管理员（SaaS）不得进入业务链，与白板④职责分离（管理员走 /admin/*）
        perm = guards.check_role(self.cfg, role, "precheck")  # 第一步：权限校验，无授权岗位不得发起预检
        tier = permissions.check_tier(self.cfg, role, "L1")  # 同时校验用户分层，L1 及以上才能发起预检（白板④）
        mats = [str(m) for m in (materials or [])]  # 规整材料名，容忍空列表
        raw_text = " ".join([question] + mats)  # 拼接待审文本，材料名也参与红线检查
        guards.review_input(self.cfg, raw_text)  # 第二步：安全审查，真实个人材料与越权审批请求拦在检索之前
        can_view = bool(perm.get("can_view_material", False))  # 该角色能否查看材料与证据原文
        ctx = rag.build_context(self.cfg, question, can_view)  # 第三步：RAG 检索 + 增强，得到上下文、证据与图谱关联编号
        hits = ctx["hits"]  # 取检索命中，供证据返回与审计
        graph_chain = {"hops": ctx["hops"], "related": ctx["related"], "nodes": [], "truncated": ctx["truncated"]}  # 图谱证据链：逐跳链与关联编号都来自 RAG 增强段
        graph_ready = bool(ctx["hops"]) or not hits  # 有命中却无逐跳链说明图谱降级；无命中不判定图谱故障
        rule_version = ctx["rule_version"]  # 第四步：取 RAG 段带回的资料版本，写入审计与待确认单
        evidence_ids = [str(h["id"]) for h in hits]  # 抽取证据编号，供审计重建处理链
        if not can_view:  # 无材料查看权限
            # 为什么这么卡：材料 f41 只允许授权岗位查看事项清单、材料样本等原文，热线等非授权岗位只能拿到编号，连提示词里都不塞原文
            public_hits = [{"id": h["id"], "short_id": h["short_id"], "topic": h["topic"], "score": h["score"]} for h in hits]  # 去掉 text / source / character
        else:  # 有查看权限
            public_hits = [dict(h) for h in hits]  # 原文随建议返回，供窗口逐项核对
        flagged = True  # 默认强制转人工，只有正常生成且输出审查通过才置 False
        if not hits:  # 第五步：无依据就不调模型，避免凭空生成
            suggestion = str(rag.rag_conf(self.cfg)["no_evidence_hint"])  # 用 RAG 配置里的兜底提示
            gen = {"text": "", "degraded": True, "reason": "未检索到可核对依据，未调用模型", "model": str(self.cfg["ollama"]["model"])}  # 标记降级原因
        else:  # 有依据才调用 RAG 生成段
            gen = rag.generate(self.cfg, str(self.cfg["agent"]["system_prompt"]), business_id, question, mats, ctx["context"])  # RAG 生成段：本机 Ollama，未就绪按配置降级
            base = gen["text"].strip() if gen["text"].strip() else str(rag.rag_conf(self.cfg)["degrade_hint"])  # 降级时用 RAG 检索提示兜底
            suggestion, output_hit = guards.review_output(self.cfg, base)  # 第六步：输出审查，越权结论直接脱敏
            flagged = output_hit or bool(gen["degraded"])  # 命中红线或模型降级都必须人工确认
        reason = "AI 结论须人工确认" if not flagged else "输出命中越权结论或模型降级，已强制转人工"  # 触发原因留痕
        ticket = confirmation.create_ticket(self.cfg, business_id, rule_version, str(role), actor, evidence_ids, suggestion, reason)  # 第八步：生成待确认单
        # 为什么这么卡：AI 预检只作提示、不输出批准结论，任何建议都必须挂人工确认单，接手人确认后才算责任转移（f40 / f44 / f45）
        append_audit(self.cfg, {"ts": time.time(), "action": "precheck", "business_id": business_id, "rule_version": rule_version, "role": role, "tier": tier, "actor": actor, "fact_refs": evidence_ids, "graph_refs": graph_chain["related"], "graph_hops": len(graph_chain["hops"]), "graph_ready": graph_ready, "suggestion": suggestion, "output_flagged": flagged, "ticket_id": ticket["ticket_id"], "human_change": "", "signer": ""})  # 写审计：业务编号、资料版本、事实引用、图谱关联、建议结果，附用户分层
        return {"business_id": business_id, "rule_version": rule_version, "role": role, "tier": tier, "actor": actor, "evidence": public_hits, "evidence_ids": evidence_ids, "graph_chain": graph_chain["hops"], "graph_refs": graph_chain["related"], "graph_ready": graph_ready, "wiki_summary": ctx.get("wiki_summary", ""), "suggestion": suggestion, "requires_human_confirmation": True, "ticket": {"ticket_id": ticket["ticket_id"], "status": ticket["status"], "expires_at": ticket["expires_at"], "trigger_reason": ticket["trigger_reason"]}, "model": {"name": gen["model"], "degraded": bool(gen["degraded"]), "reason": gen["reason"]}, "output_flagged": flagged}  # 返回业务数据，新增 tier 与 wiki_summary 字段

    def confirm(self, role: str | None, actor: str, ticket_id: str, decision: str, note: str) -> dict:  # 人工确认入口
        """人工确认：先校验岗位，再校验单据并落签核留痕。
        入参：role 确认角色；actor 确认人标识；ticket_id 待确认单编号；decision 人工结论；note 人工改动说明。
        出参：含 ticket_id / status / decision / 签核人与确认时间的业务数据 dict。
        失败时的行为：越权抛 ROLE_NOT_ALLOWED；单据问题抛 TICKET_* 错误；自确认抛 SELF_CONFIRM_BLOCKED。
        """
        guards.check_role(self.cfg, role, "confirm")  # 先卡权限：只有审核 / 监督岗位能接手确认
        ticket = confirmation.confirm_ticket(self.cfg, ticket_id, str(role), actor, decision, note)  # 再卡单据：存在性 / 状态 / 时效 / 禁止自确认
        # 为什么这么卡：确认人、确认时间、人工改动必须入审计，才能重建一次完整处理链（f45）
        append_audit(self.cfg, {"ts": time.time(), "action": "confirm", "business_id": ticket["business_id"], "rule_version": ticket["rule_version"], "role": role, "actor": actor, "fact_refs": ticket["evidence_ids"], "suggestion": ticket["suggestion"], "output_flagged": False, "ticket_id": ticket_id, "human_change": note, "signer": actor})  # 补齐人工改动与签核人
        return {"ticket_id": ticket_id, "status": ticket["status"], "decision": ticket["decision"], "confirmed_by": ticket["confirmed_by"], "confirmed_role": ticket["confirmed_role"], "confirmed_at": ticket["confirmed_at"], "note": ticket["note"]}  # 返回确认结果

    def _graph_perm(self, role: str | None) -> bool:  # 图谱查询的统一权限入口
        """校验图谱查询权限并返回是否可看原文。
        入参：role 角色键。
        出参：bool，True 表示可返回节点正文，False 表示只返回关联结构。
        失败时的行为：匿名或未登记角色抛 ROLE_REQUIRED / ROLE_UNKNOWN，由接口层统一改写。
        """
        perm = guards.check_role(self.cfg, role, "audit")  # 为什么这么卡：知识关联会暴露事实之间的关系，未登记角色连结构都不给看
        return bool(perm.get("can_view_material", False))  # 权限矩阵决定节点属性裁剪范围

    def graph_stats(self, role: str | None) -> dict:  # 图谱统计入口
        """返回知识图谱规模统计。
        入参：role 角色键。
        出参：含 ready / rule_version / nodes / edges / relations 的字典。
        失败时的行为：图谱未生成或快照损坏时返回 ready=False 的全零统计，健康检查不被拖垮。
        """
        self._graph_perm(role)  # 先卡权限，未登记角色不得读图谱规模
        try:  # 图谱缺失只降级，不抛异常
            return {"ready": True, **graph.stats(self.cfg)}  # 正常返回统计
        except BizError:  # 未建图谱或结构损坏
            return {"ready": False, "nodes": 0, "edges": 0, "relations": {}, "rule_version": "UNBUILT"}  # 明确标记未就绪

    def graph_relations(self, role: str | None, node_id: str, rel: str = "") -> dict:  # 知识关联查询入口
        """查询某个知识节点的全部关联。
        入参：role 角色键；node_id 节点编号；rel 可选关系语义过滤。
        出参：含 node / links / total / truncated 的字典。
        失败时的行为：越权抛 ROLE_*；编号或关系词命中红线抛对应 BizError；节点不存在抛 GRAPH_NODE_NOT_FOUND。
        """
        can_view = self._graph_perm(role)  # 先卡权限并取可见范围
        guards.review_input(self.cfg, f"{node_id} {rel}")  # 为什么这么卡：查询参数同样是外部输入，不能借编号或关系词把真实个人材料塞进系统
        return {"can_view_material": can_view, **graph.relations(self.cfg, str(node_id).strip(), str(rel).strip(), can_view)}  # 返回裁剪后的关联

    def graph_path(self, role: str | None, src: str, dst: str) -> dict:  # 知识之间最短关联链
        """查询两个知识节点之间的最短关联链。
        入参：role 角色键；src / dst 起止节点编号。
        出参：含 chain / hops / nodes / found 的字典。
        失败时的行为：越权或端点不存在抛对应 BizError；不连通返回 found=False 的空链。
        """
        can_view = self._graph_perm(role)  # 先卡权限并取可见范围
        guards.review_input(self.cfg, f"{src} {dst}")  # 安全审查同样覆盖两个端点编号
        return graph.shortest_path(self.cfg, str(src).strip(), str(dst).strip(), can_view)  # 返回最短关联链

    def graph_subgraph(self, role: str | None, node_id: str, depth: int = 2) -> dict:  # 多跳关联子图
        """以某个知识为中心取指定跳数内的关联子图。
        入参：role 角色键；node_id 中心节点编号；depth 跳数。
        出参：含 nodes / edges / depth / truncated 的字典。
        失败时的行为：越权或节点不存在抛对应 BizError；depth 超上限自动压到配置值。
        """
        can_view = self._graph_perm(role)  # 先卡权限并取可见范围
        guards.review_input(self.cfg, str(node_id))  # 安全审查覆盖中心编号
        return graph.subgraph(self.cfg, str(node_id).strip(), int(depth), can_view)  # 返回多跳子图

    def graph_relation_types(self, role: str | None) -> dict:  # 关系语义清单
        """列出图谱中全部知识关联语义及条数。
        入参：role 角色键。
        出参：含 items / total 的字典。
        失败时的行为：越权抛 ROLE_*；图谱缺失抛 GRAPH_MISSING。
        """
        self._graph_perm(role)  # 先卡权限，关系清单也属于图谱结构信息
        return graph.relation_types(self.cfg)  # 返回可核对清单

    def link_knowledge(self, role: str | None, actor: str, payload: dict) -> dict:  # 人工确认后登记知识关联
        """在人工确认单据已签核的前提下，登记一条新的知识关联。
        入参：role 登记角色；actor 登记人；payload 含 src / dst / rel / note / business_id / ticket_id。
        出参：含新关联与图谱统计的字典；同时写图谱快照与审计流水。
        失败时的行为：越权抛 ROLE_NOT_ALLOWED；输入命中红线抛对应 BizError；关系不在白名单抛 GRAPH_REL_NOT_ALLOWED；单据未确认 / 签核人不符 / 业务编号不符 / 三元组重复分别抛对应错误，均不写图谱。
        """
        guards.check_role(self.cfg, role, "confirm")  # 为什么这么卡：写图谱会改变后续所有预检的证据链，只有审核与监督岗位能碰
        src = str(payload.get("src", "")).strip()  # 取起点知识编号
        dst = str(payload.get("dst", "")).strip()  # 取终点知识编号
        rel = str(payload.get("rel", "")).strip()  # 取关系语义
        note = str(payload.get("note", "")).strip()  # 取人工核对依据
        business_id = str(payload.get("business_id", "")).strip()  # 取业务编号
        ticket_id = str(payload.get("ticket_id", "")).strip()  # 取已签核的单据编号
        guards.review_input(self.cfg, " ".join([src, dst, rel, note, business_id, ticket_id]))  # 安全审查：登记内容同样不得含真实个人材料或越权审批意图
        allowed = [str(x) for x in self.cfg["graph"]["writable_relations"]]  # 可登记关系白名单只从配置取
        if rel not in allowed:  # 不在白名单内
            raise BizError("GRAPH_REL_NOT_ALLOWED", f"关系语义 {rel} 不在人工登记白名单内")  # 拒绝自造关系类型
        db = read_json(resolve_path(self.cfg["confirmation"]["store_path"]), {"tickets": {}})  # 读取本地待确认单
        ticket = db.get("tickets", {}).get(ticket_id)  # 按编号取单据
        # 为什么这么卡：知识关联属于规则源变更，AI 与发起人都不能自行写入；必须由接手岗位本人凭已确认单据登记，才能重建「谁改了什么」（f44 / f45）
        if not ticket or str(ticket.get("status", "")) != "confirmed":  # 单据不存在、未确认或已作废
            raise BizError("GRAPH_CONFIRM_REQUIRED", "须提供已人工确认的待确认单编号")  # 未确认一律拒绝写入
        if str(ticket.get("confirmed_by", "")) != actor:  # 登记人不是签核人
            raise BizError("GRAPH_SIGNER_MISMATCH", "登记人必须是该单据的签核人，不得借用账号")  # 防止冒用他人签核
        if str(ticket.get("business_id", "")) != business_id:  # 单据与业务编号不一致
            raise BizError("GRAPH_TICKET_MISMATCH", "业务编号与待确认单不一致，拒绝登记")  # 防止复用旧单据
        added = graph.add_link(self.cfg, src, dst, rel, note, actor, ticket_id)  # 三元组去重后原子落盘
        append_audit(self.cfg, {"ts": time.time(), "action": "graph_link", "business_id": business_id, "rule_version": self.rule_version(), "role": role, "actor": actor, "fact_refs": [src, dst], "graph_refs": [], "graph_hops": 0, "graph_ready": True, "suggestion": f"登记知识关联 {src} -[{rel}]-> {dst}", "output_flagged": False, "ticket_id": ticket_id, "human_change": note, "signer": actor})  # 审计补齐人工改动与签核人
        return {"business_id": business_id, "ticket_id": ticket_id, "signed_by": actor, "link": added["link"], "stats": added["stats"]}  # 返回可核对结果

    def replay(self, role: str | None, business_id: str) -> dict:  # 按业务编号重建处理链
        """按业务编号把一次处理链从审计流水里重建出来。
        入参：role 角色键；business_id 业务编号。
        出参：含 business_id / chain（按时间排序）/ actions / total 的字典。
        失败时的行为：越权抛 ROLE_*；编号命中红线抛对应 BizError；无记录返回空链。
        """
        guards.check_role(self.cfg, role, "audit")  # 已登记角色才能重建处理链
        guards.review_input(self.cfg, str(business_id))  # 安全审查覆盖业务编号
        items = [r for r in read_audit(self.cfg, 100000) if str(r.get("business_id", "")) == business_id]  # 全量过滤该业务编号
        items.sort(key=lambda r: float(r.get("ts", 0)))  # 按时间戳排成先后链
        return {"business_id": business_id, "chain": items, "actions": [str(r.get("action", "")) for r in items], "total": len(items)}  # 返回可重建的处理链

    def audit(self, role: str | None, limit: int = 50) -> dict:  # 审计查询入口
        """查询本地审计流水。
        入参：role 角色键；limit 最多返回条数。
        出参：含 items 与 limit 的业务数据 dict。
        失败时的行为：匿名或未登记角色抛 ROLE_REQUIRED / ROLE_UNKNOWN；审计文件不存在时返回空列表。
        """
        guards.check_role(self.cfg, role, "audit")  # 已登记角色才能查审计，避免匿名读取流水
        return {"items": read_audit(self.cfg, limit), "limit": limit}  # 返回最近记录

    def wiki_list(self, role: str | None) -> dict:  # Wiki 条目清单
        """列出 Wiki 条目：业务岗只见 published，管理员可见 draft。
        入参：role 角色键（已认证）。
        出参：含 items / total / visible_draft 的业务数据 dict。
        失败时的行为：管理员身份判定失败抛 CONFIG_INVALID；快照缺失返回空列表。
        """
        admins = [str(x) for x in permissions.perm_conf(self.cfg)["admin_roles"]]  # 管理员角色清单只从配置取
        return wiki.list_entries(self.cfg, role, admins)  # 委托条目层按可见性过滤

    def wiki_get(self, role: str | None, node_id: str) -> dict:  # 取单条 Wiki 条目
        """按节点编号取一条 Wiki 条目并校验可见性。
        入参：role 角色键；node_id 节点编号。
        出参：裁剪后的条目 dict。
        失败时的行为：条目不存在抛 WIKI_ENTRY_NOT_FOUND；业务岗读 draft 抛 WIKI_DRAFT_NOT_PUBLISHED。
        """
        admins = [str(x) for x in permissions.perm_conf(self.cfg)["admin_roles"]]  # 管理员角色清单只从配置取
        guards.review_input(self.cfg, str(node_id))  # 编号同样是外部输入，先过安全审查
        return wiki.get_entry(self.cfg, node_id, role, admins)  # 委托条目层校验可见性

    def wiki_submit(self, role: str | None, actor: str, node_id: str, text: str, business_id: str) -> dict:  # 提交条目起草或修订
        """提交 Wiki 条目草稿并挂人工确认单，起草不等于发布。
        入参：role 提交角色；actor 提交人；node_id 节点编号；text 摘要；business_id 业务编号。
        出参：含草稿与待确认单的 dict。
        失败时的行为：管理员提交业务条目抛 ADMIN_FORBIDDEN_BIZ；摘要非法或节点不存在抛对应 WIKI_* / GRAPH_* 错误。
        """
        permissions.forbid_admin_precheck(self.cfg, role)  # 为什么这么卡：管理员只管配置与发布门禁，不能自己起草业务条目，避免既写口径又签核
        guards.check_role(self.cfg, role, "precheck")  # 只有授权业务岗可提交条目草稿
        entry = wiki.submit_entry(self.cfg, node_id, text, actor)  # 落 draft，未发布
        rule_version = self.rule_version()  # 取资料版本写入待确认单
        ticket = confirmation.create_ticket(self.cfg, business_id, rule_version, str(role), actor, [node_id], text, "Wiki 条目待人工签核发布")  # 挂确认单，发布必须先确认
        append_audit(self.cfg, {"ts": time.time(), "action": "wiki_submit", "business_id": business_id, "rule_version": rule_version, "role": role, "actor": actor, "fact_refs": [node_id], "graph_refs": [], "graph_hops": 0, "graph_ready": True, "suggestion": text, "output_flagged": True, "ticket_id": ticket["ticket_id"], "human_change": "", "signer": ""})  # 草稿动作留痕，output_flagged 恒 True 表示未经签核
        return {**entry, "ticket_id": ticket["ticket_id"], "requires_human_confirmation": True}  # 返回草稿与确认单编号

    def wiki_publish(self, role: str | None, actor: str, node_id: str, ticket_id: str) -> dict:  # 签核发布 Wiki 条目
        """人工确认后把 Wiki 草稿发布为 published，使其进入 RAG 语料。
        入参：role 签核角色；actor 签核人；node_id 节点编号；ticket_id 已确认单据编号。
        出参：含发布结果与分层信息的 dict。
        失败时的行为：签核人分层不足抛 WIKI_SIGNER_TIER_LOW；单据未确认或签核人不符抛对应错误。
        """
        guards.check_role(self.cfg, role, "confirm")  # 只有审核 / 监督岗位可签核发布
        tier = permissions.check_tier(self.cfg, role, str(wiki.wiki_conf(self.cfg)["publish_min_tier"]))  # 校验分层达到发布门槛（白板④）
        result = wiki.publish_entry(self.cfg, node_id, actor, tier, ticket_id)  # 发布草稿，否则抛对应错误
        append_audit(self.cfg, {"ts": time.time(), "action": "wiki_publish", "business_id": "", "rule_version": result["version"], "role": role, "tier": tier, "actor": actor, "fact_refs": [node_id], "graph_refs": [], "graph_hops": 0, "graph_ready": True, "suggestion": f"发布 Wiki 条目 {node_id}", "output_flagged": False, "ticket_id": ticket_id, "human_change": "", "signer": actor})  # 发布动作单独留痕，签核人入审计
        return {**result, "tier": tier}  # 返回发布结果与签核分层

    def admin_roles(self, role: str | None) -> dict:  # 只读角色矩阵
        """返回管理员与用户分层的只读矩阵，供管理员查看权限结构。
        入参：role 角色键（已认证为管理员）。
        出参：含 admins / tiers / role_tier 的 dict。
        失败时的行为：非管理员抛 ADMIN_REQUIRED。
        """
        permissions.check_admin(self.cfg, role)  # 先卡管理员身份，业务岗不得读取权限矩阵
        return permissions.role_matrix(self.cfg)  # 返回可公开的角色矩阵，不含凭据

    def admin_action(self, role: str | None, actor: str, action: str, target: str, second_admin: str = "") -> dict:  # 管理员动作
        """执行一个管理员动作，敏感动作需第二位管理员复核，且单独留痕。
        入参：role 管理员角色；actor 操作人；action 动作名；target 目标标识；second_admin 复核管理员标识。
        出参：含 action / status / need_second 的 dict，敏感动作缺复核时 status=pending。
        失败时的行为：非管理员抛 ADMIN_REQUIRED；动作非法抛 ADMIN_ACTION_UNKNOWN。
        """
        permissions.check_admin(self.cfg, role)  # 先卡管理员身份，业务岗一律拒绝
        allowed = set(permissions.perm_conf(self.cfg).get("sensitive_actions", [])) | {"enable_tenant", "create_user", "rotate_token"}  # 合法动作集合：敏感动作 + 常规动作
        if str(action) not in allowed:  # 动作不在白名单
            raise BizError("ADMIN_ACTION_UNKNOWN", f"未登记的管理动作：{action}")  # 拒绝自造动作名
        review = permissions.check_sensitive(self.cfg, str(action), second_admin)  # 判定是否需要双人复核
        status = "applied" if review["ready"] else "pending"  # 敏感动作缺复核只落 pending，不直接生效
        # 为什么这么卡：白板④的管理员权限过大，关闭租户、批量改分层、发版本必须双人复核，单人提交不能立刻生效
        permissions.append_admin_audit(self.cfg, {"actor": actor, "role": role, "action": str(action), "target": str(target), "status": status, "need_second": review["need_second"], "second_admin": second_admin})  # 管理员动作单独留痕，与业务审计分开
        return {"action": str(action), "target": str(target), "status": status, "need_second": review["need_second"], "confirmed_by": review["confirmed_by"]}  # 返回执行状态

    def admin_audit(self, role: str | None, limit: int = 50) -> dict:  # 管理员审计查询
        """查询管理员动作审计流水。
        入参：role 管理员角色；limit 最多返回条数。
        出参：含 items / limit 的 dict。
        失败时的行为：非管理员抛 ADMIN_REQUIRED；流水不存在返回空列表。
        """
        permissions.check_admin(self.cfg, role)  # 只有管理员可查管理员审计
        return {"items": permissions.read_admin_audit(self.cfg, limit), "limit": limit}  # 返回最近管理员动作
