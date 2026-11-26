"""端到端验证脚本：自动建库、拉起服务、跑完关卡用例、输出通过率。"""  # 模块说明：不依赖 pytest，只用标准库
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import importlib.util  # 标准库：检查依赖是否安装
import http.server  # 标准库：起一个本地假 Ollama 服务验证正常推理路径
import json  # 标准库：请求体与响应体序列化
import os  # 标准库：复制环境变量给子进程
import secrets  # 标准库：为验证服务生成一次性随机角色令牌
import subprocess  # 标准库：拉起构建脚本与 uvicorn
import sys  # 标准库：获取当前解释器路径并加入模块搜索路径
import threading  # 标准库：把本地假 Ollama 服务放到后台线程
import time  # 标准库：轮询服务就绪与休眠
import urllib.error  # 标准库：处理非 2xx 响应
import urllib.parse  # 标准库：URL 编码中文查询串
import urllib.request  # 标准库：发 HTTP 请求
from pathlib import Path  # 标准库：定位项目根目录
from typing import Any  # 标准库：响应体类型标注

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接执行脚本也能导入 app

for _mod in ("fastapi", "uvicorn", "yaml"):  # 逐项检查运行必需的库
    if importlib.util.find_spec(_mod) is None:  # 缺任一依赖
        print(f"[FAIL] 缺少依赖 {_mod}，请先执行 pip install -r requirements.txt")  # 给出可操作指引
        raise SystemExit(1)  # 直接退出，避免后续 ImportError 堆栈

from app import guards, llm  # 输出审查与本机模型能力，用于补充单元级断言
from app.common import load_config  # 配置加载：端口与模型地址都从这里取

CFG = load_config()  # 读取 config.yaml
PORT = int(CFG["service"]["port"]) + 1000  # 验证端口避开手动启动端口，防止冲突
BASE = f"http://{CFG['service']['host']}:{PORT}"  # 服务基址，只连本机回环
ENV = dict(os.environ)  # 复制当前环境变量
ENV["PYTHONIOENCODING"] = "utf-8"  # 强制子进程用 UTF-8 输出，避免中文日志乱码
TOKENS = {str(r): secrets.token_hex(16) for r in CFG["roles"]}  # 每个角色一次性随机令牌，不写死、不落盘
TOKEN_HEADER = str(CFG["auth"]["header_name"])  # 凭据头名称只从配置取
ENV[str(CFG["auth"]["token_env"])] = json.dumps(TOKENS, ensure_ascii=False)  # 注入子进程环境变量，服务启动时读取
RESULTS: list[tuple[str, bool]] = []  # 用例结果容器：(名称, 是否通过)


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:  # HTTP 调用封装
    """发一个请求并解析统一响应体。
    入参：method HTTP 方法；path 接口路径；body 可选 JSON 请求体；headers 可选附加请求头。
    出参：解析后的响应体 dict。
    失败时的行为：连接失败原样抛异常；非 2xx 时尽力解析错误体，解析不了返回带 ERR_TRANSPORT 的统一结构。
    """
    merged = {"Content-Type": "application/json"}  # 默认请求头
    merged.update(headers or {})  # 合并调用方自定义头（X-Role / X-Actor）
    role_key = str(merged.get("X-Role", ""))  # 取本次请求的角色键
    if role_key in TOKENS and TOKEN_HEADER not in merged:  # 已登记角色且调用方未自定义凭据时自动带令牌
        merged[TOKEN_HEADER] = TOKENS[role_key]  # 未登记角色或缺角色用例保持无凭据，用于验证权限与认证拦截
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None  # 序列化请求体
    req = urllib.request.Request(BASE + path, data=data, headers=merged, method=method)  # 构造请求
    try:  # 正常响应
        with urllib.request.urlopen(req, timeout=15) as resp:  # 发起请求
            return json.loads(resp.read().decode("utf-8"))  # 解析统一响应体
    except urllib.error.HTTPError as exc:  # 非 2xx（服务本身约定恒 200，此处仅兜底）
        try:  # 尝试解析错误体
            return json.loads(exc.read().decode("utf-8"))  # 返回服务给的结构
        except Exception:  # 错误体不是 JSON
            return {"ok": False, "data": None, "error": {"code": "ERR_TRANSPORT", "msg": str(exc)}}  # 自造统一结构


