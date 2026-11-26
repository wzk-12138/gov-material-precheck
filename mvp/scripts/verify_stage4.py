"""阶段四验证脚本：白板③（RAG 三段 / LLM Wiki 起草发布）与白板④（管理员 SaaS / 用户分层 / 租户隔离）。"""  # 模块说明：只用标准库，不依赖 pytest
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import json  # 标准库：请求体与响应体序列化
import os  # 标准库：复制环境变量给子进程
import secrets  # 标准库：为验证服务生成一次性随机角色令牌
import subprocess  # 标准库：拉起构建脚本与 uvicorn
import sys  # 标准库：加入模块搜索路径
import time  # 标准库：轮询服务就绪
import urllib.error  # 标准库：处理非 2xx 响应
import urllib.request  # 标准库：发 HTTP 请求
from pathlib import Path  # 标准库：定位项目根目录

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接执行脚本也能导入 app

from app import permissions, rag, wiki  # 单元级断言直接调用：权限、RAG、Wiki
from app.common import BizError, load_config  # 统一异常与配置加载

CFG = load_config()  # 读取 config.yaml
PORT = int(CFG["service"]["port"]) + 4000  # 验证端口避开阶段一~三，使用 12000
BASE = f"http://{CFG['service']['host']}:{PORT}"  # 服务基址，只连本机回环
ENV = dict(os.environ)  # 复制当前环境变量
ENV["PYTHONIOENCODING"] = "utf-8"  # 强制子进程用 UTF-8 输出，避免中文日志乱码
TOKENS = {str(r): secrets.token_hex(16) for r in CFG["roles"]}  # 业务岗一次性随机令牌
ADMINS = {str(r): secrets.token_hex(16) for r in CFG["permissions"]["admin_roles"]}  # 管理员一次性随机令牌
TOKEN_HEADER = str(CFG["auth"]["header_name"])  # 凭据头名称只从配置取
ENV[str(CFG["auth"]["token_env"])] = json.dumps(TOKENS, ensure_ascii=False)  # 注入业务岗凭据表
ENV[str(CFG["permissions"]["admin_token_env"])] = json.dumps(ADMINS, ensure_ascii=False)  # 注入管理员凭据表
ALL_TOKENS = {**TOKENS, **ADMINS}  # 合并令牌表供 HTTP 封装自动带凭据
RESULTS: list[tuple[str, bool]] = []  # 用例结果容器


def check(name: str, passed: bool, detail: str = "") -> bool:  # 断言并记账
    """记录一个用例的通过与否并打印单行结果。
    入参：name 用例名；passed 是否通过；detail 可选补充信息。
    出参：bool 本次是否通过。
    失败时的行为：不抛异常，只记为失败，保证全部用例跑完。
    """
    RESULTS.append((name, bool(passed)))  # 记账
    print(f"[{'PASS' if passed else 'FAIL'}] {name}{' -> ' + detail if detail else ''}")  # 打印单行结果
    return bool(passed)  # 返回结果


