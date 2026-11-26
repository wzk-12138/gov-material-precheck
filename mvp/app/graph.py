"""知识图谱：把合成事实之间的关联显式建模，本地文件 + 标准库存取。"""  # 模块说明：不引入图数据库、RDF 或任何中间件
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import itertools  # 标准库：事实两两组合，构建同需求的共现关联
import time  # 标准库：图谱构建时间戳
from collections import Counter, deque  # 标准库：关系类型计数与广度优先遍历
from typing import Any  # 标准库：图谱数据结构类型标注

from . import knowledge  # 复用规则库的纯标准库词元切分，保证关联阈值口径一致
from .common import BizError, read_json, resolve_path, write_json  # 统一异常与本地存储


def graph_conf(cfg: dict) -> dict:  # 读取图谱配置节
    """读取并校验 config.yaml 的 graph 节。
    入参：cfg 配置字典。
    出参：graph 配置子字典。
    失败时的行为：缺节或类型不符抛 BizError(CONFIG_INVALID)，绝不在代码里补默认路径。
    """
    conf = cfg.get("graph")  # 图谱参数一律从配置读取
    if not isinstance(conf, dict):  # 配置节缺失或写成了标量
        raise BizError("CONFIG_INVALID", "config.yaml 缺少 graph 配置节")  # 明确失败，不带病建图
    return conf  # 返回图谱配置


def _facts(cfg: dict) -> tuple[list[dict], str]:  # 读取规则库事实条目
    """读取已构建的本地规则库。
    入参：cfg 配置字典，知识库路径取自 knowledge.kb_path。
    出参：元组 (事实条目列表, 资料版本字符串)。
    失败时的行为：规则库缺失或为空抛 BizError(KB_MISSING)；文件损坏由 read_json 抛 STORE_READ_FAILED。
    """
    path = resolve_path(cfg["knowledge"]["kb_path"])  # 规则库路径只从配置取
    if not path.exists():  # 尚未执行 scripts/build_kb.py
        raise BizError("KB_MISSING", "规则库未生成，请先运行 scripts/build_kb.py")  # 给出可操作指引
    payload = read_json(path, {})  # 标准库读取本地 JSON
    items = payload.get("items", [])  # 取出事实条目
    if not isinstance(items, list) or not items:  # 结构异常或空库
        raise BizError("KB_MISSING", "规则库为空，无法构建知识图谱")  # 拒绝生成空图谱
    return [dict(it) for it in items], str(payload.get("rule_version", "UNKNOWN"))  # 复制条目，避免改动库内对象


def _bigrams(text: str) -> set[str]:  # 取中文二元词元集合
    """把一段事实正文切成中文二字词集合，用于计算知识之间的内容相关度。
    入参：text 事实正文。
    出参：中文二字词集合。
    失败时的行为：无失败分支；空文本返回空集合。
    """
    words = knowledge.tokenize(text)  # 复用规则库的纯标准库切分
    return {t for t in words if len(t) == 2 and "\u4e00" <= t[0] <= "\u9fff" and "\u4e00" <= t[1] <= "\u9fff"}  # 只留中文二字词，过滤编号与英文


def _jaccard(a: set[str], b: set[str]) -> float:  # 计算词元重合度
    """计算两条事实正文的 Jaccard 重合度，作为「内容相关」关联的可核对依据。
    入参：a / b 两条事实的中文二字词集合。
    出参：0 到 1 之间的浮点值。
    失败时的行为：任一集合为空返回 0.0，不抛异常。
    """
    if not a or not b:  # 无可比较词元
        return 0.0  # 视为不相关
    return len(a & b) / len(a | b)  # 交集比并集


def _source_kind(source: str) -> str:  # 提取资料类型
    """从「政务-001 · 访谈事实记录」这类来源串中取出资料类型。
    入参：source 事实来源字符串。
    出参：资料类型名；取不到时返回「未标注」。
    失败时的行为：无失败分支；空串返回「未标注」。
    """
    tail = str(source).split("·")[-1].strip()  # 以分隔符取末段
    return tail or "未标注"  # 空值给可核对的兜底标签


