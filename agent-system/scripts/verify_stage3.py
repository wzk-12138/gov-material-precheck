"""阶段三验证脚本：认证卡点、本机 Ollama 状态、两阶段回归与代码规范全量收口。"""  # 模块说明：只用标准库，不依赖 pytest
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import ast  # 标准库：检查每个函数都有中文 docstring
import importlib.util  # 标准库：检查运行必需依赖
import json  # 标准库：请求体、响应体与凭据表序列化
import os  # 标准库：复制环境变量给子进程并注入一次性凭据
import re  # 标准库：硬编码地址与密钥扫描
import secrets  # 标准库：生成一次性随机角色令牌
import subprocess  # 标准库：拉起回归脚本与 uvicorn
import sys  # 标准库：获取当前解释器路径并加入模块搜索路径
import time  # 标准库：轮询服务就绪
import urllib.error  # 标准库：处理非 2xx 响应
import urllib.request  # 标准库：发 HTTP 请求
from pathlib import Path  # 标准库：定位项目根目录与扫描源码

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接执行脚本也能导入 app

for _mod in ("fastapi", "uvicorn", "yaml"):  # 逐项检查运行必需的库
    if importlib.util.find_spec(_mod) is None:  # 缺任一依赖
        print(f"[FAIL] 缺少依赖 {_mod}，请先执行 pip install -r requirements.txt")  # 给出可操作指引
        raise SystemExit(1)  # 直接退出，避免后续 ImportError 堆栈

from app import auth  # 认证卡点
from app.common import BizError, load_config  # 统一业务异常与唯一配置入口
sys.path.insert(0, str(ROOT / "scripts"))  # 加入脚本目录，复用本机 Ollama 自检模块
import check_ollama as ollama_probe  # 本机 Ollama 自检：只调用 probe，不执行其 main

CFG = load_config()  # 读取 config.yaml
PORT = int(CFG["service"]["port"]) + 3000  # 验证端口避开手动 8000、阶段一 9000、阶段二 10000
BASE = f"http://{CFG['service']['host']}:{PORT}"  # 服务基址，只连本机回环
TOKEN_HEADER = str(CFG["auth"]["header_name"])  # 凭据头名称只从配置取
TOKEN_ENV = str(CFG["auth"]["token_env"])  # 凭据环境变量名只从配置取
TOKENS = {str(r): secrets.token_hex(16) for r in CFG["roles"]}  # 每个角色一次性随机令牌，不写死、不落盘
ENV = dict(os.environ)  # 复制当前环境变量
ENV["PYTHONIOENCODING"] = "utf-8"  # 强制子进程用 UTF-8 输出，避免中文日志乱码
ENV[TOKEN_ENV] = json.dumps(TOKENS, ensure_ascii=False)  # 注入子进程环境变量，服务启动时读取
RESULTS: list[tuple[str, bool]] = []  # 用例结果容器：(名称, 是否通过)
STRUCT_OK: list[bool] = []  # 每次响应的统一结构检查结果


def check(name: str, passed: bool, detail: str = "") -> None:  # 断言并记账
    """记录一个用例的通过与否并打印单行结果。
    入参：name 用例名；passed 是否通过；detail 可选补充信息。
    出参：无。
    失败时的行为：不抛异常，只记为失败，保证全部用例跑完。
    """
    RESULTS.append((name, bool(passed)))  # 记账
    print(f"[{'PASS' if passed else 'FAIL'}] {name}{' -> ' + detail if detail else ''}")  # 打印单行结果


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None, with_token: bool = True) -> dict:  # HTTP 调用封装
    """发一个请求并检查统一响应结构。
    入参：method HTTP 方法；path 接口路径；body 可选 JSON 请求体；headers 可选请求头；with_token 是否自动按 X-Role 注入凭据。
    出参：解析后的响应体 dict。
    失败时的行为：连接失败原样抛异常；非 2xx 时尽力解析错误体，解析不了返回带 ERR_TRANSPORT 的统一结构。
    """
    merged = {"Content-Type": "application/json"}  # 默认请求头
    merged.update(headers or {})  # 合并调用方自定义头（X-Role / X-Actor）
    if with_token and TOKEN_HEADER not in merged:  # 需要认证且调用方未自定义凭据
        role_key = str(merged.get("X-Role", ""))  # 取本次请求的角色键
        if role_key in TOKENS:  # 已登记角色才自动带令牌
            merged[TOKEN_HEADER] = TOKENS[role_key]  # 缺角色或未登记角色用例保持无凭据
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None  # 序列化请求体
    req = urllib.request.Request(BASE + path, data=data, headers=merged, method=method)  # 构造请求
    try:  # 正常响应
        with urllib.request.urlopen(req, timeout=15) as resp:  # 发起请求
            parsed = json.loads(resp.read().decode("utf-8"))  # 解析响应体
    except urllib.error.HTTPError as exc:  # 非 2xx（服务约定恒 200，此处仅兜底）
        try:  # 尝试解析错误体
            parsed = json.loads(exc.read().decode("utf-8"))  # 返回服务给的结构
        except Exception:  # 错误体不是 JSON
            parsed = {"ok": False, "data": None, "error": {"code": "ERR_TRANSPORT", "msg": str(exc)}}  # 自造统一结构
    ok_shape = set(parsed.keys()) == {"ok", "data", "error"} and set(parsed.get("error", {}).keys()) == {"code", "msg"}  # 三键与 error 双键都必须齐
    STRUCT_OK.append(ok_shape)  # 记账，最后统一断言
    return parsed  # 返回响应体


