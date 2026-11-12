"""对外接口认证卡点：角色与令牌绑定，凭据只从环境变量读取，不落盘、不写死。"""  # 模块说明：标准库实现，不引入任何认证框架或网关
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import hmac  # 标准库：常量时间比较，避免逐字符试探令牌
import json  # 标准库：解析环境变量里的角色凭据表
import os  # 标准库：读取运行时注入的环境变量
from typing import Any  # 标准库：凭据表类型标注
from .common import BizError  # 统一业务异常，由接口层全局处理器改成统一响应体


def load_role_tokens(cfg: dict) -> dict[str, str]:  # 读取角色凭据表
    """从配置指定的环境变量读取角色到令牌的映射。
    入参：cfg 配置字典，取 auth.token_env 作为环境变量名。
    出参：dict，键为角色键、值为令牌字符串。
    失败时的行为：未注入抛 AUTH_NOT_CONFIGURED；不是合法映射、含空令牌或不同角色共用同一令牌时抛 AUTH_BAD_TOKENS / AUTH_TOKEN_DUPLICATED，绝不回退到默认凭据。
    """
    env_name = str(cfg["auth"]["token_env"])  # 环境变量名只从配置取，代码不写死凭据来源
    raw = os.environ.get(env_name, "")  # 每次请求都重读，支持运行时轮换令牌而无需重启
    if not raw.strip():  # 凭据表未注入
        # 为什么这么卡：不能因为嫌麻烦就退回明文 X-Role 模式，否则「不得借用账号」这条红线形同虚设（f41 / f45）
        raise BizError("AUTH_NOT_CONFIGURED", f"未注入凭据环境变量 {env_name}")  # 明确要求注入，不提供任何默认令牌
    try:  # 捕获非法 JSON
        data: Any = json.loads(raw)  # 解析 {"window":"令牌",...} 形式的凭据表
    except Exception as exc:  # JSON 格式错误
        raise BizError("AUTH_BAD_TOKENS", "凭据表不是合法 JSON") from exc  # 不回显原文，避免日志泄露凭据
    if not isinstance(data, dict) or not data:  # 根节点必须是非空映射
        raise BizError("AUTH_BAD_TOKENS", "凭据表必须是角色到令牌的非空映射")  # 拒绝列表或空表
    tokens = {str(k): str(v) for k, v in data.items()}  # 规整为字符串键值对
    if any(not v.strip() for v in tokens.values()):  # 存在空令牌
        raise BizError("AUTH_BAD_TOKENS", "凭据表存在空令牌")  # 空令牌等于未签发
    if len(set(tokens.values())) != len(tokens):  # 两个角色共用同一令牌
        # 为什么这么卡：共用令牌时无法判定请求到底代表哪个岗位，审计里的签核人也就不可信（f45）
        raise BizError("AUTH_TOKEN_DUPLICATED", "不同角色不得共用同一令牌")  # 拒绝歧义凭据
    return tokens  # 返回可核对的凭据表


def verify_request(cfg: dict, role: str | None, token: str | None) -> str:  # 认证主卡点
    """校验请求的角色头与凭据是否一致且属于同一角色。
    入参：cfg 配置字典；role 请求头 X-Role；token 请求头里的凭据。
    出参：通过时返回已校验的角色键字符串；auth.required=false 时返回 "disabled"。
    失败时的行为：缺角色抛 ROLE_REQUIRED、未登记角色抛 ROLE_UNKNOWN、缺凭据抛 AUTH_REQUIRED、无效凭据抛 AUTH_FAILED、凭据与角色不符抛 AUTH_ROLE_MISMATCH；一律不进入业务层。
    """
    if not bool(cfg["auth"]["required"]):  # 显式关闭认证（仅本地调试允许）
        return "disabled"  # 返回标记，供调用方记录降级状态
    if role is None or not str(role).strip():  # 缺少角色头
        # 为什么这么卡：先卡角色再卡凭据，保持权限矩阵的错误码语义，匿名请求不可能绕过权限校验
        raise BizError("ROLE_REQUIRED", "缺少 X-Role 请求头，请表明授权岗位")  # 与 guards 保持同一错误码
    key = str(role).strip()  # 规整角色键
    if key not in cfg["roles"]:  # 角色不在配置白名单
        raise BizError("ROLE_UNKNOWN", f"未登记的角色：{key}")  # 拒绝伪造角色键，不泄露凭据表内容
    tokens = load_role_tokens(cfg)  # 读取运行时凭据表
    supplied = str(token or "").strip()  # 规整请求凭据
    if not supplied:  # 未提供凭据
        # 为什么这么卡：明文 X-Role 可以任意伪造，必须持有该角色签发的令牌才能调用对外接口（f41）
        raise BizError("AUTH_REQUIRED", "缺少凭据请求头，禁止仅凭明文角色调用")  # 拒绝无凭据调用
    owner = ""  # 记录该令牌实际归属的角色
    for role_key, expected in tokens.items():  # 遍历凭据表找归属
        if hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):  # 转 UTF-8 字节做常量时间比较，非 ASCII 令牌也不会抛 TypeError
            owner = role_key  # 找到归属角色
            break  # 已确定归属即可结束
    if not owner:  # 令牌不在凭据表内
        raise BizError("AUTH_FAILED", "凭据无效或已轮换，请重新获取")  # 不区分「错」与「不存在」，避免探测
    if owner != key:  # 令牌属于别的角色
        # 为什么这么卡：材料 f41 明确不得借用账号，用窗口的令牌冒充审核员去签核会污染责任链（f23 / f44）
        raise BizError("AUTH_ROLE_MISMATCH", "凭据与请求角色不一致，禁止借用账号")  # 拒绝跨岗位冒用
    return key  # 返回已校验角色，业务层仍按原权限矩阵再判一次