def _add_node(nodes: dict[str, dict], nid: str, ntype: str, label: str, meta: dict | None = None) -> None:  # 登记一个图谱节点
    """往节点表登记一个节点，同编号只登记一次。
    入参：nodes 节点表；nid 节点编号；ntype 节点类型；label 公开标签；meta 可选属性。
    出参：无，就地写入 nodes。
    失败时的行为：无失败分支；重复编号静默忽略，保证节点唯一。
    """
    if nid not in nodes:  # 同编号只建一次
        nodes[nid] = {"id": nid, "type": ntype, "label": label, "meta": dict(meta or {})}  # 组装节点结构


def _add_edge(edges: list[dict], seen: set[tuple[str, str, str]], src: str, dst: str, rel: str, note: str) -> None:  # 登记一条语义关联
    """往关联表登记一条三元组，并按 (起点, 终点, 关系) 去重。
    入参：edges 关联表；seen 去重集合；src 起点；dst 终点；rel 关系语义；note 可核对依据。
    出参：无，就地写入 edges 与 seen。
    失败时的行为：无失败分支；自环或重复三元组静默忽略。
    """
    key = (src, dst, rel)  # 三元组去重键
    if src == dst or key in seen:  # 不建自环，也不重复登记
        return  # 直接跳过
    seen.add(key)  # 标记已登记
    edges.append({"src": src, "dst": dst, "rel": rel, "note": note})  # 关联带上依据，便于人工核对


def _summarize(graph: dict) -> dict:  # 统计图谱规模
    """统计节点数、关联数与各关系类型的条数。
    入参：graph 图谱 dict。
    出参：含 nodes / edges / relations 的统计字典。
    失败时的行为：缺 nodes 或 edges 时按空列表统计，不抛异常。
    """
    nodes = graph.get("nodes", [])  # 取节点列表
    edges = graph.get("edges", [])  # 取关联列表
    counter = Counter(str(e.get("rel", "")) for e in edges)  # 按关系语义计数
    return {"nodes": len(nodes), "edges": len(edges), "relations": dict(counter)}  # 返回可核对统计