def http_check(name: str, body: dict, expect_ok: bool, expect_code: str = "") -> dict:  # HTTP 用例断言
    """校验一个 HTTP 用例的 ok 与错误码。
    入参：name 用例名；body 响应体；expect_ok 期望成功与否；expect_code 期望错误码，空串表示不校验。
    出参：原样返回 body，便于后续取业务数据。
    失败时的行为：不抛异常，只记为失败并打印。
    """
    actual_ok = bool(body.get("ok"))  # 取实际 ok 值
    actual_code = str(body.get("error", {}).get("code", ""))  # 取实际错误码
    passed = actual_ok == expect_ok and (expect_code == "" or actual_code == expect_code)  # 同时校验 ok 与错误码
    check(name, passed, f"ok={actual_ok} code={actual_code or '-'}")  # 记账并打印
    return body  # 原样返回响应体


def unit_checks() -> None:  # 认证单元级断言
    """直接调用认证模块，覆盖缺角色、缺凭据、错误凭据、冒用、异常令牌与凭据表缺陷十种情形。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：不抛异常中断；任何 unexpected 异常都记为该用例失败，最后恢复环境变量。
    """
    cfg = dict(CFG)  # 浅拷贝配置，避免污染模块级配置
    cfg["auth"] = dict(CFG["auth"])  # 单独拷贝认证节，便于切换开关
    os.environ[TOKEN_ENV] = json.dumps(TOKENS, ensure_ascii=False)  # 注入本次运行的凭据表
    def code_of(role: str | None, token: str | None, conf: dict | None = None) -> str:  # 取认证结果码
        """调用认证卡点并把结果折算成错误码字符串。
        入参：role 角色键；token 凭据；conf 可选配置副本，为空用本函数外层的 cfg。
        出参：通过返回 "OK"，失败返回 BizError.code，未预期异常返回 "EXCEPTION:<类型>"。
        失败时的行为：不向外抛异常，便于逐条跑完全部用例。
        """
        try:  # 捕获业务异常
            auth.verify_request(conf or cfg, role, token)  # 执行认证卡点
            return "OK"  # 通过
        except BizError as exc:  # 预期内的拦截
            return str(exc.code)  # 返回错误码
        except Exception as exc:  # 未预期的崩溃（例如非 ASCII 比较类型错误）
            return "EXCEPTION:" + type(exc).__name__  # 记为异常，不算通过
    check("U01 缺角色被拦", code_of(None, TOKENS["window"]) == "ROLE_REQUIRED")  # 角色头优先于凭据
    check("U02 未登记角色被拦", code_of("guest", TOKENS["window"]) == "ROLE_UNKNOWN")  # 白名单
    check("U03 缺凭据被拦", code_of("window", None) == "AUTH_REQUIRED")  # 明文角色不得单独调用
    check("U04 错误凭据被拦", code_of("window", "bad-token") == "AUTH_FAILED")  # 无效令牌
    check("U05 冒用他人令牌被拦", code_of("supervisor", TOKENS["window"]) == "AUTH_ROLE_MISMATCH")  # 不得借用账号
    check("U06 合法凭据放行", code_of("reviewer", TOKENS["reviewer"]) == "OK")  # 正常调用
    check("U07 非 ASCII 凭据不崩溃", code_of("window", "错误令牌中文") == "AUTH_FAILED")  # UTF-8 字节比较健壮性
    check("U08 显式关闭认证返回 disabled", auth.verify_request({**cfg, "auth": {**cfg["auth"], "required": False}}, "window", "") == "disabled")  # 调试开关
    os.environ[TOKEN_ENV] = json.dumps({"window": ""}, ensure_ascii=False)  # 临时改成含空令牌的凭据表
    check("U09 空令牌凭据表被拒", code_of("window", "abc") == "AUTH_BAD_TOKENS")  # 空令牌等于未签发
    os.environ[TOKEN_ENV] = json.dumps({"window": "t", "reviewer": "t"}, ensure_ascii=False)  # 临时改成重复令牌
    check("U10 角色共用令牌被拒", code_of("window", "t") == "AUTH_TOKEN_DUPLICATED")  # 责任链歧义
    os.environ[TOKEN_ENV] = json.dumps(TOKENS, ensure_ascii=False)  # 恢复合法凭据表
    os.environ.pop(TOKEN_ENV, None)  # 临时移除环境变量
    check("U11 未注入凭据表被拒", code_of("window", "abc") == "AUTH_NOT_CONFIGURED")  # 不回退默认凭据
    os.environ[TOKEN_ENV] = json.dumps(TOKENS, ensure_ascii=False)  # 最终恢复，供后续 HTTP 用例使用


