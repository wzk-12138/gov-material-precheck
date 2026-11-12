"""FastAPI 对外接口层：只做请求解析、调用运行壳、包统一响应体。"""  # 模块说明：业务逻辑全在 harness / guards 里
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

from typing import Any  # 标准库：请求与响应类型标注
from fastapi import Depends, FastAPI, Header, Request  # 用户指定的 Web 框架，不替换；Depends 为框架自带依赖注入
from fastapi.exceptions import RequestValidationError  # 框架参数校验异常，需改写成统一结构
from fastapi.responses import JSONResponse  # 统一 JSON 响应
from pydantic import BaseModel, Field  # 随 FastAPI 安装的校验库，不是中间件
from starlette.exceptions import HTTPException as StarletteHTTPException  # 框架 404 等异常
from . import auth, graph  # 认证卡点、知识图谱统计：健康检查只读节点与关联数量
from .common import BizError, fail, load_config, ok  # 统一异常与统一响应体
from .harness import AgentHarness  # agent-harness 运行壳


class PrecheckIn(BaseModel):  # 清单预检请求体
    """清单预检入参模型。
    入参：business_id 业务编号；question 咨询问题；materials 已提交材料名称。
    出参：校验后的模型实例，供接口函数读取。
    失败时的行为：缺字段或类型不符时框架抛 RequestValidationError，由全局处理器转成 ERR_VALIDATION。
    """
    business_id: str = Field(..., min_length=1)  # 业务编号必填，用于投诉关联与审计
    question: str = Field(..., min_length=1)  # 咨询问题必填，作为检索查询
    materials: list[str] = Field(default_factory=list)  # 材料名可为空列表


class ConfirmIn(BaseModel):  # 人工确认请求体
    """人工确认入参模型。
    入参：ticket_id 待确认单编号；decision 人工结论；note 人工改动说明。
    出参：校验后的模型实例。
    失败时的行为：缺字段抛 RequestValidationError，转成 ERR_VALIDATION；结论取值合法性由 confirmation 层再校验。
    """
    ticket_id: str = Field(..., min_length=1)  # 待确认单编号必填
    decision: str = Field(..., min_length=1)  # accept / reject / revise
    note: str = Field(default="")  # 人工改动说明，纳入审计


class GraphLinkIn(BaseModel):  # 人工确认后登记知识关联的请求体
    """知识关联登记入参模型。
    入参：src / dst 两个事实编号；rel 关系语义；note 核对依据；business_id 业务编号；ticket_id 已确认单据编号。
    出参：校验后的模型实例。
    失败时的行为：缺字段或类型不符抛 RequestValidationError，转成 ERR_VALIDATION；业务合法性由 harness.link_knowledge 再校验。
    """
    src: str = Field(..., min_length=1)  # 起点事实编号必填
    dst: str = Field(..., min_length=1)  # 终点事实编号必填
    rel: str = Field(..., min_length=1)  # 关系语义必填，且必须在配置白名单内
    note: str = Field(default="")  # 人工核对依据，纳入审计
    business_id: str = Field(..., min_length=1)  # 业务编号必填，用于重建处理链
    ticket_id: str = Field(..., min_length=1)  # 已人工确认的待确认单编号必填


CFG = load_config()  # 启动时一次性读配置；失败直接让进程起不来，避免带病运行
HARNESS = AgentHarness(CFG)  # 构造运行壳，后续请求复用同一实例
app = FastAPI(title=CFG["agent"]["name"], docs_url=None, redoc_url=None)  # 关闭在线文档页，减少对外暴露面


def _gate(request: Request) -> None:  # 对外接口的统一认证卡点
    """在业务路由执行前校验角色与凭据绑定。
    入参：request 当前 HTTP 请求，读取 X-Role 与配置指定的凭据头。
    出参：无，校验通过直接返回。
    失败时的行为：抛 BizError，由全局处理器改成统一失败结构；/health 不挂此依赖以便探活。
    """
    # 为什么这么卡：认证必须前置到所有业务路由，任何一个接口漏挂就会变成「只凭明文角色头」的越权入口（f41 / f45）
    auth.verify_request(CFG, request.headers.get("X-Role"), request.headers.get(str(CFG["auth"]["header_name"])))  # 角色与令牌必须同源