def build(cfg: dict) -> dict:  # 从规则库构建知识图谱并落盘
    """把事实、需求、岗位、资料、版本建成节点，并建立可核对的知识关联。
    入参：cfg 配置字典，路径与阈值全从 config.yaml 的 graph / knowledge 节读取。
    出参：完整图谱 dict，同时原子写入 graph.path。
    失败时的行为：规则库缺失抛 KB_MISSING；配置缺 graph 节抛 CONFIG_INVALID；磁盘异常原样抛出。
    """
    conf = graph_conf(cfg)  # 取图谱配置，缺节直接失败
    items, version = _facts(cfg)  # 读合成事实与资料版本
    nodes: dict[str, dict] = {}  # 节点表，键为节点编号
    edges: list[dict] = []  # 关联表
    seen: set[tuple[str, str, str]] = set()  # 三元组去重集合
    topic_ids: dict[str, str] = {}  # 需求标题到节点编号的映射
    source_ids: dict[str, str] = {}  # 资料类型到节点编号的映射
    role_ids: dict[str, str] = {}  # 岗位名到节点编号的映射
    version_id = "V1"  # 资料版本节点编号，单次构建只有一个版本
    _add_node(nodes, version_id, "version", version, {"rule_version": version})  # 登记资料版本节点
    short_ids: list[str] = []  # 事实节点编号清单，供两两组合
    grams: dict[str, set[str]] = {}  # 事实编号到词元集合的映射
    texts: dict[str, str] = {}  # 事实编号到正文的映射
    for it in items:  # 逐条事实建节点与归属关联
        sid = str(it.get("short_id", "")).strip()  # 取短编号 fxx
        if not sid:  # 无编号的事实无法被引用
            continue  # 跳过，保证节点都可核对
        text = str(it.get("text", ""))  # 事实正文
        topic = str(it.get("topic", "未标注需求"))  # 所属需求标题
        character = str(it.get("character", "未标注岗位"))  # 提出岗位
        source = str(it.get("source", ""))  # 资料来源串
        kind = _source_kind(source)  # 资料类型
        if topic not in topic_ids:  # 首次遇到该需求
            tid = f"T{len(topic_ids) + 1}"  # 顺序编号，稳定可核对
            topic_ids[topic] = tid  # 记入映射
            _add_node(nodes, tid, "topic", topic, {"topic": topic})  # 登记需求节点
        if character not in role_ids:  # 首次遇到该岗位
            rid = f"R{len(role_ids) + 1}"  # 顺序编号
            role_ids[character] = rid  # 记入映射
            _add_node(nodes, rid, "role", character, {"character": character})  # 登记岗位节点
        if kind not in source_ids:  # 首次遇到该资料类型
            kid = f"S{len(source_ids) + 1}"  # 顺序编号
            source_ids[kind] = kid  # 记入映射
            _add_node(nodes, kid, "source", kind, {"source_kind": kind})  # 登记资料类型节点
        _add_node(nodes, sid, "fact", sid, {"text": text, "topic": topic, "character": character, "source": source, "full_id": str(it.get("id", "")), "rule_version": version})  # 事实节点：正文放 meta，便于按权限裁剪
        tid, rid, kid = topic_ids[topic], role_ids[character], source_ids[kind]  # 取三类归属节点编号
        _add_edge(edges, seen, sid, tid, "属于需求", f"同属需求：{topic}")  # 归属关联，依据来自 requirements 分组
        _add_edge(edges, seen, tid, sid, "包含事实", f"需求收录事实 {sid}")  # 反向关联，让多跳链能扩散到同组事实
        _add_edge(edges, seen, sid, rid, "由谁提出", f"访谈事实由 {character} 提出")  # 岗位关联，依据来自 evidence.character
        _add_edge(edges, seen, rid, sid, "涉及岗位", f"岗位 {character} 涉及事实 {sid}")  # 反向岗位关联
        _add_edge(edges, seen, sid, kid, "出自资料", f"资料类型：{kind}")  # 资料关联，依据来自 evidence.source
        _add_edge(edges, seen, kid, sid, "收录事实", f"资料类型 {kind} 收录事实 {sid}")  # 反向资料关联
        _add_edge(edges, seen, sid, version_id, "生效版本", f"资料版本 {version}")  # 版本关联，依据来自规则库版本
        _add_edge(edges, seen, version_id, sid, "版本收录", f"版本 {version} 收录事实 {sid}")  # 反向版本关联
        short_ids.append(sid)  # 记入事实编号清单
        grams[sid] = _bigrams(text)  # 缓存词元集合
        texts[sid] = text  # 缓存正文
    groups: dict[str, list[str]] = {}  # 按需求分组的事实编号
    for sid in short_ids:  # 遍历事实编号
        topic = str(nodes[sid]["meta"].get("topic", ""))  # 取所属需求
        groups.setdefault(topic, []).append(sid)  # 归入同组
    for topic, members in groups.items():  # 组内两两建「同一需求共现」
        for a, b in itertools.combinations(members, 2):  # 只取无序对，避免重复
            note = f"同属需求：{topic}"  # 可核对依据
            _add_edge(edges, seen, a, b, "同一需求共现", note)  # 正向共现关联
            _add_edge(edges, seen, b, a, "同一需求共现", note)  # 反向共现关联
    threshold = float(conf["related_threshold"])  # 内容相关阈值只从配置取
    top_k = int(conf["related_top_k"])  # 每条事实最多保留的内容关联数
    candidates: list[tuple[float, str, str]] = []  # 相关度候选三元组
    for a, b in itertools.combinations(short_ids, 2):  # 全量两两比较
        score = _jaccard(grams[a], grams[b])  # 词元重合度
        if score >= threshold:  # 达到阈值才认为内容相关
            candidates.append((score, a, b))  # 收入候选
    candidates.sort(key=lambda x: x[0], reverse=True)  # 按相关度从高到低排
    kept: Counter = Counter()  # 每条事实已保留的内容关联数
    for score, a, b in candidates:  # 依序挑选
        if kept[a] >= top_k or kept[b] >= top_k:  # 任一端已达上限
            continue  # 跳过，避免全连接图
        note = f"正文中文二字词 Jaccard={score:.3f}（可核对）"  # 关联依据写进 note
        _add_edge(edges, seen, a, b, "内容相关", note)  # 正向内容关联
        _add_edge(edges, seen, b, a, "内容相关", note)  # 反向内容关联
        kept[a] += 1  # 累加起点计数
        kept[b] += 1  # 累加终点计数
    for a in short_ids:  # 检查事实之间的直接援引
        for b in short_ids:  # 遍历可能被援引的事实
            if a == b or not texts[b] or len(texts[b]) < 8:  # 排除自身与过短正文
                continue  # 跳过不可核对的组合
            if texts[b] in texts[a]:  # a 的正文完整引用了 b 的正文
                _add_edge(edges, seen, a, b, "援引", f"事实 {a} 正文直接援引 {b} 原文")  # 援引关联，依据为原文包含关系
    payload: dict[str, Any] = {"rule_version": version, "built_at": time.time(), "source_note": "仅合成训练资料，关联依据均可在原始事实中核对", "nodes": list(nodes.values()), "edges": edges}  # 组装图谱
    payload["stats"] = _summarize(payload)  # 附上可核对统计
    write_json(resolve_path(conf["path"]), payload)  # 原子写入本地图谱文件
    return payload  # 返回内存图谱