def source_rule_checks() -> None:  # 代码规范扫描
    """扫描全部源码与配置，验证每行注释、中文 docstring、配置外置与三道关卡注释四条硬约束。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：文件缺失或语法错误记为失败，不抛异常中断后续用例。
    """
    pyfiles = sorted(list((ROOT / "app").glob("*.py")) + list((ROOT / "scripts").glob("*.py")))  # 全部源码
    missing_lines: list[str] = []  # 缺注释的代码行
    missing_docs: list[str] = []  # 缺中文 docstring 的函数
    hardcoded: list[str] = []  # 写死的地址或密钥
    for path in pyfiles:  # 逐文件检查
        text = path.read_text(encoding="utf-8")  # 读源码
        in_doc = False  # docstring 内部标记
        for i, line in enumerate(text.splitlines(), 1):  # 逐行检查注释
            stripped = line.strip()  # 去首尾空白
            if not stripped or stripped.startswith("#"):  # 空行或纯注释行
                continue  # 不算代码行
            if in_doc:  # 处于多行 docstring 内
                if '"""' in stripped:  # 到结束标记
                    in_doc = False  # 退出 docstring
                continue  # docstring 内容不要求行尾注释
            if stripped.startswith('"""') and stripped.count('"""') == 1:  # 多行 docstring 开始
                in_doc = True  # 进入 docstring
                continue  # 跳过
            if "#" not in line:  # 代码行缺注释
                missing_lines.append(f"{path.name}:{i}")  # 记录位置
        tree = ast.parse(text)  # 解析语法树
        for node in ast.walk(tree):  # 遍历全部函数节点
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):  # 只查函数
                doc = ast.get_docstring(node) or ""  # 取 docstring
                if not any("\u4e00" <= ch <= "\u9fff" for ch in doc):  # 没有中文
                    missing_docs.append(f"{path.name}:{node.name}")  # 记录函数
        for hit in re.findall(r"https?://[^\s\"']+", text):  # 扫描地址字面量
            if path.parent.name == "app":  # 业务代码内不允许写死地址
                hardcoded.append(f"{path.name}:{hit[:40]}")  # 记录命中
        if path.parent.name == "app" and "sk-" in text:  # 业务代码内不允许写死密钥
            hardcoded.append(f"{path.name}:sk-")  # 记录命中
    check("U12 每行代码都有注释", not missing_lines, f"缺失 {len(missing_lines)} 行")  # 注释覆盖断言
    check("U13 每个函数都有中文 docstring", not missing_docs, f"缺失 {len(missing_docs)} 个")  # docstring 断言
    check("U14 业务代码不写死地址或密钥", not hardcoded)  # 配置外置断言
    guards_text = (ROOT / "app" / "guards.py").read_text(encoding="utf-8")  # 三道关卡源码
    auth_text = (ROOT / "app" / "auth.py").read_text(encoding="utf-8")  # 认证卡点源码
    confirm_text = (ROOT / "app" / "confirmation.py").read_text(encoding="utf-8")  # 人工确认源码
    check("U15 安全审查/权限校验/人工确认/认证均写明为何卡", all(t.count("为什么这么卡") >= 2 for t in (guards_text, auth_text, confirm_text)))  # 中文行内注释断言
    cfg_missing = [i for i, line in enumerate((ROOT / "config.yaml").read_text(encoding="utf-8").splitlines(), 1) if line.strip() and "#" not in line]  # 配置缺注释行号
    check("U16 config.yaml 每行带注释", not cfg_missing, f"缺失 {len(cfg_missing)} 行")  # 配置注释断言


