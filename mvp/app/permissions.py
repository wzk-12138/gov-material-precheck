"""权限主体（白板④）：管理员（SaaS）与用户分层两支判定，租户隔离。"""  # 模块说明：纯配置驱动，请求体不参与鉴权，不引入任何权限框架
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import time  # 标准库：管理员审计时间戳
from typing import Any  # 标准库：权限上下文类型标注
from .common import BizError, append_audit, read_json, resolve_path, write_json  # 统一异常、审计追加与本地存储


def perm_conf(cfg: dict) -> dict:  # 读取权限配置节
    """读取并校验 config.yaml 的 permissions 节。
    入参：cfg 配置字典。
    出参：permissions 配置子字典。
    失败时的行为：缺节或字段类型不符抛 BizError(CONFIG_INVALID)，绝不在代码里补默认角色或层级。
    """
    conf = cfg.get("permissions")  # 权限参数一律从配置读取
    if not isinstance(conf, dict):  # 配置节缺失或写成了标量
        raise BizError("CONFIG_INVALID", "config.yaml 缺少 permissions 配置节")  # 明确失败，不带病判权
    for key in ("admin_roles", "tiers", "role_tier", "admin_can_precheck", "admin_audit_path"):  # 必需字段
        if key not in conf:  # 缺任一字段
            raise BizError("CONFIG_INVALID", f"permissions 配置节缺少字段：{key}")  # 指出具体缺失项
    return conf  # 返回权限配置


def is_admin(cfg: dict, role: str | None) -> bool:  # 判定是否为管理员角色
    """判断角色是否属于管理员（SaaS）两支之一。
    入参：cfg 配置字典；role 角色键。
    出参：bool，True 表示平台管理员或租户管理员。
    失败时的行为：角色为空返回 False，不抛异常，由上层按业务岗流程再校验。
    """
    if not role:  # 空角色直接判否
        return False  # 交给上层抛 ROLE_REQUIRED
    return str(role) in [str(x) for x in perm_conf(cfg)["admin_roles"]]  # 只认配置白名单内的管理员


def tier_of(cfg: dict, role: str | None) -> str:  # 取角色的用户分层
    """按配置的角色到分层绑定，返回该角色的分层标记。
    入参：cfg 配置字典；role 角色键。
    出参：分层字符串（L1 / L2 / L3）；管理员或未知角色返回空串。
    失败时的行为：不在 role_tier 映射内的角色返回空串，不臆造层级。
    """
    conf = perm_conf(cfg)  # 取权限配置
    return str(conf["role_tier"].get(str(role), ""))  # 只从配置绑定取，取不到为空


def tier_rank(cfg: dict, tier: str) -> int:  # 分层转可比较的序号
    """把分层标记转成从 1 开始的序号，便于比较层级高低。
    入参：cfg 配置字典；tier 分层标记。
    出参：int 序号；不在配置清单内返回 0。
    失败时的行为：未知分层返回 0，调用方据此判定层级不足，不抛异常。
    """
    tiers = [str(x) for x in perm_conf(cfg)["tiers"]]  # 分层清单来自配置且有序
    return tiers.index(str(tier)) + 1 if str(tier) in tiers else 0  # 存在则给 1 起的序号，否则 0


def check_tier(cfg: dict, role: str | None, need: str) -> str:  # 分层是否达标
    """校验角色分层是否达到动作要求的最低层级。
    入参：cfg 配置字典；role 角色键；need 要求的最低分层（如 L2）。
    出参：通过时返回该角色实际分层字符串。
    失败时的行为：层级不足抛 TIER_INSUFFICIENT；最低层级配置非法抛 CONFIG_INVALID；未绑定分层抛 ROLE_UNKNOWN。
    """
    conf = perm_conf(cfg)  # 取权限配置
    tiers = [str(x) for x in conf["tiers"]]  # 合法分层清单
    if str(need) not in tiers:  # 要求的最低层级本身非法
        raise BizError("CONFIG_INVALID", f"未登记的最低分层：{need}")  # 配置错误，不带病判权
    if is_admin(cfg, role):  # 管理员不参与业务分层判定
        # 为什么这么卡：管理员只管平台与账号，不签核业务结论；用分层口径去放行等于让管理员混入业务链（白板④职责分离）
        raise BizError("ADMIN_FORBIDDEN_BIZ", "管理员不得参与业务分层动作")  # 管理员走管理接口，不走分层接口
    actual = tier_of(cfg, role)  # 取角色实际分层
    if not actual:  # 角色未绑定分层
        raise BizError("ROLE_UNKNOWN", f"角色 {role} 未绑定用户分层")  # 拒绝未分层角色
    if tier_rank(cfg, actual) < tier_rank(cfg, str(need)):  # 层级不足
        # 为什么这么卡：白板④要求用户分层决定能看多少原文、能否签核；越层签核会让低权限岗位接触超出授权的业务动作
        raise BizError("TIER_INSUFFICIENT", f"当前分层 {actual} 低于要求 {need}")  # 明确拒绝越层
    return actual  # 返回达标分层，供上层写入审计


