"""把技术方案.md 转换为带政务风排版的 docx：文档维护工具，不写入 requirements.txt。"""  # 模块说明：依赖 python-docx / markdown / beautifulsoup4，仅本机维护文档时使用
from __future__ import annotations  # 启用延迟注解，兼容低版本类型写法

import re  # 标准库：解析 Markdown 表格分隔行
import sys  # 标准库：加入模块搜索路径与回传退出码
from pathlib import Path  # 标准库：定位项目根与文档路径

from docx import Document  # 文档生成库：构造 Word 文档
from docx.enum.table import WD_TABLE_ALIGNMENT  # 表格对齐枚举
from docx.enum.text import WD_ALIGN_PARAGRAPH  # 段落对齐枚举
from docx.oxml import OxmlElement  # 底层 XML：设置中文字体与页脚页码
from docx.oxml.ns import qn  # 命名空间限定名
from docx.shared import Pt, RGBColor  # 字号与颜色

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))  # 加入搜索路径

MD = ROOT / "docs" / "技术方案.md"  # 源文件：Markdown 版技术方案
DOCX = ROOT / "docs" / "技术方案.docx"  # 目标文件：Word 版技术方案


def set_cn_font(run, name: str = "宋体", size: int = 11, bold: bool = False, color: str | None = None) -> None:  # 设置中文字体
    """给一个文本片段设置中文字体、字号、加粗与颜色。
    入参：run 文本片段；name 字体名；size 字号磅值；bold 是否加粗；color 十六进制颜色。
    出参：无，就地修改 run 样式。
    失败时的行为：无失败分支；颜色为空时不设置，沿用默认。
    """
    run.font.name = name  # 西文字体名
    run.font.size = Pt(size)  # 字号
    run.font.bold = bold  # 加粗开关
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)  # 中文字体单独设置，避免中文回退
    if color:  # 指定颜色
        run.font.color.rgb = RGBColor.from_string(color)  # 设置十六进制颜色


def add_page_number(paragraph) -> None:  # 在段落插入 PAGE 域实现页码
    """给页脚段落插入 Word 的 PAGE 域，实现自动页码。
    入参：paragraph 页脚段落。
    出参：无。
    失败时的行为：无失败分支，纯粹写入域代码。
    """
    run = paragraph.add_run()  # 新建文本片段承载域
    fld_char1 = OxmlElement("w:fldChar")  # 域开始字符
    fld_char1.set(qn("w:fldCharType"), "begin")  # 标记为开始
    instr = OxmlElement("w:instrText")  # 域指令
    instr.set(qn("xml:space"), "preserve")  # 保留空白
    instr.text = "PAGE"  # 页码域
    fld_char2 = OxmlElement("w:fldChar")  # 域结束字符
    fld_char2.set(qn("w:fldCharType"), "end")  # 标记为结束
    run._r.append(fld_char1)  # 追加开始字符
    run._r.append(instr)  # 追加指令
    run._r.append(fld_char2)  # 追加结束字符
    set_cn_font(run, "宋体", 9)  # 页码用小号宋体


def is_table_sep(line: str) -> bool:  # 判定 Markdown 表格分隔行
    """判断一行是否为 Markdown 表格的分隔行（由短横线与竖线组成）。
    入参：line 原始行文本。
    出参：bool 是否为分隔行。
    失败时的行为：无失败分支，纯正则匹配。
    """
    return bool(re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", line)) and "-" in line  # 含短横线且仅由表格语法字符组成


def split_row(line: str) -> list[str]:  # 拆分 Markdown 表格行
    """把一个 Markdown 表格行拆成单元格文本列表。
    入参：line 形如 | a | b | 的行。
    出参：单元格文本列表。
    失败时的行为：无失败分支；首尾竖线被去掉，单元格去空白。
    """
    return [c.strip() for c in line.strip().strip("|").split("|")]  # 去首尾竖线后按竖线切分


def inline_text(text: str) -> str:  # 去掉行内 Markdown 标记
    """去掉反引号、加粗、链接等行内标记，保留可读文本。
    入参：text 含行内 Markdown 标记的字符串。
    出参：去掉标记后的纯文本。
    失败时的行为：无失败分支，纯字符串替换。
    """
    text = re.sub(r"`([^`]*)`", r"\1", text)  # 去行内代码反引号
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)  # 去加粗标记
    text = re.sub(r"\*([^*]*)\*", r"\1", text)  # 去斜体标记
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 去链接标记，保留文字
    return text  # 返回纯文本