def run_script(name: str, tolerant: bool = False) -> bool:  # 执行构建、自检或回归脚本
    """以子进程执行 scripts/ 下的脚本并回传是否成功。
    入参：name 脚本文件名；tolerant 为真时非 0 退出码只提示、不算失败（用于本机 Ollama 自检）。
    出参：bool，退出码为 0 或容错放行时为真。
    失败时的行为：不抛异常，打印 stdout 末段与 stderr 末段并返回 False。
    """
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / name)], cwd=str(ROOT), env=ENV, capture_output=True, text=True, encoding="utf-8")  # 同步执行
    tail = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""  # 取输出末行
    print(f"[INFO] {name} exit={proc.returncode} | {tail}")  # 打印摘要
    if proc.returncode != 0:  # 脚本失败
        print(f"[INFO] {name} 输出尾部：{(proc.stdout or '')[-400:]} {(proc.stderr or '')[-300:]}")  # 打印错误尾部
        if tolerant:  # 容错脚本（本机模型未安装属环境问题）
            return True  # 只提示，不判失败
    return proc.returncode == 0 or tolerant  # 返回成败


def ollama_checks(pre_data: dict) -> None:  # 本机模型状态与预检结果一致性断言
    """根据 Ollama 在线状态断言预检是走真实推理还是按配置降级。
    入参：pre_data 一次预检响应的 data 字典。
    出参：无，结果记入 RESULTS。
    失败时的行为：不抛异常；模型未就绪但预检未降级时记为失败并打印原因。
    """
    state = ollama_probe.probe(CFG)  # 复用 scripts/check_ollama.py 的探测函数
    model = dict(pre_data.get("model") or {})  # 取预检里的模型状态
    degraded = bool(model.get("degraded"))  # 是否降级
    print(f"[OLLAMA] online={state['online']} matched={state['matched']} reason={state['reason']}")  # 打印本机状态
    if state["online"] and state["matched"]:  # 本机服务与模型都就绪
        suggestion = str(pre_data.get("suggestion", ""))  # 取模型或兜底产出的建议文本
        real = not degraded and model.get("name") == CFG["ollama"]["model"] and bool(suggestion.strip())  # 真实推理必须回配置模型名与非空建议
        check("H10 真实推理未降级且产出建议", real, f"model={model.get('name')} text={len(suggestion)}字")  # 应走完整推理链路
        print("[OLLAMA] 本轮已覆盖真实模型推理路径")  # 明确记录
    else:  # 服务未启动或模型未拉取
        check("H10 模型未就绪时按配置降级", degraded and bool(model.get("reason")), str(model.get("reason", "")))  # 必须降级且写明原因
        print("[OLLAMA] 本轮未跑真实模型推理；部署 Ollama 后重跑 scripts/verify_all.py 即自动覆盖")  # 明确边界