def load(cfg: dict) -> dict:  # 读取图谱快照
    """读取本地知识图谱快照；人工确认后写入的新关联也存这里，重启自动回载。
    入参：cfg 配置字典，路径取自 graph.path。
    出参：完整图谱 dict。
    失败时的行为：文件不存在抛 GRAPH_MISSING；结构不完整抛 GRAPH_INVALID；内容损坏由 read_json 抛 STORE_READ_FAILED。
    """
    path = resolve_path(graph_conf(cfg)["path"])  # 图谱路径只从配置取
    if not path.exists():  # 尚未执行 scripts/build_graph.py
        raise BizError("GRAPH_MISSING", "知识图谱未生成，请先运行 scripts/build_graph.py")  # 给出可操作指引
    graph = read_json(path, {})  # 标准库读取本地 JSON
    if not isinstance(graph, dict) or not graph.get("nodes") or not graph.get("edges"):  # 结构不完整
        raise BizError("GRAPH_INVALID", "知识图谱文件结构不完整，请重新构建")  # 拒绝带病查询
    return graph  # 返回图谱


def stats(cfg: dict) -> dict:  # 图谱统计
    """返回图谱规模统计与资料版本。
    入参：cfg 配置字典。
    出参：含 rule_version / nodes / edges / relations 的字典。
    失败时的行为：图谱缺失或配置不合法时抛对应 BizError，由接口层统一改写。
    """
    graph = load(cfg)  # 读取图谱
    return {"rule_version": str(graph.get("rule_version", "UNKNOWN")), **_summarize(graph)}  # 合并版本与统计


def relation_types(cfg: dict) -> dict:  # 关系类型清单
    """列出图谱中全部知识关联语义及条数。
    入参：cfg 配置字典。
    出参：含 items（按条数降序）与 total 的字典。
    失败时的行为：图谱缺失抛 GRAPH_MISSING。
    """
    relations = _summarize(load(cfg))["relations"]  # 取关系计数
    items = [{"rel": k, "count": v} for k, v in sorted(relations.items(), key=lambda x: (-x[1], x[0]))]  # 转成可核对清单
    return {"items": items, "total": len(items)}  # 返回清单与类型数