def check_admin(cfg: dict, role: str | None) -> str:  # 管理员权限校验
    """校验角色是否可执行管理员动作。
    入参：cfg 配置字典；role 角色键。
    出参：通过时返回管理员角色键。
    失败时的行为：非管理员抛 ADMIN_REQUIRED；未登记角色抛 ROLE_UNKNOWN。
    """
    conf = perm_conf(cfg)  # 取权限配置
    if not role:  # 空角色
        raise BizError("ROLE_REQUIRED", "缺少管理员角色")  # 与业务接口错误码保持一致
    if is_admin(cfg, role):  # 命中管理员白名单
        return str(role)  # 放行管理接口
    if str(role) in conf["role_tier"]:  # 是已登记的业务岗
        # 为什么这么卡：业务岗调用管理接口会形成「既出结论又改权限」的自审自批，白板④要求两支职责严格分离
        raise BizError("ADMIN_REQUIRED", "该动作仅限管理员，业务岗无权执行")  # 明确拒绝越权
    raise BizError("ROLE_UNKNOWN", f"未登记的角色：{role}")  # 白名单外角色


def forbid_admin_precheck(cfg: dict, role: str | None) -> None:  # 管理员不得发起业务预检
    """在业务入口前置拦截管理员，确保管理权与业务权分离。
    入参：cfg 配置字典；role 角色键。
    出参：无，业务岗直接返回。
    失败时的行为：配置为管理员禁预检且当前为管理员时抛 ADMIN_FORBIDDEN_BIZ。
    """
    if not bool(perm_conf(cfg)["admin_can_precheck"]) and is_admin(cfg, role):  # 管理员被禁止发起业务且当前是管理员
        # 为什么这么卡：白板④把管理员与业务岗分成两支；管理员能改规则版本，若还能发起预检就能自造依据再自证结论
        raise BizError("ADMIN_FORBIDDEN_BIZ", "管理员不得发起业务预检，请使用业务岗账号")  # 拒绝管理员进入业务链


def check_tenant(tenant_from_token: str, tenant_of_resource: str) -> str:  # 租户隔离校验
    """校验令牌租户与资源租户是否一致，防止跨租户越权。
    入参：tenant_from_token 令牌归属租户；tenant_of_resource 被访问资源所属租户。
    出参：通过时返回一致的租户键。
    失败时的行为：租户不一致抛 TENANT_MISMATCH；任一侧为空抛 TENANT_REQUIRED。
    """
    token_tenant = str(tenant_from_token or "").strip()  # 规整令牌租户
    resource_tenant = str(tenant_of_resource or "").strip()  # 规整资源租户
    if not token_tenant or not resource_tenant:  # 任一侧缺失
        raise BizError("TENANT_REQUIRED", "租户标识缺失，无法判权")  # 拒绝无租户上下文的访问
    if token_tenant != resource_tenant:  # 令牌租户与资源租户不一致
        # 为什么这么卡：SaaS 多租户下数据必须隔离；跨租户查询若返回空结果会误导调用方以为「没有数据」，因此直接报错
        raise BizError("TENANT_MISMATCH", "令牌租户与资源租户不一致，禁止跨租户访问")  # 明确拒绝跨租户
    return token_tenant  # 返回一致的租户键


