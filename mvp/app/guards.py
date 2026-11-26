"""三道关卡：安全审查、权限校验、输出审查。"""  # 模块说明：关键业务逻辑集中在此，行内注释写清「为什么这么卡」
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import re  # 标准库：数据红线正则匹配
from typing import Any  # 标准库：权限字典类型标注
from .common import BizError  # 统一业务异常


def review_input(cfg: dict, text: str) -> None:  # 输入安全审查
    """安全审查：拦住真实个人材料与越权审批请求。
    入参：cfg 配置字典；text 用户提交的完整待审文本（问题 + 材料名）。
    出参：无，审查通过返回 None。
    失败时的行为：超长抛 INPUT_TOO_LONG；命中个人材料特征抛 PERSONAL_DATA_BLOCKED；命中越权审批词抛 APPROVAL_REQUEST_BLOCKED；均不写入原文。
    """
    red = cfg["redlines"]  # 红线配置统一从 config.yaml 读取，代码不内嵌规则
    if len(text) > int(red["max_input_chars"]):  # 先卡长度，避免超长文本拖垮本地检索
        raise BizError("INPUT_TOO_LONG", "输入超出允许长度，请精简后重试")  # 直接拒绝，不进入后续链路
    for pattern in red["personal_data_patterns"]:  # 逐条检查疑似真实个人材料
        if re.search(pattern, text):  # 命中身份证 / 手机号等特征
            # 为什么这么卡：数据红线规定只用合成训练资料作规则源，任何真实个人材料一律不得进系统，连日志都不能留原文
            raise BizError("PERSONAL_DATA_BLOCKED", "输入含疑似真实个人材料，已拦截且未写入任何记录")  # 只回结论，不回显原文
    for word in red["forbidden_input_words"]:  # 逐条检查越权审批意图
        if word in text:  # 命中要求系统作行政审批或解释法律效力的说法
            # 为什么这么卡：AI 预检只作提示，不代替行政审批、不解释真实法律效力（访谈 f40 / f52）
            raise BizError("APPROVAL_REQUEST_BLOCKED", "本系统不输出批准结论，也不解释法律效力，请转人工办理")  # 明确边界并指引入工


def check_role(cfg: dict, role: str | None, action: str = "precheck") -> dict:  # 权限校验
    """权限校验：只有配置内已登记的授权岗位可执行对应动作。
    入参：cfg 配置字典；role 请求头 X-Role 的角色键；action 动作名（precheck / confirm / audit）。
    出参：该角色的权限字典，供上层决定是否展示材料原文。
    失败时的行为：角色缺失抛 ROLE_REQUIRED；未登记抛 ROLE_UNKNOWN；无对应动作权限抛 ROLE_NOT_ALLOWED。
    """
    if role is None or str(role).strip() == "":  # 请求头缺失或全空白
        # 为什么这么卡：材料 f41 要求只有授权业务岗位可查看相关资料且不能借用账号，因此没有角色绝不隐式放行
        raise BizError("ROLE_REQUIRED", "缺少 X-Role 请求头，请表明授权岗位")  # 拒绝匿名调用
    roles = cfg["roles"]  # 角色白名单来自 config.yaml
    if role not in roles:  # 不在白名单内
        # 为什么这么卡：白名单之外的角色键一律不认，防止伪造请求头绕过权限矩阵
        raise BizError("ROLE_UNKNOWN", f"未登记的角色：{role}")  # 回显角色键便于排查
    perm: dict[str, Any] = roles[role]  # 取出该角色的权限字典
    if action == "audit":  # 审计查询只需已登记
        return perm  # 直接放行，不额外判断动作权限
    key = "allow_precheck" if action == "precheck" else "allow_confirm"  # 动作对应的权限字段
    if not bool(perm.get(key, False)):  # 该角色没有对应动作权限
        # 为什么这么卡：人工确认必须由审核 / 监督岗位接手，窗口不能自己确认自己发起的建议（f22 / f44）
        raise BizError("ROLE_NOT_ALLOWED", f"角色 {role} 无权执行 {action}")  # 拒绝越权操作
    return perm  # 返回权限字典


def review_output(cfg: dict, text: str) -> tuple[str, bool]:  # 输出安全审查
    """安全审查：模型输出出现批准 / 效力结论时脱敏并强制转人工。
    入参：cfg 配置字典；text 模型生成文本或降级兜底文本。
    出参：元组 (可外发文本, 是否命中红线需人工复核)。
    失败时的行为：不抛异常；命中时返回脱敏文本与 True，由上层挂人工确认单，绝不外发原文。
    """
    for word in cfg["redlines"]["forbidden_output_words"]:  # 逐条检查越权结论词
        if word in text:  # 命中批准 / 受理 / 效力类表述
            # 为什么这么卡：模型可能被提示注入诱导输出行政结论，必须截住原文并转人工，不能让越权结论外发（f40 / f42）
            return ("[已脱敏] 输出含越权结论，已强制转人工复核；本系统仅提供清单预检与补正解释提示。", True)  # 不回显原文
    return (text.strip(), False)  # 未命中则原样返回，仅去掉首尾空白
