"""统一响应、配置加载与本地存储：所有模块共用的基础设施。"""  # 模块说明：不含业务逻辑
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import json  # 标准库：JSON / JSONL 序列化
import threading  # 标准库：本地文件写锁，避免并发写坏数据
from pathlib import Path  # 标准库：路径解析
from typing import Any  # 标准库：统一返回值的类型标注
import yaml  # 配置解析库：读取 config.yaml，不是中间件

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：app/ 的上一级
CONFIG_PATH = PROJECT_ROOT / "config.yaml"  # 配置文件唯一位置，代码内不另设路径
_WRITE_LOCK = threading.Lock()  # 本地文件写锁：串行化 JSON 与 JSONL 写入


class BizError(Exception):  # 业务异常：所有关卡失败统一抛这一种异常
    """业务异常：携带统一错误码与中文说明。
    入参：code 错误码字符串；msg 中文错误说明。
    出参：异常对象，属性 code / msg 供 API 层全局处理器读取。
    失败时的行为：不吞异常、不落盘原文，由上层转成统一响应体。
    """

    def __init__(self, code: str, msg: str) -> None:  # 构造器：只保存错误码与说明
        """初始化业务异常。
        入参：code 错误码字符串；msg 中文错误说明。
        出参：无，异常对象就地初始化，属性 code / msg 可被 API 层读取。
        失败时的行为：不吞异常、不落盘原文，由上层转成统一响应体。
        """
        super().__init__(msg)  # 调用父类构造，保留可读错误信息
        self.code = code  # 错误码，例如 ROLE_REQUIRED
        self.msg = msg  # 给调用方看的中文说明


def ok(data: Any) -> dict:  # 成功响应构造器
    """构造统一成功响应。
    入参：data 任意业务数据。
    出参：dict，恒含 ok / data / error 三键，error 也是含 code 与 msg 的对象。
    失败时的行为：无失败分支，数据原样返回，不在此处做业务判断。
    """
    return {"ok": True, "data": data, "error": {"code": "OK", "msg": ""}}  # 三键固定，成功时 error 也保持同构


def fail(code: str, msg: str) -> dict:  # 失败响应构造器
    """构造统一失败响应。
    入参：code 错误码；msg 中文错误说明。
    出参：dict，ok=False、data=None、error 含 code 与 msg。
    失败时的行为：无失败分支；不回显堆栈与输入原文，避免泄露本地规则库内容。
    """
    return {"ok": False, "data": None, "error": {"code": code, "msg": msg}}  # 与成功响应结构完全一致


def load_config(path: str = "") -> dict:  # 配置加载：唯一入口
    """读取并校验 config.yaml。
    入参：path 可选配置文件路径；为空时使用项目根下的 config.yaml。
    出参：完整配置字典。
    失败时的行为：文件缺失抛 BizError(CONFIG_LOAD_FAILED)，YAML 非法或缺配置节抛 BizError(CONFIG_INVALID)；绝不用代码内默认值补位。
    """
    cfg_path = Path(path) if path else CONFIG_PATH  # 允许自测传临时配置，默认只认唯一配置文件
    if not cfg_path.exists():  # 配置文件不存在时不能静默降级
        raise BizError("CONFIG_LOAD_FAILED", f"配置文件不存在：{cfg_path}")  # 明确失败原因，避免密钥被写死进代码
    try:  # 捕获 YAML 解析异常
        with cfg_path.open("r", encoding="utf-8") as fh:  # 以 UTF-8 打开，中文注释不乱码
            cfg = yaml.safe_load(fh)  # safe_load 不构造任意对象，防止配置注入
    except Exception as exc:  # 任何解析或 IO 异常
        raise BizError("CONFIG_INVALID", f"config.yaml 解析失败：{exc}") from exc  # 保留原始异常链
    if not isinstance(cfg, dict):  # 配置根节点必须是映射
        raise BizError("CONFIG_INVALID", "config.yaml 根节点必须是映射")  # 拒绝空文件或列表结构
    for key in ("service", "auth", "agent", "ollama", "knowledge", "roles", "redlines", "confirmation", "audit"):  # 必需配置节
        if key not in cfg:  # 缺任何一节都算配置不完整
            raise BizError("CONFIG_INVALID", f"config.yaml 缺少配置节：{key}")  # 指出具体缺失项，便于修复
    return cfg  # 返回完整配置字典