def check_sensitive(cfg: dict, action: str, confirmed_by: str = "") -> dict:  # 敏感管理动作双人复核
    """校验敏感管理动作是否具备第二位管理员复核。
    入参：cfg 配置字典；action 管理动作名；confirmed_by 复核管理员标识，空表示尚未复核。
    出参：含 action / need_second / ready / confirmed_by 的字典。
    失败时的行为：动作不在敏感清单时直接返回 ready=True；在清单且缺复核人时返回 ready=False，由上层落 pending 而非直接执行。
    """
    conf = perm_conf(cfg)  # 取权限配置
    sensitive = [str(x) for x in conf.get("sensitive_actions", [])]  # 敏感动作清单来自配置
    need_second = str(action) in sensitive  # 是否属于敏感动作
    ready = (not need_second) or bool(str(confirmed_by).strip())  # 非敏感或已有复核人即就绪
    return {"action": str(action), "need_second": need_second, "ready": ready, "confirmed_by": str(confirmed_by)}  # 返回复核状态供上层决策


def append_admin_audit(cfg: dict, record: dict) -> None:  # 管理员动作单独留痕
    """把一次管理员动作写入独立审计流水，与业务审计分开。
    入参：cfg 配置字典；record 管理员动作记录字典。
    出参：无。
    失败时的行为：路径或写入异常原样抛出，由接口层统一处理；只在记录缺时间戳时补当前时间。
    """
    conf = perm_conf(cfg)  # 取权限配置
    path = resolve_path(str(conf["admin_audit_path"]))  # 管理员审计路径只从配置取
    path.parent.mkdir(parents=True, exist_ok=True)  # 确保 data/ 目录存在
    if "ts" not in record:  # 调用方未给时间戳
        record = {**record, "ts": time.time()}  # 补当前时间，不改动传入字典
    append_audit_entry(path, record)  # 追加写入独立流水


def append_audit_entry(path: Any, record: dict) -> None:  # 追加一条 JSONL 到指定路径
    """把一条管理员审计记录追加到指定 JSONL 文件。
    入参：path 目标路径（Path 对象）；record 记录字典。
    出参：无。
    失败时的行为：写入异常原样抛出；单行一条 JSON，便于按行回溯。
    """
    import json  # 标准库：JSON 序列化，局部导入避免模块顶部冗余
    with path.open("a", encoding="utf-8") as fh:  # 追加模式，保留全部历史
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")  # 一行一条 JSON，中文不转义


def read_admin_audit(cfg: dict, limit: int = 50) -> list[dict]:  # 读取管理员审计流水
    """读取最近若干条管理员动作记录。
    入参：cfg 配置字典；limit 最多返回条数。
    出参：管理员审计记录列表，按写入顺序返回。
    失败时的行为：文件不存在返回空列表；单行损坏跳过该行，不让整个接口失败。
    """
    import json  # 标准库：JSON 反序列化，局部导入避免模块顶部冗余
    path = resolve_path(str(perm_conf(cfg)["admin_audit_path"]))  # 管理员审计路径只从配置取
    if not path.exists():  # 尚未产生管理员动作
        return []  # 返回空列表而非报错
    out: list[dict] = []  # 结果容器
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:  # 只取最后 limit 行
        try:  # 单行损坏不应影响其余记录
            out.append(json.loads(line))  # 反序列化单条记录
        except Exception:  # 跳过坏行
            continue  # 保持其余记录可用
    return out  # 返回管理员审计列表


def role_matrix(cfg: dict) -> dict:  # 供只读展示的角色矩阵
    """汇总管理员角色与用户分层，供 /admin/roles 只读展示。
    入参：cfg 配置字典。
    出参：含 admins / tiers / role_tier 的字典，不含任何令牌或凭据。
    失败时的行为：配置缺节抛 BizError(CONFIG_INVALID)。
    """
    conf = perm_conf(cfg)  # 取权限配置
    admins = [{"role": str(r), "duty": str(conf.get("role_descriptions", {}).get(r, ""))} for r in conf["admin_roles"]]  # 管理员职责清单
    return {"admins": admins, "tiers": [str(x) for x in conf["tiers"]], "role_tier": {str(k): str(v) for k, v in conf["role_tier"].items()}}  # 只回可公开的矩阵
