"""本地知识库：从合成训练资料建库，用标准库做相关度检索。"""  # 模块说明：不引入向量库、分词库或任何中间件
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import json  # 标准库：读取合成资料与本地规则库
import math  # 标准库：IDF 对数与余弦模长
import re  # 标准库：词元切分与红线正则
from collections import Counter  # 标准库：词频与文档频率统计
from typing import Any  # 标准库：规则条目类型标注
from .common import BizError, resolve_path, write_json  # 统一异常、路径解析与原子写


def _short_id(full: str) -> str:  # 证据编号缩写
    """把长证据编号缩成 fxx 形式。
    入参：full 完整编号，例如 government-service-f00。
    出参：短编号 f00；不符合形式时原样返回。
    失败时的行为：无失败分支；空字符串原样返回。
    """
    tail = full.rsplit("-", 1)[-1]  # 取最后一段
    return tail if tail.startswith("f") else full  # 形如 f00 才缩写，否则保留全名


def build_rules(cfg: dict) -> dict:  # 规则库构建
    """从合成训练资料构建本地规则库并落盘。
    入参：cfg 配置字典，knowledge.sources 指向合成资料文件。
    出参：规则库 dict，含 rule_version、items、skipped；同时写入 knowledge.kb_path。
    失败时的行为：源文件缺失抛 SOURCE_MISSING，结构异常抛 SOURCE_INVALID，抽不到条目抛 SOURCE_INVALID；命中个人材料特征的条目只计数不入库。
    """
    items: list[dict[str, Any]] = []  # 规则条目容器
    versions: list[str] = []  # 源资料版本容器，用于生成可核对的资料版本
    seen: set[str] = set()  # 证据编号去重集合
    patterns = [re.compile(p) for p in cfg["redlines"]["personal_data_patterns"]]  # 预编译数据红线正则
    skipped = 0  # 被红线跳过的条目计数，只记数量不记原文
    for rel in cfg["knowledge"]["sources"]:  # 逐个读取配置登记的合成资料
        path = resolve_path(rel)  # 相对路径挂到项目根目录
        if not path.exists():  # 源资料缺失
            raise BizError("SOURCE_MISSING", f"规则源文件不存在：{path}")  # 明确失败，不用空库顶替
        try:  # 捕获 JSON 损坏或编码问题
            doc = json.loads(path.read_text(encoding="utf-8"))  # 标准库读取合成资料
        except Exception as exc:  # 解析失败
            raise BizError("SOURCE_INVALID", f"规则源解析失败：{exc}") from exc  # 转成统一业务错误
        versions.append(str(doc.get("generatedAt", path.name)))  # 记录资料版本时间，供审计核对
        reqs = doc.get("requirements", [])  # 只取需求假设下的访谈事实，不读逐字稿
        if not isinstance(reqs, list):  # 结构异常
            raise BizError("SOURCE_INVALID", "requirements 字段必须是列表")  # 拒绝非预期结构
        for req in reqs:  # 遍历每条需求假设
            topic = str(req.get("title", ""))  # 需求标题作为检索上下文
            for ev in req.get("evidence", []):  # 遍历每条访谈事实
                eid = str(ev.get("id", "")).strip()  # 事实编号
                text = str(ev.get("text", "")).strip()  # 事实正文
                if not eid or not text or eid in seen:  # 空编号、空正文或已入库
                    continue  # 跳过该条，保证一编号一条目
                if any(p.search(text) for p in patterns):  # 命中真实个人材料特征
                    # 为什么这么卡：数据红线规定只用这批合成训练资料作规则源，疑似真实个人材料一律不入库
                    skipped += 1  # 只累计数量，绝不保存原文
                    continue  # 跳过该条
                seen.add(eid)  # 标记已入库
                items.append({"id": eid, "short_id": _short_id(eid), "source": str(ev.get("source", "")), "character": str(ev.get("character", "")), "topic": topic, "text": text})  # 组装可核对条目
    if not items:  # 一条都没抽到
        raise BizError("SOURCE_INVALID", "未从合成资料抽取到任何规则条目")  # 拒绝生成空知识库
    payload = {"rule_version": "|".join(versions), "source_files": list(cfg["knowledge"]["sources"]), "source_note": "仅合成训练资料，不含任何真实个人材料", "count": len(items), "skipped": skipped, "items": items}  # 组装规则库
    write_json(resolve_path(cfg["knowledge"]["kb_path"]), payload)  # 原子写入本地 kb/rules.json
    return payload  # 返回内存中的规则库