def _neighbors(graph: dict, fact_only: bool = False) -> dict[str, list[tuple[str, dict]]]:  # 构建无向邻接表
    """把有语义方向的关联整理成双向邻接，用于多跳链与最短链遍历。
    入参：graph 图谱 dict；fact_only 为 True 时只保留事实与事实之间的知识关联。
    出参：节点编号到 (邻居编号, 原关联) 列表的映射。
    失败时的行为：无失败分支；孤立节点不出现在邻接表中。
    """
    table = _table(graph) if fact_only else {}  # 需要判定节点类型时才建索引
    adj: dict[str, list[tuple[str, dict]]] = {}  # 邻接表
    for edge in graph["edges"]:  # 遍历全部关联
        src, dst = str(edge["src"]), str(edge["dst"])  # 取两端编号
        if fact_only and (table.get(src, {}).get("type") != "fact" or table.get(dst, {}).get("type") != "fact"):  # 归属类边会让两跳覆盖全图
            continue  # 只沿事实之间的关联扩散，证据链才可核对
        adj.setdefault(src, []).append((dst, edge))  # 正向可达
        adj.setdefault(dst, []).append((src, edge))  # 反向可达，知识与知识之间的关联按无向使用
    return adj  # 返回邻接表


def _public_node(node: dict, can_view: bool) -> dict:  # 按角色裁剪节点属性
    """把图谱节点裁成可对外返回的形态。
    入参：node 图谱节点；can_view 该角色能否查看材料与证据原文。
    出参：含 id / type / label / meta 的节点 dict。
    失败时的行为：无失败分支；节点缺 meta 时按空字典处理。
    """
    meta = dict(node.get("meta", {}))  # 复制属性，避免改动图谱内存对象
    if not can_view:  # 非授权岗位只看得到关联结构
        # 为什么这么卡：材料 f41 要求只有授权岗位可查看材料原文，热线等岗位连图谱节点的正文、来源、岗位都不给
        for key in ("text", "source", "character", "full_id"):  # 逐项去掉可能含原文的字段
            meta.pop(key, None)  # 只留 topic 与版本等公开字段
    return {"id": str(node.get("id", "")), "type": str(node.get("type", "")), "label": str(node.get("label", "")), "meta": meta}  # 返回裁剪后的节点


def _table(graph: dict) -> dict[str, dict]:  # 构建节点编号索引
    """把节点列表整理成编号到节点的映射。
    入参：graph 图谱 dict。
    出参：节点编号到节点 dict 的映射。
    失败时的行为：无失败分支；重复编号保留最后一个。
    """
    return {str(n.get("id", "")): n for n in graph["nodes"]}  # 字典推导建索引


def relations(cfg: dict, node_id: str, rel: str = "", can_view: bool = False) -> dict:  # 查询一个知识的关联
    """按编号查询某个知识节点的全部关联（出向 + 入向）。
    入参：cfg 配置字典；node_id 节点编号；rel 可选关系类型过滤；can_view 是否可看原文。
    出参：含 node / links / total / truncated 的字典。
    失败时的行为：节点不存在抛 GRAPH_NODE_NOT_FOUND；图谱缺失抛 GRAPH_MISSING；结果按 graph.max_nodes 截断。
    """
    conf = graph_conf(cfg)  # 取截断上限
    graph = load(cfg)  # 读图谱
    table = _table(graph)  # 建节点索引
    if node_id not in table:  # 编号不存在
        raise BizError("GRAPH_NODE_NOT_FOUND", f"知识节点不存在：{node_id}")  # 拒绝凭空查询
    limit = int(conf["max_nodes"])  # 单次返回上限
    links: list[dict] = []  # 关联结果容器
    for edge in graph["edges"]:  # 逐条筛选与该节点有关的关联
        src, dst = str(edge["src"]), str(edge["dst"])  # 取两端编号
        if node_id not in (src, dst):  # 与该节点无关
            continue  # 跳过
        if rel and str(edge["rel"]) != rel:  # 关系类型过滤不命中
            continue  # 跳过
        direction = "out" if src == node_id else "in"  # 标注语义方向
        peer_id = dst if direction == "out" else src  # 取对端编号
        if peer_id not in table:  # 对端节点缺失，属脏数据
            continue  # 跳过，不返回悬空关联
        links.append({"direction": direction, "rel": str(edge["rel"]), "note": str(edge.get("note", "")), "peer": _public_node(table[peer_id], can_view)})  # 组装可核对关联
    total = len(links)  # 记录截断前总数
    return {"node": _public_node(table[node_id], can_view), "links": links[:limit], "total": total, "truncated": total > limit}  # 返回关联查询结果