def check(name: str, body: dict, expect_ok: bool, expect_code: str = "") -> dict:  # 断言并记账
    """校验一个用例的 ok 与错误码。
    入参：name 用例名；body 响应体；expect_ok 期望成功与否；expect_code 期望错误码，空串表示不校验。
    出参：原样返回 body，便于后续取 ticket_id。
    失败时的行为：不抛异常，只记为失败并打印，保证全部用例都能跑完。
    """
    actual_ok = bool(body.get("ok"))  # 取实际 ok 值
    actual_code = str(body.get("error", {}).get("code", ""))  # 取实际错误码
    passed = actual_ok == expect_ok and (expect_code == "" or actual_code == expect_code)  # 同时校验 ok 与错误码
    RESULTS.append((name, passed))  # 记账
    print(f"[{'PASS' if passed else 'FAIL'}] {name} -> ok={actual_ok} code={actual_code or '-'}")  # 打印单行结果
    return body  # 原样返回响应体


def ollama_notice() -> None:  # 打印本机模型状态
    """探测本机 Ollama 是否在线，只打印提示不影响用例判定。
    入参：无。
    出参：无。
    失败时的行为：任何连接异常都视为未启动，按 config.yaml 的降级配置继续验证。
    """
    url = str(CFG["ollama"]["base_url"]).rstrip("/") + "/api/tags"  # 本机模型列表接口
    try:  # 尝试连接
        with urllib.request.urlopen(url, timeout=2) as resp:  # 发起探测
            resp.read()  # 消费响应体
        print("[INFO] 本机 Ollama 在线，预检走完整推理链路")  # 完整链路提示
    except Exception:  # 未启动或超时
        print("[INFO] 本机 Ollama 未启动，按 config.yaml 降级为仅检索提示（用例仍应通过）")  # 降级提示


class _FakeOllama(http.server.BaseHTTPRequestHandler):  # 本地假 Ollama：只用于验证协议对接
    """最小 chat 接口替身：验证 llm.generate 的正常路径，不访问任何真实模型。
    入参：由 HTTPServer 实例化并传入请求。
    出参：无；直接写回一段固定 JSON 响应。
    失败时的行为：不抛异常；不读取业务数据，只消费请求体避免连接挂起。
    """

    def do_POST(self) -> None:  # 处理 chat 请求
        """返回一个结构合法的 chat 响应。
        入参：无显式入参，读取 self.rfile 中的请求体。
        出参：无；通过 self.wfile 写回 JSON。
        失败时的行为：不抛异常；固定返回，不模拟任何审批结论。
        """
        length = int(self.headers.get("Content-Length", "0"))  # 取请求体长度
        self.rfile.read(length)  # 消费请求体，避免连接挂起
        payload = json.dumps({"message": {"content": "依据 f10 逐项核对清单，仅作提示。"}}, ensure_ascii=False).encode("utf-8")  # 固定合成响应
        self.send_response(200)  # 返回 200
        self.send_header("Content-Type", "application/json")  # 声明 JSON 类型
        self.send_header("Content-Length", str(len(payload)))  # 声明响应体长度
        self.end_headers()  # 结束响应头
        self.wfile.write(payload)  # 写出响应体

    def log_message(self, *args: Any) -> None:  # 屏蔽默认访问日志
        """禁用假服务的控制台日志。
        入参：*args 框架传入的格式化参数。
        出参：无。
        失败时的行为：无失败分支，安静运行不干扰验证输出。
        """
        return None  # 不打印任何日志