def tokenize(text: str) -> list[str]:  # 纯标准库词元切分
    """中文按单字与相邻二字切分，英文数字按词切分。
    入参：text 任意中英文文本。
    出参：词元列表。
    失败时的行为：无失败分支；空文本返回空列表。
    """
    cleaned = re.sub(r"\s+", " ", text.lower())  # 统一小写并压缩空白
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", cleaned)  # 英文数字成词，中文拆成单字
    out = list(tokens)  # 先保留单字 / 单词词元
    for i in range(len(tokens) - 1):  # 构造相邻二元词，提升中文区分度
        a, b = tokens[i], tokens[i + 1]  # 取相邻两个词元
        if len(a) == 1 and len(b) == 1 and "\u4e00" <= a <= "\u9fff" and "\u4e00" <= b <= "\u9fff":  # 仅中文单字相邻才组合
            out.append(a + b)  # 加入二元词元
    return out  # 返回词元列表


def search(cfg: dict, query: str, top_k: int = 0) -> list[dict]:  # 本地标准库检索
    """按 IDF 加权余弦相关度检索规则库，返回带证据编号的条目。
    入参：cfg 配置字典；query 查询文本；top_k 为 0 时用配置里的 knowledge.top_k。
    出参：按相关度降序的条目列表，每条附 score。
    失败时的行为：规则库未生成抛 BizError(KB_MISSING)；空查询返回空列表；规则库损坏抛 BizError(STORE_READ_FAILED)。
    """
    path = resolve_path(cfg["knowledge"]["kb_path"])  # 规则库路径只从配置读取
    if not path.exists():  # 尚未执行 build_kb.py
        raise BizError("KB_MISSING", "规则库未生成，请先运行 python scripts/build_kb.py")  # 给出可操作指引
    try:  # 捕获规则库损坏
        payload = json.loads(path.read_text(encoding="utf-8"))  # 标准库读取本地文件
    except Exception as exc:  # 解析失败
        raise BizError("STORE_READ_FAILED", f"规则库读取失败：{exc}") from exc  # 转成统一业务错误
    items = payload.get("items", [])  # 取出规则条目
    if not query.strip():  # 空查询无需计算
        return []  # 直接返回空列表
    k = top_k or int(cfg["knowledge"]["top_k"])  # 本次返回条数
    q_tokens = Counter(tokenize(query))  # 查询词频
    docs = [Counter(tokenize(str(it["text"]) + " " + str(it["topic"]))) for it in items]  # 每条规则的词频
    df: Counter = Counter()  # 文档频率统计
    for d in docs:  # 遍历全部文档词频
        for t in d:  # 遍历该文档词元
            df[t] += 1  # 累加出现该词元的文档数
    n = len(items)  # 文档总数
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}  # 平滑 IDF，只用标准库 math
    q_vec = {t: c * idf.get(t, 1.0) for t, c in q_tokens.items()}  # 查询向量
    q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0  # 查询向量模长，避免除零
    scored: list[dict] = []  # 结果容器
    for idx, d in enumerate(docs):  # 逐条打分
        dot = 0.0  # 内积累加
        d_norm = 0.0  # 文档模长平方累加
        for t, c in d.items():  # 遍历文档词元
            w = c * idf.get(t, 1.0)  # 文档词元权重
            d_norm += w * w  # 累加模长平方
            if t in q_vec:  # 命中查询词元
                dot += q_vec[t] * w  # 累加内积
        score = dot / (q_norm * (math.sqrt(d_norm) or 1.0))  # 余弦相关度
        if score >= float(cfg["knowledge"]["min_score"]):  # 低于阈值视为无依据
            row = dict(items[idx])  # 复制条目，避免改动库内数据
            row["score"] = round(score, 4)  # 附相关度，便于人工核对
            scored.append(row)  # 收入候选
    scored.sort(key=lambda r: r["score"], reverse=True)  # 按相关度降序
    return scored[:k]  # 返回前 k 条