def code_of(fn) -> str:  # 把一次调用折算成错误码
    """执行一个无参函数并把结果折算成错误码字符串。
    入参：fn 无参可调用对象，内部执行待测逻辑。
    出参：通过返回 OK，BizError 返回其 code，未预期异常返回 EXCEPTION:类型。
    失败时的行为：不向外抛异常，便于逐条跑完全部用例。
    """
    try:  # 捕获业务与未预期异常
        fn()  # 执行待测逻辑
        return "OK"  # 通过
    except BizError as exc:  # 预期内拦截
        return str(exc.code)  # 返回业务错误码
    except Exception as exc:  # 未预期崩溃
        return "EXCEPTION:" + type(exc).__name__  # 记为异常，不算通过


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:  # HTTP 调用封装
    """发一个请求并解析统一响应结构。
    入参：method HTTP 方法；path 接口路径；body 可选 JSON 请求体；headers 可选附加请求头。
    出参：解析后的响应体 dict。
    失败时的行为：连接失败返回带 ERR_TRANSPORT 的统一结构，不中断用例。
    """
    merged = {"Content-Type": "application/json"}  # 默认请求头
    merged.update(headers or {})  # 合并调用方自定义头
    role_key = str(merged.get("X-Role", ""))  # 取本次请求角色
    if role_key in ALL_TOKENS and TOKEN_HEADER not in merged:  # 已登记角色且未自定义凭据时自动带令牌
        merged[TOKEN_HEADER] = ALL_TOKENS[role_key]  # 自动注入对应令牌
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None  # 序列化请求体
    req = urllib.request.Request(BASE + path, data=data, headers=merged, method=method)  # 构造请求
    try:  # 正常响应
        with urllib.request.urlopen(req, timeout=15) as resp:  # 发起请求
            return json.loads(resp.read().decode("utf-8"))  # 解析响应体
    except urllib.error.HTTPError as exc:  # 非 2xx
        try:  # 尝试解析错误体
            return json.loads(exc.read().decode("utf-8"))  # 返回服务给的结构
        except Exception:  # 错误体不是 JSON
            return {"ok": False, "data": None, "error": {"code": "ERR_TRANSPORT", "msg": str(exc)}}  # 自造统一结构
    except Exception as exc:  # 连接失败
        return {"ok": False, "data": None, "error": {"code": "ERR_TRANSPORT", "msg": str(exc)}}  # 自造统一结构


def rag_checks() -> None:  # 白板③：RAG 三段单元断言
    """验证 RAG 检索、增强、生成与上下文编号可回溯。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：不抛异常中断，每条断言独立记录。
    """
    hits = rag.retrieve(CFG, "清单版本冲突时如何处理")  # 第一段：检索合成资料
    check("R01 检索返回可核对证据", bool(hits) and all("short_id" in h for h in hits), f"{len(hits)} 条")  # 命中且带短编号
    check("R02 证据数量不超过配置上限", len(hits) <= min(int(CFG["rag"]["top_k"]), int(CFG["knowledge"]["top_k"])))  # 条数受限
    aug = rag.augment(CFG, hits, True, "测试条目摘要")  # 第二段：增强
    check("R03 增强上下文含证据编号", all(f"[{h['short_id']}]" in aug["context"] for h in hits))  # 每条证据进上下文
    check("R04 关联编号不超过上限", len(aug["related"]) <= int(CFG["rag"]["max_graph_refs"]))  # 关联编号截断生效
    empty_aug = rag.augment(CFG, [], True, "")  # 无命中增强
    check("R05 无命中上下文为空", empty_aug["context"] == "")  # 无依据不构造上下文
    gen = rag.generate(CFG, "测试", "B-1", "问题", [], "")  # 第三段：空上下文
    check("R06 无上下文不调模型直接降级", bool(gen["degraded"]) and gen["text"] == "")  # 不凭空生成
    built = rag.build_context(CFG, "清单版本冲突时如何处理", True)  # 一次串起检索与增强
    check("R07 上下文构建含版本字段", "rule_version" in built and "hits" in built)  # 返回结构完整


