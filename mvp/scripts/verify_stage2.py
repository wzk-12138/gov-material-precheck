"""阶段二验证脚本：知识图谱、知识关联、多跳证据链、人工确认写关联、处理链重建。"""  # 模块说明：只用标准库，不依赖 pytest
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import ast  # 标准库：检查每个函数都有中文 docstring
import importlib.util  # 标准库：检查运行必需依赖
import json  # 标准库：请求体与响应体序列化
import os  # 标准库：复制环境变量给子进程
import re  # 标准库：硬编码地址与密钥扫描
import secrets  # 标准库：为验证服务生成一次性随机角色令牌
import subprocess  # 标准库：拉起构建脚本、回归脚本与 uvicorn
import sys  # 标准库：获取当前解释器路径并加入模块搜索路径
import time  # 标准库：轮询服务就绪
import urllib.error  # 标准库：处理非 2xx 响应
import urllib.parse  # 标准库：URL 编码中文查询串
import urllib.request  # 标准库：发 HTTP 请求
from pathlib import Path  # 标准库：定位项目根目录与扫描源码

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接执行脚本也能导入 app

for _mod in ("fastapi", "uvicorn", "yaml"):  # 逐项检查运行必需的库
    if importlib.util.find_spec(_mod) is None:  # 缺任一依赖
        print(f"[FAIL] 缺少依赖 {_mod}，请先执行 pip install -r requirements.txt")  # 给出可操作指引
        raise SystemExit(1)  # 直接退出，避免后续 ImportError 堆栈

from app import graph  # 知识图谱：单元级断言直接调用，不走网络
from app.common import load_config  # 配置加载：端口与图谱路径都从这里取