def shortest_path(cfg: dict, src: str, dst: str, can_view: bool = False) -> dict:  # 查询两个知识之间的最短关联链
    """用广度优先遍历找两个知识节点之间跳数最少的关联链。
    入参：cfg 配置字典；src / dst 起止节点编号；can_view 是否可看原文。
    出参：含 chain（节点编号序列）/ hops（逐跳关联）/ nodes / found 的字典。
    失败时的行为：任一端不存在抛 GRAPH_NODE_NOT_FOUND；不连通时返回 found=False 与空链，不报错。
    """
    graph = load(cfg)  # 读图谱
    table = _table(graph)  # 建节点索引
    if src not in table or dst not in table:  # 端点不存在
        raise BizError("GRAPH_NODE_NOT_FOUND", "最短链的起点或终点不存在")  # 明确失败
    adj = _neighbors(graph, True)  # 最短链只走事实与事实之间的知识关联，避免经版本节点产生无意义的两跳
    queue: deque[str] = deque([src])  # 从起点开始
    parent: dict[str, tuple[str, dict]] = {}  # 记录每个节点由哪条边走到
    visited: set[str] = {src}  # 已访问集合
    while queue:  # 广度优先遍历
        cur = queue.popleft()  # 取当前节点
        if cur == dst:  # 到达终点
            break  # 提前结束
        for nxt, edge in adj.get(cur, []):  # 遍历邻居
            if nxt in visited:  # 已访问
                continue  # 跳过，避免环路
            visited.add(nxt)  # 标记访问
            parent[nxt] = (cur, edge)  # 记录父节点与关联
            queue.append(nxt)  # 入队继续扩散
    if dst not in parent and src != dst:  # 不连通
        return {"chain": [], "hops": [], "nodes": [], "found": False}  # 明确标记未找到
    chain = [dst]  # 从终点回溯
    hops: list[dict] = []  # 逐跳关联容器
    cur = dst  # 回溯游标
    while cur != src:  # 一直回到起点
        prev, edge = parent[cur]  # 取父节点与关联
        hops.append({"from": prev, "to": cur, "rel": str(edge["rel"]), "note": str(edge.get("note", ""))})  # 记录一跳
        chain.append(prev)  # 追加节点
        cur = prev  # 继续回溯
    chain.reverse()  # 转成起点到终点顺序
    hops.reverse()  # 同步翻正逐跳顺序
    for i, hop in enumerate(hops, 1):  # 标注跳数
        hop["depth"] = i  # 第 i 跳
    nodes = [_public_node(table[nid], can_view) for nid in chain if nid in table]  # 按权限裁剪链上节点
    return {"chain": chain, "hops": hops, "nodes": nodes, "found": True}  # 返回关联链