def _uni(code: str, msg: str) -> JSONResponse:  # 统一失败响应构造器
    """把任意失败改写成约定结构。
    入参：code 错误码；msg 中文说明。
    出参：JSONResponse，HTTP 状态恒 200，body 为统一结构。
    失败时的行为：无失败分支；不回显堆栈、配置内容与输入原文。
    """
    # 为什么这么卡：框架默认的 422 / 404 / 500 响应体不符合约定，必须全部由全局处理器改写成同一结构
    return JSONResponse(status_code=200, content=fail(code, msg))  # 统一 200 + ok=false，由 error.code 区分原因


@app.exception_handler(BizError)  # 业务异常处理器
async def _biz_handler(request: Request, exc: BizError) -> JSONResponse:  # 把关卡失败转成统一响应
    """业务异常处理器。
    入参：request 当前请求；exc 业务异常。
    出参：统一失败响应。
    失败时的行为：无失败分支，异常码与说明原样透传到 error 字段。
    """
    return _uni(exc.code, exc.msg)  # 复用统一构造器


@app.exception_handler(RequestValidationError)  # 参数校验异常处理器
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:  # 把 422 改成统一响应
    """参数校验异常处理器。
    入参：request 当前请求；exc 校验异常。
    出参：统一失败响应，错误码 ERR_VALIDATION。
    失败时的行为：无失败分支；不回显字段级明细，避免泄露内部结构。
    """
    return _uni("ERR_VALIDATION", "请求参数不合法，请检查必填字段与类型")  # 提示可操作，但不泄露细节


@app.exception_handler(StarletteHTTPException)  # HTTP 异常处理器
async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:  # 把 404 改成统一响应
    """HTTP 异常处理器。
    入参：request 当前请求；exc HTTP 异常。
    出参：统一失败响应，错误码 ERR_HTTP。
    失败时的行为：无失败分支；把框架状态码信息压进 msg，不返回框架默认 JSON。
    """
    return _uni("ERR_HTTP", f"接口不存在或不可访问（{exc.status_code}）")  # 统一结构


@app.exception_handler(Exception)  # 兜底异常处理器
async def _internal_handler(request: Request, exc: Exception) -> JSONResponse:  # 把未捕获异常改成统一响应
    """兜底异常处理器。
    入参：request 当前请求；exc 任意未捕获异常。
    出参：统一失败响应，错误码 ERR_INTERNAL。
    失败时的行为：无失败分支；不回显堆栈，异常由服务器日志记录。
    """
    return _uni("ERR_INTERNAL", "服务内部错误，请查看服务端日志")  # 不泄露本地文件路径


@app.get("/health")  # 健康检查
def health() -> dict:  # 返回服务与配置摘要
    """健康检查与配置摘要。
    入参：无。
    出参：统一成功响应，data 含系统名、监听地址、模型配置、规则库路径与资料版本。
    失败时的行为：配置在模块导入时已校验；此接口不返回任何密钥或规则原文。
    """
    try:  # 图谱未构建时健康检查仍要可用
        graph_info = {"ready": True, **graph.stats(CFG)}  # 只回节点数、关联数、关系类型与资料版本，不回任何原文
    except BizError:  # 未建图谱或快照损坏
        graph_info = {"ready": False, "nodes": 0, "edges": 0, "relations": {}, "rule_version": "UNBUILT"}  # 降级摘要，便于运维发现
    auth_info = {"required": bool(CFG["auth"]["required"]), "header": str(CFG["auth"]["header_name"]), "token_env": str(CFG["auth"]["token_env"])}  # 只回认证开关与环境变量名，绝不回令牌内容
    return ok({"service": CFG["agent"]["name"], "host": CFG["service"]["host"], "port": CFG["service"]["port"], "ollama_base_url": CFG["ollama"]["base_url"], "ollama_model": CFG["ollama"]["model"], "kb_path": CFG["knowledge"]["kb_path"], "rule_version": HARNESS.rule_version(), "graph": graph_info, "auth": auth_info})  # 只给可核对摘要