CFG = load_config()  # 读取 config.yaml
PORT = int(CFG["service"]["port"]) + 2000  # 验证端口避开手动 8000 与阶段一 9000
BASE = f"http://{CFG['service']['host']}:{PORT}"  # 服务基址，只连本机回环
ENV = dict(os.environ)  # 复制当前环境变量
ENV["PYTHONIOENCODING"] = "utf-8"  # 强制子进程用 UTF-8 输出，避免中文日志乱码
TOKENS = {str(r): secrets.token_hex(16) for r in CFG["roles"]}  # 每个角色一次性随机令牌，不写死、不落盘
TOKEN_HEADER = str(CFG["auth"]["header_name"])  # 凭据头名称只从配置取
ENV[str(CFG["auth"]["token_env"])] = json.dumps(TOKENS, ensure_ascii=False)  # 注入子进程环境变量，服务启动时读取
RESULTS: list[tuple[str, bool]] = []  # 用例结果容器：(名称, 是否通过)
STRUCT_OK: list[bool] = []  # 每次响应的统一结构检查结果


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:  # HTTP 调用封装
    """发一个请求并检查统一响应结构。
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
            parsed = json.loads(resp.read().decode("utf-8"))  # 解析响应体
    except urllib.error.HTTPError as exc:  # 非 2xx（服务约定恒 200，此处仅兜底）
        try:  # 尝试解析错误体
            parsed = json.loads(exc.read().decode("utf-8"))  # 返回服务给的结构
        except Exception:  # 错误体不是 JSON
            parsed = {"ok": False, "data": None, "error": {"code": "ERR_TRANSPORT", "msg": str(exc)}}  # 自造统一结构
    ok_shape = set(parsed.keys()) == {"ok", "data", "error"} and set(parsed.get("error", {}).keys()) == {"code", "msg"}  # 三键与 error 双键都必须齐
    STRUCT_OK.append(ok_shape)  # 记账，最后统一断言
    return parsed  # 返回响应体


def check(name: str, passed: bool, detail: str = "") -> dict:  # 断言并记账
    """记录一个用例的通过与否并打印单行结果。
    入参：name 用例名；passed 是否通过；detail 可选补充信息。
    出参：返回一个带 ok 的字典，便于与 HTTP 用例混排。
    失败时的行为：不抛异常，只记为失败，保证全部用例跑完。
    """
    RESULTS.append((name, bool(passed)))  # 记账
    print(f"[{'PASS' if passed else 'FAIL'}] {name}{' -> ' + detail if detail else ''}")  # 打印单行结果
    return {"ok": bool(passed), "data": None, "error": {"code": "OK", "msg": ""}}  # 返回可核对结构


def http_check(name: str, body: dict, expect_ok: bool, expect_code: str = "") -> dict:  # HTTP 用例断言
    """校验一个 HTTP 用例的 ok 与错误码。
    入参：name 用例名；body 响应体；expect_ok 期望成功与否；expect_code 期望错误码，空串表示不校验。
    出参：原样返回 body，便于后续取业务数据。
    失败时的行为：不抛异常，只记为失败并打印。
    """
    actual_ok = bool(body.get("ok"))  # 取实际 ok 值
    actual_code = str(body.get("error", {}).get("code", ""))  # 取实际错误码
    passed = actual_ok == expect_ok and (expect_code == "" or actual_code == expect_code)  # 同时校验 ok 与错误码
    RESULTS.append((name, passed))  # 记账
    print(f"[{'PASS' if passed else 'FAIL'}] {name} -> ok={actual_ok} code={actual_code or '-'}")  # 打印单行结果
    return body  # 原样返回响应体


def run_script(name: str) -> bool:  # 执行一个构建或回归脚本
    """以子进程执行 scripts/ 下的脚本并回传是否成功。
    入参：name 脚本文件名，例如 build_graph.py。
    出参：bool，退出码为 0 且无致命输出时为真。
    失败时的行为：不抛异常，打印 stdout 末段与 stderr 末段并返回 False。
    """
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / name)], cwd=str(ROOT), env=ENV, capture_output=True, text=True, encoding="utf-8")  # 同步执行
    print(f"[INFO] {name} exit={proc.returncode} | {(proc.stdout or '').strip().splitlines()[-1] if (proc.stdout or '').strip() else ''}")  # 打印末行摘要
    if proc.returncode != 0:  # 脚本失败
        print(f"[FAIL] {name} 输出：{(proc.stdout or '')[-500:]} {(proc.stderr or '')[-500:]}")  # 打印错误尾部
    return proc.returncode == 0  # 返回成败


def graph_unit_checks() -> None:  # 图谱单元级断言
    """直接调用图谱模块，验证关联依据、多跳链与权限裁剪。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：任一断言取不到数据时记为失败，不抛异常中断后续 HTTP 用例。
    """
    stat = graph.stats(CFG)  # 读图谱统计
    check("U1 图谱规模可核对", stat["nodes"] >= 50 and stat["edges"] >= 100 and len(stat["relations"]) >= 8, f"{stat['nodes']} 节点 / {stat['edges']} 关联 / {len(stat['relations'])} 类")  # 规模断言
    types = graph.relation_types(CFG)["items"]  # 取关系清单
    rel_names = {x["rel"] for x in types}  # 转成集合
    check("U2 关联语义含归属与内容关系", {"属于需求", "同一需求共现", "内容相关", "援引"}.issubset(rel_names))  # 语义断言
    f10 = graph.relations(CFG, "f10", "", True)  # 取 f10 的全部关联
    quoted = [x for x in f10["links"] if x["rel"] == "援引"]  # 找援引关联
    check("U3 事实之间存在原文援引关联", bool(quoted))  # 援引来自正文直接包含
    scored = [x for x in f10["links"] if x["rel"] == "内容相关" and "Jaccard" in x["note"]]  # 找带可核对依据的内容关联
    check("U4 内容关联带可核对依据", bool(scored))  # 阈值与词元重合度写进 note
    path = graph.shortest_path(CFG, "f10", "f31", True)  # 跨需求最短链
    check("U5 跨需求关联链达到多跳", path["found"] and len(path["hops"]) >= 3, f"{len(path['hops'])} 跳")  # 至少三跳
    sub = graph.subgraph(CFG, "f10", 2, True)  # 两跳子图
    check("U6 子图确实扩散到第二跳", any(n.get("depth", 0) >= 2 for n in sub["nodes"]))  # 深度断言
    hot = graph.relations(CFG, "f10", "", False)  # 非授权岗位的关联视图
    leaked = any("text" in l["peer"]["meta"] for l in hot["links"])  # 检查是否带回原文
    check("U7 非授权岗位看不到关联节点原文", not leaked)  # 权限裁剪断言
    chain = graph.evidence_chain(CFG, ["f10"], 2, True)  # 单起点证据链
    check("U8 证据链按上限收敛且可核对", 0 < len(chain["hops"]) <= int(CFG["graph"]["max_hops"]) and max((h["depth"] for h in chain["hops"]), default=0) >= 2, f"{len(chain['hops'])} 跳")  # 条数与深度断言


def source_rule_checks() -> None:  # 代码规范扫描
    """扫描源码与配置，验证每行注释、中文 docstring 与配置外置三条硬约束。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：文件缺失记为失败，不抛异常。
    """
    pyfiles = sorted(list((ROOT / "app").glob("*.py")) + list((ROOT / "scripts").glob("*.py")))  # 全部源码
    missing_lines: list[str] = []  # 缺注释的代码行
    missing_docs: list[str] = []  # 缺中文 docstring 的函数
    hardcoded: list[str] = []  # 写死的地址或密钥
    for path in pyfiles:  # 逐文件检查
        text = path.read_text(encoding="utf-8")  # 读源码
        lines = text.splitlines()  # 按行切分
        in_doc = False  # docstring 内部标记
        for i, line in enumerate(lines, 1):  # 逐行检查注释
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
    check("U9 每行代码都有注释", not missing_lines, f"缺失 {len(missing_lines)} 行")  # 注释覆盖断言
    check("U10 每个函数都有中文 docstring", not missing_docs, f"缺失 {len(missing_docs)} 个")  # docstring 断言
    check("U11 业务代码不写死地址或密钥", not hardcoded)  # 配置外置断言
    cfg_lines = (ROOT / "config.yaml").read_text(encoding="utf-8").splitlines()  # 读配置文件
    cfg_missing = [i for i, line in enumerate(cfg_lines, 1) if line.strip() and "#" not in line]  # 缺注释行号
    check("U12 config.yaml 每行带注释", not cfg_missing, f"缺失 {len(cfg_missing)} 行")  # 配置注释断言


def main() -> int:  # 验证主流程
    """重建图谱、回归阶段一、跑单元与 HTTP 用例，最后重置演示数据。
    入参：无。
    出参：进程退出码，0 表示全部通过、1 表示有失败或环境未就绪。
    失败时的行为：构建或回归失败时提前返回 1；服务无论成败都在 finally 中关闭。
    """
    bid = "SYN-G3-" + str(int(time.time()))  # 每次运行用独立业务编号，避免审计回放串轮
    bid_pending = bid + "-PEND"  # 未确认单据用另一个业务编号
    check("00 规则库构建", run_script("build_kb.py"))  # 先重建合成规则库
    check("01 知识图谱构建", run_script("build_graph.py"))  # 重建图谱，保证重复跑可复现
    check("02 阶段一 18 用例回归", run_script("verify.py"))  # 不改阶段一脚本，原样回归
    graph_unit_checks()  # 单元级断言
    source_rule_checks()  # 代码规范扫描
    log_path = ROOT / "data" / "verify-stage2-server.log"  # 服务日志落盘
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
        win = {"X-Role": "window", "X-Actor": "lin"}  # 窗口发起人
        rev = {"X-Role": "reviewer", "X-Actor": "tan"}  # 审核接手人
        hot = {"X-Role": "hotline", "X-Actor": "ye"}  # 热线无原文权限
        h = http_check("03 健康检查含图谱统计", call("GET", "/health"), True)  # 健康检查
        check("04 health.graph 已就绪", bool((h.get("data") or {}).get("graph", {}).get("ready")))  # 图谱状态断言
        gs = http_check("05 图谱统计接口", call("GET", "/graph/stats", headers={"X-Role": "supervisor"}), True)  # 统计接口
        check("06 统计含节点与关联数", (gs.get("data") or {}).get("nodes", 0) > 0 and (gs.get("data") or {}).get("edges", 0) > 0)  # 数值断言
        f10q = urllib.parse.quote("f10")  # 编码节点编号
        rel = http_check("07 知识关联查询", call("GET", f"/graph/relations?node_id={f10q}", headers={"X-Role": "supervisor"}), True)  # 关联查询
        check("08 关联列表非空且带语义", len((rel.get("data") or {}).get("links", [])) > 0 and any(x["rel"] in ("内容相关", "援引", "同一需求共现") for x in (rel.get("data") or {}).get("links", [])))  # 语义断言
        path = http_check("09 最短关联链接口", call("GET", "/graph/path?src=f10&dst=f31", headers={"X-Role": "supervisor"}), True)  # 最短链
        check("10 最短链达到多跳", (path.get("data") or {}).get("found") and len((path.get("data") or {}).get("hops", [])) >= 3)  # 多跳断言
        sub = http_check("11 多跳子图接口", call("GET", "/graph/subgraph?node_id=f10&depth=2", headers={"X-Role": "supervisor"}), True)  # 子图
        check("12 子图含第二跳节点", any(n.get("depth", 0) >= 2 for n in (sub.get("data") or {}).get("nodes", [])))  # 深度断言
        types = http_check("13 关系语义清单接口", call("GET", "/graph/relation-types", headers={"X-Role": "supervisor"}), True)  # 关系清单
        check("14 关系类型不少于八类", (types.get("data") or {}).get("total", 0) >= 8)  # 类型数断言
        http_check("15 缺角色查图谱被拒", call("GET", f"/graph/relations?node_id={f10q}"), False, "ROLE_REQUIRED")  # 权限校验
        hotrel = http_check("16 热线角色可查关联结构", call("GET", f"/graph/relations?node_id={f10q}", headers=hot), True)  # 脱敏查询
        check("17 热线关联节点不含原文", not any("text" in x["peer"]["meta"] for x in (hotrel.get("data") or {}).get("links", [])))  # 脱敏断言
        pre = http_check("18 预检返回图谱证据链", call("POST", "/precheck", {"business_id": bid, "question": "线上清单和窗口实际要求不一致时，需要补哪些材料？", "materials": ["申请表"]}, win), True)  # 主链路
        pdata = pre.get("data") or {}  # 取业务数据
        check("19 证据链非空且图谱已就绪", bool(pdata.get("graph_refs")) and bool(pdata.get("graph_chain")) and bool(pdata.get("graph_ready")))  # 证据链断言
        ticket_confirmed = str(pdata.get("ticket", {}).get("ticket_id", ""))  # 取待确认单编号
        pending = http_check("20 另起一张未确认单据", call("POST", "/precheck", {"business_id": bid_pending, "question": "清单版本冲突时如何处理", "materials": []}, win), True)  # 未确认单据
        ticket_pending = str((pending.get("data") or {}).get("ticket", {}).get("ticket_id", ""))  # 取未确认单据编号
        base_link = {"src": "f10", "dst": "f31", "rel": "依赖", "note": "人工核对后补登记：清单差异依赖事项编码字段", "business_id": bid}  # 合法写关联请求体
        http_check("21 匿名写关联被拒", call("POST", "/graph/link", {**base_link, "ticket_id": ticket_confirmed}, {"X-Role": "reviewer"}), False, "ACTOR_REQUIRED")  # 匿名拦截
        http_check("22 未确认单据写关联被拒", call("POST", "/graph/link", {**base_link, "business_id": bid_pending, "ticket_id": ticket_pending}, rev), False, "GRAPH_CONFIRM_REQUIRED")  # 人工确认卡点
        http_check("23 窗口角色写关联被拒", call("POST", "/graph/link", {**base_link, "ticket_id": ticket_pending}, {"X-Role": "window", "X-Actor": "lin"}), False, "ROLE_NOT_ALLOWED")  # 权限卡点
        http_check("24 关系不在白名单被拒", call("POST", "/graph/link", {**base_link, "rel": "自造关系", "ticket_id": ticket_confirmed}, rev), False, "GRAPH_REL_NOT_ALLOWED")  # 语义白名单
        conf = http_check("25 审核岗位接手确认", call("POST", "/confirm", {"ticket_id": ticket_confirmed, "decision": "accept", "note": "已按窗口实际要求复核"}, rev), True)  # 人工确认闭环
        edges_before = int(graph.stats(CFG)["edges"])  # 记下写入前关联数
        linked = http_check("26 已确认单据可登记知识关联", call("POST", "/graph/link", {**base_link, "ticket_id": ticket_confirmed}, rev), True)  # 合法写入
        edges_after = int(graph.stats(CFG)["edges"])  # 重新读盘，验证原子落盘
        check("27 图谱关联数增加且已落盘", edges_after == edges_before + 1 and bool((linked.get("data") or {}).get("stats")), f"{edges_before} -> {edges_after}")  # 落盘断言
        reloaded = graph.relations(CFG, "f10", "依赖", True)  # 重新从文件读，等价于重启回载
        check("28 重启回载后新关联仍在", any(x["peer"]["id"] == "f31" for x in reloaded["links"]))  # 回载断言
        http_check("29 重复登记被拒", call("POST", "/graph/link", {**base_link, "ticket_id": ticket_confirmed}, rev), False, "GRAPH_LINK_EXISTS")  # 三元组去重
        replay = http_check("30 按业务编号重建处理链", call("GET", f"/audit/replay?business_id={bid}", headers={"X-Role": "supervisor"}), True)  # 处理链重建
        actions = (replay.get("data") or {}).get("actions", [])  # 取动作序列
        check("31 处理链含预检确认与关联登记", all(a in actions for a in ("precheck", "confirm", "graph_link")), ",".join(actions))  # 可重建断言
        http_check("32 匿名重建处理链被拒", call("GET", f"/audit/replay?business_id={bid}"), False, "ROLE_REQUIRED")  # 审计权限
        check("33 全部响应结构统一", bool(STRUCT_OK) and all(STRUCT_OK), f"{sum(STRUCT_OK)}/{len(STRUCT_OK)}")  # 统一结构断言
    finally:  # 无论如何都清理服务进程
        proc.terminate()  # 请求优雅退出
        try:  # 等待退出
            proc.wait(timeout=5)  # 最多等 5 秒
        except Exception:  # 未退出
            proc.kill()  # 强制结束
        log.close()  # 关闭日志文件
    run_script("build_graph.py")  # 重置演示写入的关联，保持交付目录只有合成规则源
    passed = sum(1 for _, p in RESULTS if p)  # 统计通过数
    print(f"[RESULT] {passed}/{len(RESULTS)} 用例通过")  # 打印总结果
    return 0 if passed == len(RESULTS) else 1  # 全通过才返回 0


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
