"""人工确认门：AI 建议必须挂待确认单，由接手人确认后才完成责任转移。"""  # 模块说明：本地 JSON 存储，标准库读写
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import time  # 标准库：生成时间戳与超时判断
import uuid  # 标准库：生成唯一待确认单编号
from typing import Any  # 标准库：单据类型标注
from .common import BizError, read_json, resolve_path, write_json  # 统一异常与本地存储


def create_ticket(cfg: dict, business_id: str, rule_version: str, role: str, actor: str, evidence_ids: list[str], suggestion: str, reason: str) -> dict:  # 创建待确认单
    """创建一张人工确认单，把 AI 建议冻结为待确认状态。
    入参：cfg 配置；business_id 业务编号；rule_version 资料版本；role 发起角色；actor 发起人标识；evidence_ids 事实引用；suggestion 建议文本；reason 触发原因。
    出参：完整待确认单 dict，状态恒为 pending。
    失败时的行为：本地存储损坏时由 read_json / write_json 抛出 BizError 或原始 IO 异常，不生成半张单据。
    """
    store = resolve_path(cfg["confirmation"]["store_path"])  # 待确认单路径只从配置读取
    db = read_json(store, {"tickets": {}})  # 读取已有单据，首次运行用空库
    ticket_id = "TK-" + uuid.uuid4().hex[:12]  # 标准库生成唯一编号，便于投诉关联
    now = time.time()  # 当前 Unix 时间戳
    ttl = float(cfg["confirmation"]["ttl_seconds"])  # 有效时长来自配置
    ticket: dict[str, Any] = {"ticket_id": ticket_id, "business_id": business_id, "rule_version": rule_version, "status": "pending", "created_by": actor, "created_role": role, "created_at": now, "expires_at": now + ttl, "evidence_ids": list(evidence_ids), "suggestion": suggestion, "trigger_reason": reason, "confirmed_by": "", "confirmed_role": "", "confirmed_at": 0, "decision": "", "note": ""}  # 字段对齐 f45 审计要素
    # 为什么这么卡：材料 f44 / f45 要求转人工标注触发原因、接手人、确认时间并能重建处理链，所以单据一次写全字段、后续只补签核
    db.setdefault("tickets", {})[ticket_id] = ticket  # 写入内存单据表
    write_json(store, db)  # 原子落盘，避免并发写坏
    return ticket  # 返回完整单据


def confirm_ticket(cfg: dict, ticket_id: str, role: str, actor: str, decision: str, note: str) -> dict:  # 人工确认落签核
    """校验并落签核：只有存在、未关闭、未过期、非自确认的单据可被确认。
    入参：cfg 配置；ticket_id 单据编号；role 确认角色；actor 确认人标识；decision accept / reject / revise；note 人工改动说明。
    出参：更新后的待确认单 dict。
    失败时的行为：不存在抛 TICKET_NOT_FOUND；已处理抛 TICKET_CLOSED；过期抛 TICKET_EXPIRED 并把状态改 expired；自确认抛 SELF_CONFIRM_BLOCKED；非法结论抛 DECISION_INVALID。
    """
    store = resolve_path(cfg["confirmation"]["store_path"])  # 待确认单路径只从配置读取
    db = read_json(store, {"tickets": {}})  # 读取单据表
    ticket = db.get("tickets", {}).get(ticket_id)  # 按编号查找单据
    if ticket is None:  # 单据不存在
        raise BizError("TICKET_NOT_FOUND", f"待确认单不存在：{ticket_id}")  # 拒绝凭空确认
    if ticket["status"] != "pending":  # 已确认 / 已关闭 / 已过期
        raise BizError("TICKET_CLOSED", f"待确认单当前状态为 {ticket['status']}，不可重复确认")  # 防止重复签核
    if time.time() > float(ticket["expires_at"]):  # 超过配置的有效期
        ticket["status"] = "expired"  # 落状态，避免同一单据一直挂起
        write_json(store, db)  # 原子写回
        raise BizError("TICKET_EXPIRED", "待确认单已超时作废，请重新发起预检")  # 明确要求重发
    if bool(cfg["confirmation"]["forbid_self_confirm"]) and actor == ticket["created_by"]:  # 配置了禁止自确认且是同一人
        # 为什么这么卡：材料 f23 / f44 要求对方确认接手才结束本岗待办，自己确认自己的建议等于没有责任转移
        raise BizError("SELF_CONFIRM_BLOCKED", "发起人不能确认自己发起的待确认单，请由接手岗位确认")  # 拒绝自审自批
    if decision not in ("accept", "reject", "revise"):  # 结论取值不在白名单
        raise BizError("DECISION_INVALID", "decision 只能是 accept / reject / revise")  # 拒绝非法结论
    ticket["status"] = "confirmed" if decision == "accept" else "closed"  # 采纳为已确认，其余为已关闭
    ticket["decision"] = decision  # 记录人工结论
    ticket["note"] = note  # 记录人工改动说明，纳入审计
    ticket["confirmed_by"] = actor  # 记录接手人
    ticket["confirmed_role"] = role  # 记录签核岗位
    ticket["confirmed_at"] = time.time()  # 记录确认时间
    write_json(store, db)  # 原子写回本地文件
    return ticket  # 返回更新后的单据