@app.get("/kb/search", dependencies=[Depends(_gate)])  # 知识库只读检索
def kb_search(q: str = "", x_role: str | None = Header(None, alias="X-Role")) -> dict:  # 查询参数与角色头
    """知识库只读检索接口。
    入参：q 查询文本；x_role 请求头 X-Role 角色键。
    出参：统一成功响应，data 为检索结果与资料版本。
    失败时的行为：权限或红线失败由全局处理器转成统一失败响应。
    """
    return ok(HARNESS.search(x_role, q))  # 调用运行壳并包统一结构


@app.post("/precheck", dependencies=[Depends(_gate)])  # 清单预检
def precheck(payload: PrecheckIn, x_role: str | None = Header(None, alias="X-Role"), x_actor: str | None = Header(None, alias="X-Actor")) -> dict:  # 请求体 + 两个身份头
    """清单预检接口。
    入参：payload 预检请求体；x_role 角色键；x_actor 操作者标识。
    出参：统一成功响应，data 含建议、证据与待确认单，requires_human_confirmation 恒为 True。
    失败时的行为：缺少操作者抛 ACTOR_REQUIRED；其余关卡错误由全局处理器统一改写。
    """
    actor = (x_actor or "").strip()  # 规整操作者标识
    if not actor:  # 缺少操作者标识
        # 为什么这么卡：审计与禁止自确认都依赖明确的接手人，匿名请求无法重建处理链（f45）
        raise BizError("ACTOR_REQUIRED", "缺少 X-Actor 请求头，请标明操作者")  # 拒绝匿名发起
    return ok(HARNESS.precheck(x_role, actor, payload.business_id, payload.question, payload.materials))  # 调用主链路


@app.post("/confirm", dependencies=[Depends(_gate)])  # 人工确认
def confirm(payload: ConfirmIn, x_role: str | None = Header(None, alias="X-Role"), x_actor: str | None = Header(None, alias="X-Actor")) -> dict:  # 请求体 + 两个身份头
    """人工确认接口。
    入参：payload 确认请求体；x_role 确认角色；x_actor 确认人标识。
    出参：统一成功响应，data 含单据状态、结论、签核人与确认时间。
    失败时的行为：缺少操作者抛 ACTOR_REQUIRED；越权、单据状态与自确认错误由全局处理器统一改写。
    """
    actor = (x_actor or "").strip()  # 规整确认人标识
    if not actor:  # 缺少确认人标识
        # 为什么这么卡：签核人必须可追溯，否则无法确认「对方确认接手」这一责任转移时点（f23）
        raise BizError("ACTOR_REQUIRED", "缺少 X-Actor 请求头，请标明确认人")  # 拒绝匿名签核
    return ok(HARNESS.confirm(x_role, actor, payload.ticket_id, payload.decision, payload.note))  # 调用确认链路


@app.get("/audit", dependencies=[Depends(_gate)])  # 审计流水查询
def audit(limit: int = 50, x_role: str | None = Header(None, alias="X-Role")) -> dict:  # 条数与角色头
    """审计流水查询接口。
    入参：limit 最多返回条数；x_role 角色键。
    出参：统一成功响应，data 为最近审计记录列表。
    失败时的行为：匿名或未登记角色由全局处理器转成统一失败响应；limit 非法转成 ERR_VALIDATION。
    """
    return ok(HARNESS.audit(x_role, limit))  # 调用运行壳并包统一结构


@app.get("/graph/stats", dependencies=[Depends(_gate)])  # 知识图谱统计
def graph_stats(x_role: str | None = Header(None, alias="X-Role")) -> dict:  # 角色头
    """知识图谱规模统计接口。
    入参：x_role 角色键。
    出参：统一成功响应，data 含节点数、关联数、关系类型与资料版本。
    失败时的行为：越权由全局处理器统一改写；图谱未构建时返回 ready=False 的全零统计。
    """
    return ok(HARNESS.graph_stats(x_role))  # 调用运行壳并包统一结构


@app.get("/graph/relations", dependencies=[Depends(_gate)])  # 知识与知识之间的关联查询
def graph_relations(node_id: str = "", rel: str = "", x_role: str | None = Header(None, alias="X-Role")) -> dict:  # 编号、关系过滤与角色头
    """查询某个知识节点的全部关联。
    入参：node_id 节点编号；rel 可选关系语义；x_role 角色键。
    出参：统一成功响应，data 含出向与入向关联及对端节点。
    失败时的行为：越权、红线或节点不存在由全局处理器统一改写；非授权岗位的节点属性已裁剪。
    """
    return ok(HARNESS.graph_relations(x_role, node_id, rel))  # 调用运行壳并包统一结构