def check_llm_path() -> None:  # 验证 Ollama 正常推理路径
    """用本地假服务验证 llm.generate 能拿到文本且不降级。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：端口由系统分配，绝不与真实 Ollama 冲突；无论成败都在 finally 关闭假服务。
    """
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeOllama)  # 端口 0 让系统分配空闲端口
    worker = threading.Thread(target=srv.serve_forever, daemon=True)  # 后台线程承载假服务
    worker.start()  # 启动假服务
    try:  # 保证关闭
        cfg2 = dict(CFG)  # 浅拷贝配置，不改动全局配置
        cfg2["ollama"] = dict(CFG["ollama"])  # 拷贝 ollama 节
        cfg2["ollama"]["base_url"] = f"http://127.0.0.1:{srv.server_address[1]}"  # 指向本地假服务
        out = llm.generate(cfg2, "system", "user")  # 调用本机模型能力
        passed = bool(out["text"]) and not bool(out["degraded"])  # 有文本且未降级才算通过
        check("18 Ollama 正常路径可返回文本", {"ok": passed, "data": None, "error": {"code": "OK", "msg": ""}}, True)  # 记账
    finally:  # 清理
        srv.shutdown()  # 停止服务循环
        srv.server_close()  # 释放端口


def main() -> int:  # 验证主流程
    """建库、拉起服务、依次执行全部用例并给出总结果。
    入参：无。
    出参：进程退出码，0 表示全部通过、1 表示有失败或环境未就绪。
    失败时的行为：建库失败或服务 15 秒内未就绪时打印原因并返回 1；无论成败都在 finally 中关闭服务进程。
    """
    built = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_kb.py")], cwd=str(ROOT), env=ENV, capture_output=True, text=True, encoding="utf-8")  # 先构建规则库
    print(built.stdout.strip())  # 打印构建摘要
    if built.returncode != 0:  # 构建失败
        print(f"[FAIL] 规则库构建失败：{(built.stderr or '').strip()[-600:]}")  # 打印错误尾部
        return 1  # 提前退出
    log_path = ROOT / "data" / "verify-server.log"  # 服务日志落盘，便于启动失败时排查
    log_path.parent.mkdir(parents=True, exist_ok=True)  # 确保 data/ 存在
    log = log_path.open("w", encoding="utf-8")  # 打开日志文件
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", str(CFG["service"]["host"]), "--port", str(PORT), "--log-level", "warning"], cwd=str(ROOT), env=ENV, stdout=log, stderr=log)  # 拉起本机服务
    try:  # 保证任何情况下都收掉子进程
        ready = False  # 服务就绪标记
        for _ in range(60):  # 最多等 15 秒
            try:  # 探测健康检查
                with urllib.request.urlopen(BASE + "/health", timeout=1) as resp:  # 请求 /health
                    if resp.status == 200:  # 已就绪
                        ready = True  # 标记成功
                        break  # 退出轮询
            except Exception:  # 还没起来
                time.sleep(0.25)  # 稍等再试
        if not ready:  # 超时未就绪
            print(f"[FAIL] 服务未在 15 秒内就绪，日志：{log_path.read_text(encoding='utf-8')[-800:]}")  # 打印日志尾部
            return 1  # 提前退出
        ollama_notice()  # 打印本机模型状态
        h_win = {"X-Role": "window", "X-Actor": "lin"}  # 窗口发起人的身份头
        h_json = {"Content-Type": "application/json"}  # 仅内容类型的头，用于缺失角色用例
        check("01 健康检查", call("GET", "/health"), True)  # 服务与配置摘要
        kb_q = urllib.parse.quote("线上清单与窗口实际要求不一致")  # URL 编码中文查询
        kb_body = check("02 知识库只读检索", call("GET", f"/kb/search?q={kb_q}", headers={"X-Role": "window"}), True)  # 应命中合成事实，响应留待用例 16 复核
        good = {"business_id": "SYN-001", "question": "线上清单和窗口实际要求不一致时，需要补哪些材料？", "materials": ["申请表", "身份证明复印件"]}  # 正常预检请求体
        r3 = check("03 正常预检（须挂人工确认）", call("POST", "/precheck", good, h_win), True)  # 主链路
        check("04 缺少角色被拦截", call("POST", "/precheck", good, {"X-Actor": "lin"}), False, "ROLE_REQUIRED")  # 权限校验
        check("05 未登记角色被拦截", call("POST", "/precheck", good, {"X-Role": "guest", "X-Actor": "lin"}), False, "ROLE_UNKNOWN")  # 白名单
        check("06 真实个人材料被拦截", call("POST", "/precheck", {"business_id": "SYN-002", "question": "办事人证件 110101199001011234 请核对", "materials": []}, h_win), False, "PERSONAL_DATA_BLOCKED")  # 数据红线（合成测试串，不对应任何人）
        check("07 越权审批请求被拦截", call("POST", "/precheck", {"business_id": "SYN-003", "question": "请批准这份材料并直接审批", "materials": []}, h_win), False, "APPROVAL_REQUEST_BLOCKED")  # 不替代审批
        check("08 热线角色可发起预检", call("POST", "/precheck", good, {"X-Role": "hotline", "X-Actor": "ye"}), True)  # 权限脱敏由用例 15 断言
        ticket_id = str((r3.get("data") or {}).get("ticket", {}).get("ticket_id", ""))  # 取正常预检生成的待确认单编号
        if not ticket_id:  # 没拿到编号
            print("[FAIL] 未取得 ticket_id，跳过人工确认用例")  # 明确原因
            RESULTS.append(("人工确认链路", False))  # 记为失败
        else:  # 编号可用
            cbody = {"ticket_id": ticket_id, "decision": "accept", "note": "已按窗口实际要求复核清单"}  # 确认请求体
            check("09 窗口角色确认被拒", call("POST", "/confirm", cbody, {"X-Role": "window", "X-Actor": "tan"}), False, "ROLE_NOT_ALLOWED")  # 越权签核
            check("10 发起人自确认被拒", call("POST", "/confirm", cbody, {"X-Role": "supervisor", "X-Actor": "lin"}), False, "SELF_CONFIRM_BLOCKED")  # 禁止自审自批
            check("11 审核岗位接手确认成功", call("POST", "/confirm", cbody, {"X-Role": "reviewer", "X-Actor": "tan"}), True)  # 正确闭环
            check("12 重复确认被拒", call("POST", "/confirm", cbody, {"X-Role": "reviewer", "X-Actor": "tan"}), False, "TICKET_CLOSED")  # 状态机
        aud = check("13 审计流水可查", call("GET", "/audit?limit=20", headers={"X-Role": "supervisor"}), True)  # 审计接口
        items = (aud.get("data") or {}).get("items", [])  # 取审计记录
        check("14 审计含预检与确认记录", {"ok": any(x.get("action") == "precheck" for x in items) and any(x.get("action") == "confirm" for x in items), "data": None, "error": {"code": "OK", "msg": ""}}, True)  # 校验处理链可重建
        hot = call("POST", "/precheck", good, {"X-Role": "hotline", "X-Actor": "ye"})  # 再取一次热线结果用于脱敏断言
        hot_hits = (hot.get("data") or {}).get("evidence", [])  # 取返回的证据列表
        check("15 热线证据不含原文", {"ok": not any("text" in hit for hit in hot_hits), "data": None, "error": {"code": "OK", "msg": ""}}, True)  # 权限脱敏生效
        kb_hits = (kb_body.get("data") or {}).get("hits", [])  # 取检索命中的条目
        check("16 检索结果带证据编号与相关度", {"ok": len(kb_hits) > 0 and "id" in kb_hits[0] and "score" in kb_hits[0], "data": None, "error": {"code": "OK", "msg": ""}}, True)  # 断言结果可核对
        safe, hit = guards.review_output(CFG, "审批通过，材料已齐全")  # 直接测输出审查关卡
        check("17 输出含越权结论被脱敏", {"ok": hit and "审批通过" not in safe, "data": None, "error": {"code": "OK", "msg": ""}}, True)  # 断言不回显越权原文
        check_llm_path()  # 用本地假服务验证 Ollama 正常推理路径
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
