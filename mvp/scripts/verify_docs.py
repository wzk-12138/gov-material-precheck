"""交付文档一致性校验：三份 docs 与 config.yaml、源码约定保持同步。"""  # 模块说明：只用标准库 + 项目自带配置加载器，不装 Word 解析库
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import re  # 标准库：抽取 Markdown 二级标题
import sys  # 标准库：回传退出码并加入模块搜索路径
import zipfile  # 标准库：直接读取 docx 内部 XML
import xml.etree.ElementTree as ET  # 标准库：解析 Word 正文文本节点
from pathlib import Path  # 标准库：定位项目根与文档路径

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径，保证直接执行脚本也能导入 app
from app.common import load_config  # 唯一配置入口：模型名与提示词只从 config.yaml 取

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"  # Word 正文命名空间
RESULTS: list[tuple[str, bool]] = []  # 用例结果容器：(名称, 是否通过)


def check(name: str, passed: bool, detail: str = "") -> None:  # 记账并打印一条用例
    """记录一个一致性用例的结果。
    入参：name 用例名；passed 是否通过；detail 可选补充信息。
    出参：无。
    失败时的行为：不抛异常，只记为失败，保证全部用例跑完。
    """
    RESULTS.append((name, bool(passed)))  # 记账
    print(f"[{'PASS' if passed else 'FAIL'}] {name}{' -> ' + detail if detail else ''}")  # 打印单行结果


def docx_text(path: Path) -> tuple[str, list[str]]:  # 读取 Word 正文
    """从 docx 的 document.xml 提取全文与按块切分后的文本列表。
    入参：path docx 文件路径。
    出参：(全文字符串, 块文本列表)。
    失败时的行为：文件缺失或 XML 损坏时抛异常，由调用方统一记为失败。
    """
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))  # 解析正文 XML
    blocks = ["".join((e.text or "") for e in c.iter(W + "t")) for c in root.find(W + "body")]  # 按块取文本
    return "".join(blocks), blocks  # 返回全文与块列表


def main() -> int:  # 文档一致性主流程
    """校验三份交付文档的存在性、内容完整性、与配置同步及代码规范。
    入参：无。
    出参：进程退出码，0 表示全部通过、1 表示有不一致。
    失败时的行为：配置不可读或 docx 不可解析时打印异常并返回 1，不修改任何文件。
    """
    docs = ROOT / "docs"  # 文档目录
    md = docs / "技术方案.md"  # Markdown 版技术方案
    docx = docs / "技术方案.docx"  # Word 版技术方案
    prompt = docs / "提示词.md"  # 提示词设计
    cfg = load_config()  # 读唯一配置，失败直接暴露
    names = [md, docx, prompt]  # 白板要求交付的三份文档
    check("D01 三份交付文档齐全", all(p.is_file() for p in names), "、".join(p.name for p in names))  # 存在性断言
    try:  # 捕获 Word 文件损坏
        text, blocks = docx_text(docx)  # 读取 Word 正文
    except Exception as exc:  # 解析失败
        print(f"[FAIL] 技术方案.docx 无法解析：{type(exc).__name__}")  # 打印原因
        return 1  # 直接失败
    title = "办事材料预检智能体 · 技术方案"  # 文档标题
    title_count = sum(1 for b in blocks if b.strip() == title)  # 标题块出现次数
    check("D02 Word 标题只出现一次且 17 章齐全", title_count == 1 and "17 验收对照" in text, f"标题 {title_count} 次")  # 防重复插入
    stack = ["FastAPI", "Python 3.11+", "Harness", "知识图谱", "Ollama"]  # 白板五项技术栈
    miss_stack = [k for k in stack if k not in text]  # Word 中缺失的技术栈项
    check("D03 Word 覆盖白板五项技术栈", not miss_stack, "、".join(miss_stack) or "全部命中")  # 内容完整断言
    md_text = md.read_text(encoding="utf-8")  # 读 Markdown 全文
    prompt_text = prompt.read_text(encoding="utf-8")  # 读提示词全文
    model = str(cfg["ollama"]["model"])  # 配置里的本机模型名
    stale = "qwen2.5:7b"  # 旧配置里未拉取的模型名，文档不得残留
    synced = model in md_text and model in prompt_text and model in text and stale not in md_text + prompt_text + text  # 配置与三份文档一致性
    check("D04 模型名在配置与三份文档一致且无旧值", synced, model)  # 配置与文档同步断言
    system_prompt = str(cfg["agent"]["system_prompt"])  # 系统提示词唯一来源
    hint = str(cfg["agent"]["no_evidence_hint"])  # 兜底提示唯一来源
    check("D05 提示词.md 逐字引用系统提示词与兜底提示", system_prompt in prompt_text and hint in prompt_text)  # 逐字引用断言
    heads = re.findall(r"^##\s+(.+)$", md_text, flags=re.M)  # 抽取 Markdown 二级标题
    missing_heads = [h for h in heads if h.strip() not in text]  # Word 中缺失的章节
    check("D06 Word 与 Markdown 章节一致", not missing_heads, f"缺 {len(missing_heads)} 章")  # 版本同步断言
    config_lines = (ROOT / "config.yaml").read_text(encoding="utf-8").splitlines()  # 配置原文
    req_lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()  # 依赖原文
    commented = all("#" in line for line in config_lines + req_lines if line.strip())  # 每行都要带注释
    check("D07 config.yaml 与 requirements.txt 每行带注释", commented)  # 代码规范硬约束
    exam_words = ["examcoo", "考试酷", "1139295"]  # 考试提交平台相关内容，不属于本项目交付
    check("D08 交付文档未混入考试平台内容", not any(w in md_text + prompt_text + text for w in exam_words))  # 边界断言
    passed = sum(1 for _, p in RESULTS if p)  # 统计通过数
    print(f"[RESULT] {passed}/{len(RESULTS)} 用例通过")  # 打印总结果
    return 0 if passed == len(RESULTS) else 1  # 全通过才返回 0


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(main())  # 用返回值作为进程退出码