def wiki_checks() -> None:  # 白板③：LLM Wiki 起草与发布门断言
    """验证 Wiki 草稿生成、草稿不进语料、发布签核层级与单据门。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：不抛异常中断，每条断言独立记录。
    """
    snap = wiki.build_drafts(CFG)  # 从图谱生成草稿
    entries = snap.get("entries", {})  # 取条目表
    check("W01 图谱节点生成 Wiki 草稿", len(entries) > 0 and all(e["status"] == "draft" for e in entries.values()), f"{len(entries)} 条")  # 全为 draft
    check("W02 草稿不进 RAG 语料", wiki.pick_published(CFG, list(entries)[:3]) == "")  # 未发布时摘要为空
    biz_view = wiki.list_entries(CFG, "window", CFG["permissions"]["admin_roles"])  # 业务岗视图
    check("W03 业务岗看不到草稿", all(e["status"] == "published" for e in biz_view["items"]))  # 业务岗只见 published
    admin_view = wiki.list_entries(CFG, "platform_admin", CFG["permissions"]["admin_roles"])  # 管理员视图
    check("W04 管理员可见草稿", admin_view["visible_draft"] and any(e["status"] == "draft" for e in admin_view["items"]))  # 管理员可见 draft
    first = next(iter(entries), "")  # 取首个节点编号
    low = code_of(lambda: wiki.publish_entry(CFG, first, "lin", "L1", "TK-FAKE"))  # 低层级签核
    check("W05 低层级签核发布被拒", low == "WIKI_SIGNER_TIER_LOW", low)  # L1 不得发布
    nodoc = code_of(lambda: wiki.publish_entry(CFG, first, "tan", "L2", "TK-FAKE"))  # 无确认单发布
    check("W06 无确认单发布被拒", nodoc in ("WIKI_CONFIRM_REQUIRED", "WIKI_SIGNER_MISMATCH"), nodoc)  # 必须有已确认单据
    missing = code_of(lambda: wiki.get_entry(CFG, first, "window", CFG["permissions"]["admin_roles"]))  # 业务岗读草稿
    check("W07 业务岗读草稿被拒", missing == "WIKI_DRAFT_NOT_PUBLISHED", missing)  # 草稿对业务岗不可见


def permission_checks() -> None:  # 白板④：权限主体单元断言
    """验证管理员判定、用户分层、租户隔离与敏感动作复核。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：不抛异常中断，每条断言独立记录。
    """
    check("P01 管理员角色识别", permissions.is_admin(CFG, "platform_admin") and permissions.is_admin(CFG, "tenant_admin"))  # 两级管理员
    check("P02 业务岗非管理员", not permissions.is_admin(CFG, "window"))  # 窗口不是管理员
    admin_biz = code_of(lambda: permissions.forbid_admin_precheck(CFG, "platform_admin"))  # 管理员进业务链
    check("P03 管理员发起预检被拒", admin_biz == "ADMIN_FORBIDDEN_BIZ", admin_biz)  # 职责分离
    biz_admin = code_of(lambda: permissions.check_admin(CFG, "window"))  # 业务岗进管理接口
    check("P04 业务岗调用管理接口被拒", biz_admin == "ADMIN_REQUIRED", biz_admin)  # 职责分离
    tier_low = code_of(lambda: permissions.check_tier(CFG, "window", "L2"))  # L1 越层
    check("P05 分层不足被拒", tier_low == "TIER_INSUFFICIENT", tier_low)  # 越层拒绝
    tier_ok = permissions.check_tier(CFG, "reviewer", "L2")  # L2 达标
    check("P06 达标分层放行", tier_ok == "L2")  # 正常放行
    tenant_bad = code_of(lambda: permissions.check_tenant("yz", "other"))  # 跨租户
    check("P07 跨租户被拒", tenant_bad == "TENANT_MISMATCH", tenant_bad)  # 租户隔离
    tenant_ok = permissions.check_tenant("yz", "yz")  # 同租户
    check("P08 同租户放行", tenant_ok == "yz")  # 正常放行
    sensitive = permissions.check_sensitive(CFG, "disable_tenant", "")  # 敏感动作无复核
    check("P09 敏感动作需双人复核", sensitive["need_second"] and not sensitive["ready"])  # 单人未就绪
    ready = permissions.check_sensitive(CFG, "disable_tenant", "admin-b")  # 有复核人
    check("P10 双人复核后就绪", ready["ready"])  # 复核后就绪
    normal = permissions.check_sensitive(CFG, "create_user", "")  # 常规动作
    check("P11 常规动作无需复核", normal["ready"])  # 常规动作直接就绪