def add_table(doc: Document, rows: list[list[str]]) -> None:  # 添加一个表格
    """把若干行文本渲染成带表头样式的 Word 表格。
    入参：doc 文档对象；rows 二维文本列表，第一行为表头。
    出参：无。
    失败时的行为：空行列表直接返回，不添加空表。
    """
    if not rows:  # 无内容
        return  # 不添加空表
    cols = max(len(r) for r in rows)  # 列数取最长行
    table = doc.add_table(rows=0, cols=cols)  # 建空表
    table.style = "Table Grid"  # 网格边框样式
    table.alignment = WD_TABLE_ALIGNMENT.CENTER  # 表格居中
    for idx, row in enumerate(rows):  # 逐行填充
        cells = table.add_row().cells  # 新增一行
        for ci in range(cols):  # 逐列填充
            cell = cells[ci]  # 取单元格
            txt = inline_text(row[ci]) if ci < len(row) else ""  # 取单元格文本，缺列填空
            cell.text = ""  # 清空默认段落
            para = cell.paragraphs[0]  # 取首段落
            run = para.add_run(txt)  # 写入文本
            set_cn_font(run, "宋体", 9, bold=(idx == 0))  # 表头加粗，正文常规
            if idx == 0:  # 表头行
                shd = OxmlElement("w:shd")  # 底纹元素
                shd.set(qn("w:fill"), "D9E2F3")  # 浅蓝底纹
                cell._tc.get_or_add_tcPr().append(shd)  # 应用底纹