def subgraph(cfg: dict, node_id: str, depth: int, can_view: bool = False) -> dict:  # 查询多跳关联子图
    """以某个知识为中心，取指定跳数内的关联子图。
    入参：cfg 配置字典；node_id 中心节点编号；depth 跳数；can_view 是否可看原文。
    出参：含 nodes / edges / depth / truncated 的字典。
    失败时的行为：节点不存在抛 GRAPH_NODE_NOT_FOUND；depth 超过 graph.max_depth 时压到上限，不拒绝请求。
    """
    conf = graph_conf(cfg)  # 取跳数与节点上限
    graph = load(cfg)  # 读图谱
    table = _table(graph)  # 建节点索引
    if node_id not in table:  # 中心节点不存在
        raise BizError("GRAPH_NODE_NOT_FOUND", f"知识节点不存在：{node_id}")  # 明确失败
    max_depth = int(conf["max_depth"])  # 配置的最大跳数
    use_depth = max(0, min(int(depth), max_depth))  # 压到合法范围，避免一次拉全图
    adj = _neighbors(graph, True)  # 只沿事实之间的知识关联扩散，避免归属边造成全图爆炸
    levels: dict[str, int] = {node_id: 0}  # 节点到跳数的映射
    queue: deque[str] = deque([node_id])  # 从中心开始
    while queue:  # 逐层扩散
        cur = queue.popleft()  # 取当前节点
        if levels[cur] >= use_depth:  # 已达跳数上限
            continue  # 不再扩散
        for nxt, _edge in adj.get(cur, []):  # 遍历邻居
            if nxt in levels:  # 已记录
                continue  # 跳过
            levels[nxt] = levels[cur] + 1  # 记下跳数
            queue.append(nxt)  # 入队继续扩散
    limit = int(conf["max_nodes"])  # 单次返回上限
    ids = list(levels.keys())  # 子图节点编号
    chosen = set(ids[:limit])  # 截断后的节点集合
    inner = [dict(e) for e in graph["edges"] if str(e["src"]) in chosen and str(e["dst"]) in chosen]  # 只留子图内部关联
    edge_total = len(inner)  # 截断前总数，便于人工核对
    edges = inner[:int(conf["max_edges"])]  # 按配置上限截断关联条数
    nodes = [_public_node(table[nid], can_view) for nid in ids[:limit] if nid in table]  # 按权限裁剪节点
    for node in nodes:  # 给每个节点附跳数
        node["depth"] = levels[node["id"]]  # 便于人工核对扩散范围
    return {"nodes": nodes, "edges": edges, "edge_total": edge_total, "depth": use_depth, "truncated": len(ids) > limit or edge_total > len(edges)}  # 返回子图