def main() -> int:  # 验证主流程
    """跑单元断言、代码规范、两阶段回归、HTTP 认证用例与真实启动，最后汇总。
    入参：无。
    出参：进程退出码，0 表示全部通过、1 表示有失败或环境未就绪。
    失败时的行为：回归脚本失败或服务 15 秒内未就绪时提前返回 1；无论成败都在 finally 中关闭服务进程。
    """
    unit_checks()  # 认证单元级断言
    source_rule_checks()  # 代码规范扫描
    run_script("check_ollama.py", True)  # 本机模型自检只打印状态，不因其未安装而判失败
    check("U17 阶段一 18 用例回归", run_script("verify.py"))  # 原样回归阶段一
    check("U18 阶段二 46 用例回归", run_script("verify_stage2.py"))  # 原样回归阶段二
    log_path = ROOT / "data" / "verify-stage3-server.log"  # 服务日志落盘
    log_path.parent.mkdir(parents=True, exist_ok=True)  # 确保 data/ 存在
    log = log_path.open("w", encoding="utf-8")  # 打开日志
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", str(CFG["service"]["host"]), "--port", str(PORT), "--log-level", "warning"], cwd=str(ROOT), env=ENV, stdout=log, stderr=log)  # 拉起本机服务
    try:  # 保证任何情况下都收掉子进程
        ready = False  # 就绪标记
        for _ in range(60):  # 最多等 15 秒
            try:  # 探测健康检查
                with urllib.request.urlopen(BASE + "/health", timeout=1) as resp:  # 请求 /health
                    if resp.status == 200:  # 已就绪
                        ready = True  # 标记成功
                        break  # 退出轮询
            except Exception:  # 尚未监听
                time.sleep(0.25)  # 稍等再试
        if not ready:  # 超时未就绪
            print(f"[FAIL] 服务未在 15 秒内就绪，日志：{log_path.read_text(encoding='utf-8')[-800:]}")  # 打印日志尾部
            return 1  # 提前退出
        h = http_check("H01 健康检查免凭据", call("GET", "/health"), True)  # 探活不需要令牌
        auth_info = dict((h.get("data") or {}).get("auth") or {})  # 取认证状态位
        check("H02 health 回认证状态且不泄露令牌", bool(auth_info.get("required")) and auth_info.get("header") == TOKEN_HEADER and all(t not in json.dumps(h, ensure_ascii=False) for t in TOKENS.values()))  # 只回开关与变量名
        http_check("H03 无凭据业务请求被拒", call("GET", "/graph/stats", headers={"X-Role": "supervisor"}, with_token=False), False, "AUTH_REQUIRED")  # 明文角色不得单独调用
        http_check("H04 错误凭据被拒", call("GET", "/graph/stats", headers={"X-Role": "supervisor", TOKEN_HEADER: "bad-token"}), False, "AUTH_FAILED")  # 无效令牌
        http_check("H05 冒用他人令牌被拒", call("GET", "/graph/stats", headers={"X-Role": "supervisor", TOKEN_HEADER: TOKENS["window"]}), False, "AUTH_ROLE_MISMATCH")  # 不得借用账号
        http_check("H06 合法凭据可查图谱", call("GET", "/graph/stats", headers={"X-Role": "supervisor"}), True)  # 正常调用
        http_check("H07 认证通过后业务关卡仍生效", call("POST", "/precheck", {"business_id": "SYN-A1", "question": "清单版本冲突时如何处理", "materials": []}, {"X-Role": "window"}), False, "ACTOR_REQUIRED")  # 业务校验没有被认证绕过
        win = {"X-Role": "window", "X-Actor": "lin"}  # 完整身份头
        pre = http_check("H08 合法预检仍挂人工确认", call("POST", "/precheck", {"business_id": "SYN-A2", "question": "线上清单和窗口实际要求不一致时，需要补哪些材料？", "materials": ["申请表"]}, win), True)  # 主链路
        pdata = dict(pre.get("data") or {})  # 取业务数据
        check("H09 预检证据链与人工确认门仍在", bool(pdata.get("graph_chain")) and bool(pdata.get("requires_human_confirmation")))  # 前两阶段能力未回退
        ollama_checks(pdata)  # 本机模型在线则验真实推理，离线则验降级
        check("H11 全部响应结构统一", bool(STRUCT_OK) and all(STRUCT_OK), f"{sum(STRUCT_OK)}/{len(STRUCT_OK)}")  # 统一结构断言
    finally:  # 无论如何都清理服务进程
        proc.terminate()  # 请求优雅退出
        try:  # 等待退出
            proc.wait(timeout=5)  # 最多等 5 秒
        except Exception:  # 未退出
            proc.kill()  # 强制结束
        log.close()  # 关闭日志文件
    passed = sum(1 for _, p in RESULTS if p)  # 统计通过数
    print(f"[RESULT] {passed}/{len(RESULTS)} 用例通过")  # 打印总结果
    return 0 if passed == len(RESULTS) else 1  # 全通过才返回 0


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