def convert() -> int:  # 转换主流程
    """读取 Markdown 并生成带政务风排版的 Word 文档。
    入参：无。
    出参：进程退出码，0 成功、1 失败。
    失败时的行为：源文件缺失抛 FileNotFoundError；生成异常时打印原因并返回 1。
    """
    if not MD.is_file():  # 源文件不存在
        print(f"[FAIL] 源文件不存在：{MD}")  # 打印可操作信息
        return 1  # 失败退出
    lines = MD.read_text(encoding="utf-8").splitlines()  # 逐行读取 Markdown
    doc = Document()  # 新建 Word 文档
    normal = doc.styles["Normal"]  # 正文样式
    normal.font.name = "宋体"  # 正文西文字体
    normal.font.size = Pt(11)  # 正文字号
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")  # 正文中文字体
    footer = doc.sections[0].footer.paragraphs[0]  # 页脚段落
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER  # 页码居中
    footer.add_run("第 ")  # 页码前缀
    set_cn_font(footer.runs[-1], "宋体", 9)  # 前缀样式
    add_page_number(footer)  # 插入页码域
    footer.add_run(" 页")  # 页码后缀
    set_cn_font(footer.runs[-1], "宋体", 9)  # 后缀样式
    i = 0  # 行游标
    in_code = False  # 代码块标记
    code_buf: list[str] = []  # 代码块累积
    table_buf: list[list[str]] = []  # 表格行累积
    title_done = False  # 文档主标题是否已写入（确保只写一次）

    def flush_table() -> None:  # 落盘累积的表格
        """把累积的表格行写入文档并清空缓冲。
        入参：无，使用闭包内的 table_buf。
        出参：无。
        失败时的行为：缓冲为空时不添加表格。
        """
        nonlocal table_buf  # 声明修改外层变量
        if table_buf:  # 有累积内容
            add_table(doc, table_buf)  # 写入表格
            table_buf = []  # 清空缓冲

    while i < len(lines):  # 逐行解析
        raw = lines[i]  # 当前行
        stripped = raw.strip()  # 去首尾空白
        if stripped.startswith("```"):  # 代码块边界
            if in_code:  # 结束代码块
                p = doc.add_paragraph()  # 新建段落承载代码
                run = p.add_run("\n".join(code_buf))  # 写入累积代码
                set_cn_font(run, "Consolas", 9)  # 等宽字体
                p.paragraph_format.left_indent = Pt(12)  # 代码缩进
                code_buf = []  # 清空代码缓冲
                in_code = False  # 退出代码块状态
            else:  # 开始代码块
                flush_table()  # 先落盘之前的表格
                in_code = True  # 进入代码块状态
            i += 1  # 移到下一行
            continue  # 跳过后续处理
        if in_code:  # 代码块内部
            code_buf.append(raw)  # 累积原始代码行
            i += 1  # 移到下一行
            continue  # 不解析 Markdown 语法
        if not stripped:  # 空行
            flush_table()  # 落盘之前累积的表格
            i += 1  # 移到下一行
            continue  # 跳过空行
        if stripped.startswith("---"):  # 水平分割线
            flush_table()  # 落盘表格
            i += 1  # 移到下一行
            continue  # 不输出分割线
        if stripped.startswith("|"):  # 表格行
            if i + 1 < len(lines) and is_table_sep(lines[i + 1]):  # 下一行是分隔行，当前是表头
                table_buf.append(split_row(stripped))  # 收表头
                i += 2  # 跳过分隔行
                continue  # 继续收表格数据行
            table_buf.append(split_row(stripped))  # 收数据行
            i += 1  # 移到下一行
            continue  # 继续收表格
        flush_table()  # 非表格行前先落盘表格
        if stripped.startswith("# "):  # 一级标题（文档主标题）
            text = inline_text(stripped[2:])  # 去标记
            if not title_done:  # 主标题只写一次，避免 D02 断言失败
                p = doc.add_paragraph()  # 新建标题段落
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER  # 主标题居中
                run = p.add_run(text)  # 写入标题文本
                set_cn_font(run, "黑体", 20, bold=True, color="1F3864")  # 政务蓝主标题样式
                title_done = True  # 标记已写
            i += 1  # 移到下一行
            continue  # 跳过后续处理
        if stripped.startswith("## "):  # 二级标题（章节）
            text = inline_text(stripped[3:])  # 去标记
            p = doc.add_paragraph()  # 新建章节段落
            run = p.add_run(text)  # 写入章节标题
            set_cn_font(run, "黑体", 14, bold=True, color="2E5496")  # 政务蓝章节样式
            i += 1  # 移到下一行
            continue  # 跳过后续处理
        if stripped.startswith("### "):  # 三级标题（小节）
            text = inline_text(stripped[4:])  # 去标记
            p = doc.add_paragraph()  # 新建小节段落
            run = p.add_run(text)  # 写入小节标题
            set_cn_font(run, "黑体", 12, bold=True)  # 小节样式
            i += 1  # 移到下一行
            continue  # 跳过后续处理
        if stripped.startswith("> "):  # 引用说明段
            text = inline_text(stripped[2:])  # 去标记
            p = doc.add_paragraph()  # 新建引用段落
            run = p.add_run(text)  # 写入引用文本
            set_cn_font(run, "楷体", 10, color="595959")  # 楷体灰色引用样式
            i += 1  # 移到下一行
            continue  # 跳过后续处理
        m = re.match(r"^(\d+)\.\s+(.*)$", stripped)  # 有序列表项
        if m:  # 命中列表项
            text = inline_text(m.group(2))  # 去标记
            p = doc.add_paragraph(style="List Number")  # 有序列表样式
            run = p.add_run(text)  # 写入列表文本
            set_cn_font(run, "宋体", 11)  # 列表正文字体
            i += 1  # 移到下一行
            continue  # 跳过后续处理
        if stripped.startswith("- "):  # 无序列表项
            text = inline_text(stripped[2:])  # 去标记
            p = doc.add_paragraph(style="List Bullet")  # 无序列表样式
            run = p.add_run(text)  # 写入列表文本
            set_cn_font(run, "宋体", 11)  # 列表正文字体
            i += 1  # 移到下一行
            continue  # 跳过后续处理
        p = doc.add_paragraph()  # 普通正文段落
        run = p.add_run(inline_text(stripped))  # 写入正文
        set_cn_font(run, "宋体", 11)  # 正文样式
        i += 1  # 移到下一行
    flush_table()  # 落盘结尾可能的表格
    if in_code and code_buf:  # 文件以代码块结尾未闭合
        p = doc.add_paragraph()  # 新建段落
        run = p.add_run("\n".join(code_buf))  # 写入剩余代码
        set_cn_font(run, "Consolas", 9)  # 等宽字体
    doc.save(DOCX)  # 保存到目标路径
    print(f"[OK] 已生成 Word 文档：{DOCX}")  # 打印成功信息
    return 0  # 成功退出


if __name__ == "__main__":  # 直接执行入口
    raise SystemExit(convert())  # 用返回值作为进程退出码