def http_checks() -> None:  # HTTP 级断言：新路由可达与拦截
    """启动真实服务，验证 Wiki 与管理接口的可达性、认证与职责分离。
    入参：无。
    出参：无，结果记入 RESULTS。
    失败时的行为：服务未就绪时记录失败并跳过后续 HTTP 用例，最后在 finally 关闭服务。
    """
    log_path = ROOT / "data" / "verify-stage4-server.log"  # 服务日志落盘
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
            check("H00 服务 15 秒内就绪", False, log_path.read_text(encoding="utf-8")[-300:])  # 记录失败原因
            return  # 跳过后续 HTTP 用例
        check("H00 服务 15 秒内就绪", True)  # 服务就绪
        health = call("GET", "/health")  # 不带凭据的健康检查
        check("H01 health 免凭据可达", bool(health.get("ok")))  # 探活接口可用
        wikis = call("GET", "/wiki", headers={"X-Role": "window"})  # 业务岗列 Wiki
        check("H02 业务岗可列已发布 Wiki", bool(wikis.get("ok")))  # 接口可达
        wikis_admin = call("GET", "/wiki", headers={"X-Role": "platform_admin"})  # 管理员列 Wiki
        check("H03 管理员可见草稿标记", bool(wikis_admin.get("ok")) and bool(wikis_admin.get("data", {}).get("visible_draft")))  # 管理员视图含草稿
        roles_admin = call("GET", "/admin/roles", headers={"X-Role": "platform_admin"})  # 管理员读角色矩阵
        check("H04 管理员可读角色矩阵", bool(roles_admin.get("ok")))  # 管理接口可达
        roles_biz = call("GET", "/admin/roles", headers={"X-Role": "window"})  # 业务岗读角色矩阵
        check("H05 业务岗读角色矩阵被拒", (not roles_biz.get("ok")) and roles_biz.get("error", {}).get("code") == "ADMIN_REQUIRED")  # 职责分离
        action = call("POST", "/admin/actions", {"action": "disable_tenant", "target": "yz", "second_admin": ""}, {"X-Role": "tenant_admin", "X-Actor": "admin-a"})  # 敏感动作缺复核
        check("H06 敏感动作落 pending", bool(action.get("ok")) and action.get("data", {}).get("status") == "pending")  # 双人复核门
        audit = call("GET", "/admin/audit", headers={"X-Role": "platform_admin"})  # 管理员审计
        check("H07 管理员审计可查", bool(audit.get("ok")) and len(audit.get("data", {}).get("items", [])) > 0)  # 管理动作留痕
        bad_token = call("GET", "/admin/roles", headers={"X-Role": "platform_admin", TOKEN_HEADER: "bad"})  # 错误凭据
        check("H08 管理员接口校验凭据", (not bad_token.get("ok")) and bad_token.get("error", {}).get("code") == "AUTH_FAILED")  # 认证不被绕过
    finally:  # 无论成败都关闭服务
        proc.terminate()  # 发终止信号
        try:  # 等待进程退出
            proc.wait(timeout=5)  # 最多等 5 秒
        except subprocess.TimeoutExpired:  # 超时强杀
            proc.kill()  # 强制结束
        log.close()  # 关闭日志文件


def main() -> int:  # 验证主流程
    """先跑单元断言，再起服务跑 HTTP 用例，最后汇总。
    入参：无。
    出参：进程退出码，0 表示全部通过、1 表示有失败。
    失败时的行为：任一用例失败记录为失败，返回 1；服务进程在 finally 中关闭。
    """
    rag_checks()  # 白板③ RAG
    wiki_checks()  # 白板③ Wiki
    permission_checks()  # 白板④ 权限
    http_checks()  # HTTP 级断言
    passed = sum(1 for _, p in RESULTS if p)  # 统计通过数
    print(f"[RESULT] {passed}/{len(RESULTS)} 用例通过")  # 打印总结果
    return 0 if passed == len(RESULTS) else 1  # 全通过才返回 0


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