def evidence_chain(cfg: dict, seeds: list[str], depth: int, can_view: bool = False) -> dict:  # 生成多跳证据链
    """从检索命中的事实出发，沿知识关联扩展出多跳证据链。
    入参：cfg 配置字典；seeds 检索命中的事实编号列表；depth 扩展跳数；can_view 是否可看原文。
    出参：含 hops（逐跳关联）/ related（关联事实编号）/ nodes / truncated 的字典。
    失败时的行为：图谱缺失抛 GRAPH_MISSING；无有效种子返回空链；跳数与节点数按配置上限截断。
    """
    conf = graph_conf(cfg)  # 取跳数与节点上限
    graph = load(cfg)  # 读图谱
    table = _table(graph)  # 建节点索引
    valid = [s for s in seeds if s in table]  # 只保留图中存在的种子
    if not valid:  # 没有可展开的起点
        return {"hops": [], "related": [], "nodes": [], "truncated": False}  # 返回空链，不中断主链路
    max_depth = max(0, min(int(depth), int(conf["max_depth"])))  # 压到配置上限
    adj = _neighbors(graph, True)  # 只沿事实与事实之间的知识关联扩散
    levels: dict[str, int] = {s: 0 for s in valid}  # 种子跳数为 0
    queue: deque[str] = deque(valid)  # 多源广度优先
    hops: list[dict] = []  # 逐跳关联容器
    seen_edges: set[tuple[str, str, str]] = set()  # 关联去重集合
    while queue:  # 逐层扩散
        cur = queue.popleft()  # 取当前节点
        if levels[cur] >= max_depth:  # 已达跳数上限
            continue  # 不再扩散
        for nxt, edge in adj.get(cur, []):  # 遍历邻居
            key = (cur, nxt, str(edge["rel"]))  # 去重键
            if key not in seen_edges:  # 首次走到这条关联
                seen_edges.add(key)  # 标记已记录
                hops.append({"from": cur, "to": nxt, "rel": str(edge["rel"]), "note": str(edge.get("note", "")), "depth": levels[cur] + 1})  # 记一跳
            if nxt in levels:  # 已记录节点
                continue  # 跳过，避免环路
            levels[nxt] = levels[cur] + 1  # 记下跳数
            queue.append(nxt)  # 入队继续扩散
    limit = int(conf["max_nodes"])  # 单次返回节点上限
    max_hops = int(conf["max_hops"])  # 单次返回逐跳关联上限
    hop_total = len(hops)  # 截断前逐跳总数
    hops.sort(key=lambda x: (x["depth"], x["rel"], x["from"], x["to"]))  # 先按跳数再按语义排序，便于人工核对
    hops = hops[:max_hops]  # 截断，避免把全图当证据链
    ids = list(levels.keys())  # 全部可达节点
    chosen = ids[:limit]  # 截断后的节点编号
    related = [nid for nid in chosen if table.get(nid, {}).get("type") == "fact" and nid not in set(valid)]  # 关联到的事实编号
    nodes = [_public_node(table[nid], can_view) for nid in chosen if nid in table]  # 按权限裁剪节点
    return {"hops": hops, "hop_total": hop_total, "related": related, "nodes": nodes, "truncated": len(ids) > limit or hop_total > max_hops}  # 返回多跳证据链


def add_link(cfg: dict, src: str, dst: str, rel: str, note: str, actor: str, ticket_id: str) -> dict:  # 人工确认后写入知识关联
    """把人工核对过的新知识关联追加到图谱并原子落盘。
    入参：cfg 配置字典；src / dst 两个事实编号；rel 关系语义；note 核对依据；actor 签核人；ticket_id 已确认单据编号。
    出参：含 link 与 stats 的字典。
    失败时的行为：端点不存在抛 GRAPH_NODE_NOT_FOUND；三元组已存在抛 GRAPH_LINK_EXISTS；磁盘异常原样抛出。
    """
    graph = load(cfg)  # 读图谱
    table = _table(graph)  # 建节点索引
    if src not in table or dst not in table:  # 端点不存在
        raise BizError("GRAPH_NODE_NOT_FOUND", "待登记关联的知识节点不存在")  # 拒绝悬空关联
    if table[src].get("type") != "fact" or table[dst].get("type") != "fact":  # 只允许事实之间登记关联
        raise BizError("GRAPH_NODE_NOT_FOUND", "只能在事实知识之间登记关联")  # 拒绝把归属边混进来
    edges: list[dict] = [dict(e) for e in graph["edges"]]  # 复制关联表
    if any(str(e["src"]) == src and str(e["dst"]) == dst and str(e["rel"]) == rel for e in edges):  # 三元组重复
        raise BizError("GRAPH_LINK_EXISTS", "该知识关联已存在，不重复登记")  # 保持图谱可核对
    link = {"src": src, "dst": dst, "rel": rel, "note": note, "confirmed_by": actor, "ticket_id": ticket_id}  # 新关联带签核人与单据编号
    edges.append(link)  # 追加关联
    graph["edges"] = edges  # 写回内存图谱
    graph["stats"] = _summarize(graph)  # 重算统计
    write_json(resolve_path(graph_conf(cfg)["path"]), graph)  # 原子落盘，重启后自动回载
    return {"link": {k: link[k] for k in ("src", "dst", "rel", "note")}, "stats": graph["stats"]}  # 只回可核对字段