@app.get("/graph/path", dependencies=[Depends(_gate)])  # 两个知识之间的最短关联链
def graph_path(src: str = "", dst: str = "", x_role: str | None = Header(None, alias="X-Role")) -> dict:  # 起止编号与角色头
    """最短关联链接口。
    入参：src / dst 起止节点编号；x_role 角色键。
    出参：统一成功响应，data 含 chain、逐跳 hops 与 found 标记。
    失败时的行为：越权或端点不存在由全局处理器统一改写；不连通返回 found=False。
    """
    return ok(HARNESS.graph_path(x_role, src, dst))  # 调用运行壳并包统一结构


@app.get("/graph/subgraph", dependencies=[Depends(_gate)])  # 多跳关联子图
def graph_subgraph(node_id: str = "", depth: int = 2, x_role: str | None = Header(None, alias="X-Role")) -> dict:  # 中心编号、跳数与角色头
    """多跳关联子图接口。
    入参：node_id 中心节点编号；depth 跳数；x_role 角色键。
    出参：统一成功响应，data 含子图节点、内部关联与实际跳数。
    失败时的行为：越权或节点不存在由全局处理器统一改写；depth 超限由框架转 ERR_VALIDATION 或运行壳压到上限。
    """
    return ok(HARNESS.graph_subgraph(x_role, node_id, depth))  # 调用运行壳并包统一结构


@app.get("/graph/relation-types", dependencies=[Depends(_gate)])  # 关系语义清单
def graph_relation_types(x_role: str | None = Header(None, alias="X-Role")) -> dict:  # 角色头
    """关系语义清单接口。
    入参：x_role 角色键。
    出参：统一成功响应，data 含全部关系类型及条数。
    失败时的行为：越权或图谱缺失由全局处理器统一改写。
    """
    return ok(HARNESS.graph_relation_types(x_role))  # 调用运行壳并包统一结构


@app.post("/graph/link", dependencies=[Depends(_gate)])  # 人工确认后登记知识关联
def graph_link(payload: GraphLinkIn, x_role: str | None = Header(None, alias="X-Role"), x_actor: str | None = Header(None, alias="X-Actor")) -> dict:  # 请求体 + 两个身份头
    """登记一条人工核对过的知识关联。
    入参：payload 关联登记请求体；x_role 登记角色；x_actor 登记人标识。
    出参：统一成功响应，data 含新关联、签核人与图谱统计。
    失败时的行为：缺少登记人抛 ACTOR_REQUIRED；越权、单据未确认、签核人不符、关系不在白名单或三元组重复由全局处理器统一改写，均不写图谱。
    """
    actor = (x_actor or "").strip()  # 规整登记人标识
    if not actor:  # 缺少登记人标识
        # 为什么这么卡：写图谱属于规则源变更，必须落到具体签核人，匿名登记无法重建「谁改了什么」（f45）
        raise BizError("ACTOR_REQUIRED", "缺少 X-Actor 请求头，请标明登记人")  # 拒绝匿名写入
    body = {"src": payload.src, "dst": payload.dst, "rel": payload.rel, "note": payload.note, "business_id": payload.business_id, "ticket_id": payload.ticket_id}  # 组装业务数据
    return ok(HARNESS.link_knowledge(x_role, actor, body))  # 调用运行壳并包统一结构


@app.get("/audit/replay", dependencies=[Depends(_gate)])  # 按业务编号重建处理链
def audit_replay(business_id: str = "", x_role: str | None = Header(None, alias="X-Role")) -> dict:  # 业务编号与角色头
    """按业务编号重建一次完整处理链。
    入参：business_id 业务编号；x_role 角色键。
    出参：统一成功响应，data 含按时间排序的 chain、动作序列与总数。
    失败时的行为：越权或编号命中红线由全局处理器统一改写；无记录返回空链。
    """
    return ok(HARNESS.replay(x_role, business_id))  # 调用运行壳并包统一结构