def resolve_path(p: str) -> Path:  # 配置内相对路径解析器
    """把配置里的路径解析为绝对路径。
    入参：p 配置里写的路径字符串，可绝对可相对。
    出参：Path 绝对路径对象；相对路径一律挂到项目根目录。
    失败时的行为：无失败分支；不创建目录，由调用方按需创建。
    """
    path = Path(p)  # 先按原始字符串构造路径对象
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()  # 绝对路径原样，相对路径基于项目根


def read_json(path: Path, default: Any) -> Any:  # 本地 JSON 读取
    """读取本地 JSON 文件。
    入参：path 文件路径；default 文件不存在时返回的默认值。
    出参：反序列化后的对象，或 default。
    失败时的行为：文件不存在返回 default；内容损坏抛 BizError(STORE_READ_FAILED)。
    """
    if not path.exists():  # 首次运行时文件还没生成
        return default  # 返回调用方给的默认值，不创建隐式状态
    try:  # 捕获损坏的 JSON 或 IO 异常
        with path.open("r", encoding="utf-8") as fh:  # UTF-8 打开
            return json.load(fh)  # 标准库反序列化
    except Exception as exc:  # 解析失败
        raise BizError("STORE_READ_FAILED", f"读取 {path.name} 失败：{exc}") from exc  # 转成统一业务错误


def write_json(path: Path, payload: Any) -> None:  # 本地 JSON 原子写
    """原子写本地 JSON 文件。
    入参：path 目标文件路径；payload 可序列化对象。
    出参：无。
    失败时的行为：目录创建失败或磁盘异常时原样抛出，由 API 层统一转成 ERR_INTERNAL；先写临时文件再替换，绝不留下半截文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)  # 确保 data/ 或 kb/ 目录存在
    with _WRITE_LOCK:  # 串行化写入，避免并发请求互相覆盖
        tmp = path.with_suffix(path.suffix + ".tmp")  # 先写同目录临时文件
        with tmp.open("w", encoding="utf-8") as fh:  # UTF-8 写入
            json.dump(payload, fh, ensure_ascii=False, indent=2)  # 中文不转义，便于人工核对
        tmp.replace(path)  # 原子替换，读方不会读到半截内容


def append_audit(cfg: dict, record: dict) -> None:  # 审计流水追加
    """追加一条审计记录到本地 JSONL。
    入参：cfg 配置字典（审计路径取自 audit.path）；record 一条审计字典。
    出参：无。
    失败时的行为：目录创建或写入失败时原样抛出，由 API 层统一处理；单行一条 JSON，便于标准库逐行回溯。
    """
    path = resolve_path(cfg["audit"]["path"])  # 审计路径只从配置读取，代码不写死
    path.parent.mkdir(parents=True, exist_ok=True)  # 确保 data/ 目录存在
    with _WRITE_LOCK:  # 与 write_json 共用锁，避免同进程并发写冲突
        with path.open("a", encoding="utf-8") as fh:  # 追加模式，保留全部历史
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")  # 一行一条 JSON


def read_audit(cfg: dict, limit: int = 50) -> list[dict]:  # 审计流水读取
    """读取最近若干条审计记录。
    入参：cfg 配置字典；limit 最多返回条数。
    出参：审计记录列表，按写入顺序返回。
    失败时的行为：文件不存在返回空列表；单行损坏跳过该行，不让整个接口失败。
    """
    path = resolve_path(cfg["audit"]["path"])  # 审计路径只从配置读取
    if not path.exists():  # 尚未产生任何审计记录
        return []  # 返回空列表而非报错
    lines = path.read_text(encoding="utf-8").splitlines()  # 标准库按行读取
    out: list[dict] = []  # 结果容器
    for line in lines[-limit:]:  # 只取最后 limit 行
        try:  # 单行损坏不应影响其余记录
            out.append(json.loads(line))  # 反序列化单条记录
        except Exception:  # 跳过坏行
            continue  # 保持其余记录可用
    return out  # 返回审计记录列表
