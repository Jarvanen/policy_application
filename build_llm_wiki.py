#!/usr/bin/env python3
"""
Build a persistent Markdown wiki from the Xiongan policy corpus.

This script does not call an LLM. It compiles the already-generated policy
Markdown, declaration guides, and policy_rules.jsonl into an Obsidian-friendly
wiki that can be searched and used by ask_policy_wiki.py.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_DIR = Path("data/xiongan_policy_files")
DEFAULT_RULES_FILE = DEFAULT_OUTPUT_DIR / "policy_rules.jsonl"
DEFAULT_POLICY_MD_DIR = DEFAULT_OUTPUT_DIR / "policy_md"
DEFAULT_GUIDE_DIR = DEFAULT_OUTPUT_DIR / "declaration_guides"
DEFAULT_WIKI_DIR = DEFAULT_OUTPUT_DIR / "llm_wiki"


CATEGORY_KEYWORDS = {
    "软件/人工智能/数字技术": [
        "软件开发",
        "人工智能",
        "大模型",
        "数字技术",
        "信息系统",
        "新一代信息技术",
        "卫星互联网",
        "量子计算",
        "鸿蒙",
        "RISC-V",
    ],
    "科技服务/研发": [
        "技术服务",
        "技术开发",
        "技术咨询",
        "科技服务",
        "成果转化",
        "技术转移",
        "研发",
        "科学研究",
        "服务外包",
        "信息技术外包",
    ],
    "专精特新/中小企业": ["专精特新", "小巨人", "中小企业", "创新型中小企业"],
    "高新技术/科技型企业": ["高新技术企业", "科技型中小企业", "科技企业"],
    "知识产权/标准": ["知识产权", "专利", "标准", "计量技术规范"],
    "制造业/工业": ["制造业", "高端高新制造", "先进制造", "智能制造", "工业企业", "工业诊断", "工业诊所", "技改"],
    "机器人/智能网联": ["机器人", "智能网联汽车", "机器人化"],
    "生命科学/医药医疗": ["生命科学", "生物技术", "生物医药", "医疗器械", "数字医疗", "药品"],
    "新材料": ["新材料", "材料"],
    "低空经济": ["低空经济", "航空", "无人机"],
    "物流/供应链": ["物流", "仓储", "供应链", "快递"],
    "外贸/外资": ["外贸", "跨境", "出口", "进口", "外资", "外商投资"],
    "会展/文旅": ["会展", "展会", "会议", "旅游", "文化", "体育"],
    "金融/上市/贴息": ["金融", "贷款", "贴息", "上市", "挂牌", "融资"],
    "就业/人才": ["人才", "就业", "毕业生", "特岗特薪", "培训", "技术经理人"],
    "孵化载体/平台": ["孵化器", "众创空间", "创新平台", "重点实验室", "技术创新中心", "企业技术中心"],
    "租房/落户/办公": ["租房", "租金", "落户", "办公用房"],
}


APPLICANT_PATTERNS = {
    "企业": ["企业", "公司", "单位", "经营主体", "市场主体"],
    "个人/人才": ["个人", "人员", "人才", "毕业生", "从业者", "负责人", "经理人"],
    "高校/科研院所": ["高校", "科研院所", "学校", "院校"],
    "医疗机构": ["医疗机构", "医院"],
    "政府/事业单位": ["机关", "事业单位", "政府", "部门"],
    "服务机构/载体": ["服务机构", "孵化器", "众创空间", "园区", "载体", "运营主体", "培训机构"],
}


NON_APPLICATION_TITLE_TERMS = ["名单", "公示", "延期", "声明", "公布", "发布认定名单"]

IMPORTANT_TOPICS = {
    "高新技术企业",
    "科技型中小企业",
    "创新型中小企业",
    "专精特新",
    "专精特新中小企业",
    "小巨人",
    "企业技术中心",
    "工业设计中心",
    "孵化器",
    "众创空间",
    "知识产权",
    "专利",
    "标准",
    "技术合同",
    "研发费用",
    "成果转化",
    "技术转移",
    "服务外包",
    "人工智能",
    "新材料",
    "生物医药",
    "医疗器械",
    "机器人",
    "低空经济",
    "物流",
    "会展",
    "上市",
    "贷款贴息",
    "租房补贴",
    "雄安新区",
    "河北省",
}

GENERIC_TOPICS = {
    "企业",
    "单位",
    "公司",
    "机构",
    "有限责任公司",
    "事业单位",
    "高校",
    "科研院所",
    "个人",
    "人员",
    "在雄安新区",
    "在雄安新区实质运营",
    "在雄安新区注册",
}


@dataclass
class PolicyWikiRow:
    rule: dict[str, Any]
    policy_md_path: Path | None
    guide_path: Path | None
    metadata: dict[str, str]
    page_stem: str
    status: str
    is_application: bool
    deadline_dates: list[date]
    categories: list[str]
    applicant_types: list[str]
    topics: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build llm_wiki from Xiongan policy Markdown and rules.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--rules-file", type=Path, default=DEFAULT_RULES_FILE)
    parser.add_argument("--policy-md-dir", type=Path, default=DEFAULT_POLICY_MD_DIR)
    parser.add_argument("--guide-dir", type=Path, default=DEFAULT_GUIDE_DIR)
    parser.add_argument("--wiki-dir", type=Path, default=DEFAULT_WIKI_DIR)
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--clean", action="store_true", help="Delete the existing generated wiki dir before rebuilding.")
    return parser.parse_args()


def read_rules(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if value.get("status") == "error":
            continue
        rows.append(value)
    return rows


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "；".join(normalize_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def md_escape(value: Any) -> str:
    return normalize_text(value).replace("|", "\\|").replace("\n", " ").strip()


def slug(value: str, max_len: int = 90) -> str:
    text = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", value)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:max_len].strip(" .") or "untitled")


def link(folder: str, stem: str, label: str | None = None) -> str:
    if label and label != stem:
        return f"[[{folder}/{stem}|{label}]]"
    return f"[[{folder}/{stem}]]"


def policy_id_from_path(path: Path) -> str | None:
    match = re.match(r"^(\d{3})_", path.name)
    return match.group(1) if match else None


def file_map_by_id(directory: Path, suffix: str = "*.md") -> dict[str, Path]:
    files: dict[str, Path] = {}
    if not directory.exists():
        return files
    for path in sorted(directory.glob(suffix)):
        policy_id = policy_id_from_path(path)
        if policy_id:
            files[policy_id] = path
    return files


def parse_policy_metadata(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")[:5000]
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|$", line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if key in {"字段", "---"}:
            continue
        metadata[key] = value
    return metadata


def parse_iso_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def infer_year(rule: dict[str, Any], metadata: dict[str, str]) -> int | None:
    publish_date = parse_iso_date(metadata.get("发布时间", ""))
    if publish_date:
        return publish_date.year
    source = normalize_text(rule.get("source_guide_path"))
    match = re.search(r"_(20\d{2})-\d{2}-\d{2}_", source)
    if match:
        return int(match.group(1))
    text = " ".join([normalize_text(rule.get("title")), normalize_text(rule.get("deadline"))])
    match = re.search(r"(20\d{2})年", text)
    return int(match.group(1)) if match else None


def extract_deadline_dates(deadline_text: str, fallback_year: int | None) -> list[date]:
    dates: list[date] = []
    for year, month, day in re.findall(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?", deadline_text):
        try:
            dates.append(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    if fallback_year:
        for month, day in re.findall(r"(?<!\d)(\d{1,2})月(\d{1,2})日", deadline_text):
            try:
                candidate = date(fallback_year, int(month), int(day))
            except ValueError:
                continue
            if candidate not in dates:
                dates.append(candidate)
    return sorted(set(dates))


def is_application_policy(rule: dict[str, Any]) -> bool:
    title = normalize_text(rule.get("title"))
    notes = normalize_text(rule.get("notes"))
    if "非常规政策申报" in notes or "非传统资金补贴类政策申报" in notes:
        return False
    if any(term in title for term in NON_APPLICATION_TITLE_TERMS):
        return False
    if any(term in title for term in ["培训会", "培训活动", "研修活动"]) and "申报" not in title:
        return False
    return True


def status_for_policy(rule: dict[str, Any], metadata: dict[str, str], as_of: date) -> tuple[str, list[date]]:
    if not is_application_policy(rule):
        return "非申报类", []
    dates = extract_deadline_dates(normalize_text(rule.get("deadline")), infer_year(rule, metadata))
    if dates and max(dates) < as_of:
        return "已过期", dates
    if dates and max(dates) >= as_of:
        return "当前可关注", dates
    return "未明确期限", []


def categories_for_rule(rule: dict[str, Any]) -> list[str]:
    text = " ".join(
        normalize_text(rule.get(key))
        for key in [
            "title",
            "policy_summary",
            "benefit",
            "applicant_objects",
            "hard_conditions",
            "industry_keywords",
            "company_stage_requirements",
            "notes",
        ]
    )
    categories = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            categories.append(category)
    return categories


def applicant_types_for_rule(rule: dict[str, Any]) -> list[str]:
    text = normalize_text(rule.get("applicant_objects")) or normalize_text(rule.get("title"))
    values = []
    for applicant_type, keywords in APPLICANT_PATTERNS.items():
        if any(keyword in text for keyword in keywords):
            values.append(applicant_type)
    return values or ["未明确"]


def topic_terms_for_rule(rule: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ["industry_keywords", "company_stage_requirements", "region_requirements"]:
        for item in rule.get(key) or []:
            text = normalize_text(item).strip()
            if text and len(text) <= 30:
                terms.append(text)
    for item in rule.get("hard_conditions") or []:
        text = normalize_text(item)
        for term in ["高新技术企业", "科技型中小企业", "专精特新", "小巨人", "企业技术中心", "孵化器", "众创空间", "外商投资企业"]:
            if term in text:
                terms.append(term)
    return sorted(set(terms))


def topic_is_useful(topic: str) -> bool:
    if topic in IMPORTANT_TOPICS:
        return True
    if topic in GENERIC_TOPICS:
        return False
    if "隐含" in topic or "原文未" in topic:
        return False
    if len(topic) > 18:
        return False
    if re.search(r"(以上|以下|不少于|不低于|达到|超过|万元|平方米|日期|时间|账号)", topic):
        return False
    return True


def filter_row_topics(rows: list[PolicyWikiRow]) -> None:
    counts = Counter(topic for row in rows for topic in row.topics if topic_is_useful(topic))
    allowed = {topic for topic, count in counts.items() if count >= 2 or topic in IMPORTANT_TOPICS}
    for row in rows:
        row.topics = [topic for topic in row.topics if topic in allowed]


def relpath(path: Path | None, base: Path) -> str:
    if not path:
        return ""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def yaml_array(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def markdown_list(values: Any, empty: str = "未提取到") -> list[str]:
    if isinstance(values, str):
        values = [values] if values.strip() else []
    if not values:
        return [f"- {empty}"]
    return [f"- {normalize_text(item)}" for item in values if normalize_text(item).strip()]


def make_policy_page(row: PolicyWikiRow, wiki_dir: Path) -> str:
    rule = row.rule
    title = normalize_text(rule.get("title"))
    policy_id = normalize_text(rule.get("policy_id"))
    agency = row.metadata.get("发文单位", "")
    publish_date = row.metadata.get("发布时间", "")
    policy_md_rel = relpath(row.policy_md_path, wiki_dir.parent)
    guide_rel = relpath(row.guide_path, wiki_dir.parent)
    topic_links = [link("topics", slug(topic), topic) for topic in row.topics[:20]]
    industry_links = [link("industries", slug(category), category) for category in row.categories]
    applicant_links = [link("applicant_types", slug(item), item) for item in row.applicant_types]
    agency_link = link("agencies", slug(agency), agency) if agency else ""
    deadline_date_text = "、".join(item.isoformat() for item in row.deadline_dates)

    lines = [
        "---",
        "type: policy",
        f"policy_id: {json.dumps(policy_id, ensure_ascii=False)}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"publish_date: {json.dumps(publish_date, ensure_ascii=False)}",
        f"agency: {json.dumps(agency, ensure_ascii=False)}",
        f"status: {json.dumps(row.status, ensure_ascii=False)}",
        f"is_application: {str(row.is_application).lower()}",
        f"categories: {yaml_array(row.categories)}",
        f"applicant_types: {yaml_array(row.applicant_types)}",
        f"source_policy_md: {json.dumps(policy_md_rel, ensure_ascii=False)}",
        f"source_guide: {json.dumps(guide_rel, ensure_ascii=False)}",
        "---",
        "",
        f"# {policy_id} {title}",
        "",
        "## 快速结论",
        "",
        f"- 状态：{row.status}",
        f"- 是否申报类：{'是' if row.is_application else '否'}",
        f"- 发布时间：{publish_date or '未提取到'}",
        f"- 发文单位：{agency_link or agency or '未提取到'}",
        f"- 截止日期：{deadline_date_text or normalize_text(rule.get('deadline')) or '未明确'}",
        f"- 产业主题：{'、'.join(industry_links) if industry_links else '未归类'}",
        f"- 申报主体：{'、'.join(applicant_links) if applicant_links else '未明确'}",
        "",
        "## 政策简介",
        "",
        normalize_text(rule.get("policy_summary")) or "未提取到。",
        "",
        "## 支持内容",
        "",
        normalize_text(rule.get("benefit")) or "未提取到。",
        "",
        "## 申请对象",
        "",
        *markdown_list(rule.get("applicant_objects")),
        "",
        "## 申报条件",
        "",
        "### 硬性条件",
        "",
        *markdown_list(rule.get("hard_conditions")),
        "",
        "### 方向性条件",
        "",
        *markdown_list(rule.get("soft_conditions")),
        "",
        "### 排除条件",
        "",
        *markdown_list(rule.get("exclusion_conditions")),
        "",
        "## 材料与流程",
        "",
        "### 申报材料",
        "",
        *markdown_list(rule.get("required_materials")),
        "",
        "### 办理流程",
        "",
        *markdown_list(rule.get("process")),
        "",
        "## 时间与联系方式",
        "",
        f"- 时间：{normalize_text(rule.get('deadline')) or '未提取到'}",
        "",
        *markdown_list(rule.get("contacts"), empty="未提取到联系方式"),
        "",
        "## 匹配时需要补充的信息",
        "",
        *markdown_list(rule.get("missing_info_needed")),
        "",
        "## 关联主题",
        "",
        *([f"- {item}" for item in topic_links] if topic_links else ["- 未归类"]),
        "",
        "## 来源",
        "",
        f"- 原始政策 Markdown：`{policy_md_rel or '未匹配到'}`",
        f"- 申报指南 Markdown：`{guide_rel or '未匹配到'}`",
        f"- 结构化规则：`policy_rules.jsonl` 中 `policy_id={policy_id}`",
        "",
        "## 维护备注",
        "",
        normalize_text(rule.get("notes")) or "无。",
    ]
    return "\n".join(lines).rstrip() + "\n"


def make_collection_page(
    *,
    title: str,
    description: str,
    rows: list[PolicyWikiRow],
    folder: str,
    stem: str,
) -> str:
    status_counts = Counter(row.status for row in rows)
    lines = [
        "---",
        "type: collection",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "---",
        "",
        f"# {title}",
        "",
        description,
        "",
        "## 状态分布",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for status, count in status_counts.most_common():
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## 相关政策", "", "| ID | 状态 | 政策 | 申请对象 | 截止/时间 |", "| --- | --- | --- | --- | --- |"])
    for row in sorted(rows, key=lambda item: normalize_text(item.rule.get("policy_id"))):
        policy_id = normalize_text(row.rule.get("policy_id"))
        title_text = normalize_text(row.rule.get("title"))
        deadline = md_escape(row.rule.get("deadline"))[:120]
        applicants = md_escape(row.rule.get("applicant_objects"))[:120]
        lines.append(
            f"| {policy_id} | {row.status} | {link('policies', row.page_stem, title_text)} | {applicants or '未提取到'} | {deadline or '未明确'} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def make_schema() -> str:
    return """# LLM Wiki Schema

这个目录由脚本维护，用于把雄安政策文件编译成可持续问答的 Markdown wiki。

## 目录约定

- `policies/`：每条政策一个页面，事实来自 `policy_md/`、`declaration_guides/`、`policy_rules.jsonl`。
- `topics/`：政策关键词和资质主题页，例如高新技术企业、专精特新、知识产权。
- `industries/`：产业方向页，例如软件/人工智能/数字技术、生命科学/医药医疗。
- `applicant_types/`：申报主体页，例如企业、个人/人才、高校/科研院所。
- `agencies/`：发文单位页。
- `qa/`：值得沉淀的问答结果。
- `index.md`：内容索引，问答前优先读取。
- `log.md`：构建和问答日志。
- `gaps.md`：缺失字段和需要人工补充的信息。
- `contradictions.md`：冲突、过期和口径差异记录。

## 维护规则

1. 原始来源只读，不修改 `policy_md/`、`declaration_guides/`、`policy_rules.jsonl`。
2. 回答问题时先查 `index.md`，再读相关 wiki 页面；必要时回查原始政策 Markdown。
3. 答案必须给出来源页面或原始文件路径。
4. 如果问题产生了可复用结论，可以写入 `qa/` 并在 `log.md` 追加记录。
5. 不确定的信息要明确标注为“需核实”，不要编造政策条件。
"""


def make_index(rows: list[PolicyWikiRow], generated_at: str) -> str:
    lines = [
        "# 雄安政策 LLM Wiki 索引",
        "",
        f"- 生成时间：{generated_at}",
        f"- 政策数量：{len(rows)}",
        "",
        "## 入口",
        "",
        "- [[overview|政策总览]]",
        "- [[gaps|信息缺口]]",
        "- [[contradictions|冲突与口径差异]]",
        "- [[log|维护日志]]",
        "",
        "## 分类入口",
        "",
        "- [[industries/软件 人工智能 数字技术|软件/人工智能/数字技术]]",
        "- [[topics/高新技术企业|高新技术企业]]",
        "- [[topics/专精特新|专精特新]]",
        "- [[topics/科技型中小企业|科技型中小企业]]",
        "- [[applicant_types/企业|企业]]",
        "",
        "## 全部政策",
        "",
        "| ID | 发布时间 | 状态 | 发文单位 | 政策 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda item: normalize_text(item.rule.get("policy_id"))):
        policy_id = normalize_text(row.rule.get("policy_id"))
        title = normalize_text(row.rule.get("title"))
        publish_date = row.metadata.get("发布时间", "")
        agency = row.metadata.get("发文单位", "")
        lines.append(f"| {policy_id} | {publish_date} | {row.status} | {md_escape(agency)} | {link('policies', row.page_stem, title)} |")
    return "\n".join(lines).rstrip() + "\n"


def make_overview(rows: list[PolicyWikiRow], generated_at: str, as_of: date) -> str:
    status_counts = Counter(row.status for row in rows)
    category_counts = Counter(category for row in rows for category in row.categories)
    applicant_counts = Counter(applicant for row in rows for applicant in row.applicant_types)
    lines = [
        "# 雄安政策总览",
        "",
        f"- 生成时间：{generated_at}",
        f"- 判断日期：{as_of.isoformat()}",
        f"- 政策总数：{len(rows)}",
        "",
        "## 状态分布",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for status, count in status_counts.most_common():
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## 产业主题分布", "", "| 产业主题 | 数量 |", "| --- | ---: |"])
    for category, count in category_counts.most_common():
        lines.append(f"| {link('industries', slug(category), category)} | {count} |")
    lines.extend(["", "## 申报主体分布", "", "| 主体 | 数量 |", "| --- | ---: |"])
    for applicant, count in applicant_counts.most_common():
        lines.append(f"| {link('applicant_types', slug(applicant), applicant)} | {count} |")
    lines.extend(["", "## 当前可关注政策", "", "| ID | 政策 | 截止/时间 |", "| --- | --- | --- |"])
    for row in sorted(rows, key=lambda item: normalize_text(item.rule.get("policy_id"))):
        if row.status != "当前可关注":
            continue
        lines.append(
            f"| {row.rule.get('policy_id')} | {link('policies', row.page_stem, normalize_text(row.rule.get('title')))} | {md_escape(row.rule.get('deadline')) or '未明确'} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def make_gaps(rows: list[PolicyWikiRow]) -> str:
    lines = [
        "# 信息缺口",
        "",
        "这里记录结构化字段中缺失的信息，问答时需要提醒用户补充或回查原文。",
        "",
        "| ID | 政策 | 缺口 |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        rule = row.rule
        gaps = []
        if row.is_application and not normalize_text(rule.get("deadline")):
            gaps.append("缺少申报时间/截止时间")
        if row.is_application and not rule.get("contacts"):
            gaps.append("缺少咨询电话/联系人")
        if row.is_application and not rule.get("applicant_objects"):
            gaps.append("缺少申请对象")
        if row.is_application and not rule.get("required_materials"):
            gaps.append("缺少申报材料")
        for item in rule.get("missing_info_needed") or []:
            gaps.append(normalize_text(item))
        if gaps:
            lines.append(
                f"| {rule.get('policy_id')} | {link('policies', row.page_stem, normalize_text(rule.get('title')))} | {md_escape('；'.join(gaps[:12]))} |"
            )
    return "\n".join(lines).rstrip() + "\n"


def make_contradictions(rows: list[PolicyWikiRow], as_of: date) -> str:
    lines = [
        "# 冲突与口径差异",
        "",
        "当前版本主要记录时间口径和非申报类事项。深度冲突检查可由后续 lint 脚本或人工问答触发。",
        "",
        "## 已过期但仍可能有历史参考价值的政策",
        "",
        "| ID | 政策 | 时间字段 |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        if row.status == "已过期":
            lines.append(
                f"| {row.rule.get('policy_id')} | {link('policies', row.page_stem, normalize_text(row.rule.get('title')))} | {md_escape(row.rule.get('deadline')) or '未明确'} |"
            )
    lines.extend(["", "## 非申报类政策", "", "| ID | 政策 | 备注 |", "| --- | --- | --- |"])
    for row in rows:
        if row.status == "非申报类":
            lines.append(
                f"| {row.rule.get('policy_id')} | {link('policies', row.page_stem, normalize_text(row.rule.get('title')))} | {md_escape(row.rule.get('notes')) or '标题或内容显示为非申报事项'} |"
            )
    lines.append(f"\n判断日期：{as_of.isoformat()}")
    return "\n".join(lines).rstrip() + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_rows(args: argparse.Namespace, as_of: date) -> list[PolicyWikiRow]:
    rules = read_rules(args.rules_file)
    policy_files = file_map_by_id(args.policy_md_dir)
    guide_files = file_map_by_id(args.guide_dir, "*_申报指南.md")
    rows: list[PolicyWikiRow] = []
    seen_stems: set[str] = set()

    for rule in rules:
        policy_id = normalize_text(rule.get("policy_id"))
        title = normalize_text(rule.get("title"))
        policy_md_path = policy_files.get(policy_id)
        guide_path = guide_files.get(policy_id) or Path(normalize_text(rule.get("source_guide_path")))
        if guide_path and not guide_path.exists():
            guide_path = None
        metadata = parse_policy_metadata(policy_md_path)
        status, dates = status_for_policy(rule, metadata, as_of)
        page_stem_base = f"{policy_id}_{slug(title)}"
        page_stem = page_stem_base
        suffix = 2
        while page_stem in seen_stems:
            page_stem = f"{page_stem_base}_{suffix}"
            suffix += 1
        seen_stems.add(page_stem)
        rows.append(
            PolicyWikiRow(
                rule=rule,
                policy_md_path=policy_md_path,
                guide_path=guide_path,
                metadata=metadata,
                page_stem=page_stem,
                status=status,
                is_application=is_application_policy(rule),
                deadline_dates=dates,
                categories=categories_for_rule(rule),
                applicant_types=applicant_types_for_rule(rule),
                topics=topic_terms_for_rule(rule),
            )
        )
    return rows


def main() -> int:
    args = parse_args()
    as_of = parse_iso_date(args.as_of_date)
    if not as_of:
        raise SystemExit(f"Invalid --as-of-date: {args.as_of_date}")

    wiki_dir = args.wiki_dir
    if args.clean and wiki_dir.exists():
        shutil.rmtree(wiki_dir)
    for subdir in ["policies", "topics", "industries", "applicant_types", "agencies", "qa"]:
        (wiki_dir / subdir).mkdir(parents=True, exist_ok=True)

    rows = build_rows(args, as_of)
    filter_row_topics(rows)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for row in rows:
        write_text(wiki_dir / "policies" / f"{row.page_stem}.md", make_policy_page(row, wiki_dir))

    topics: dict[str, list[PolicyWikiRow]] = defaultdict(list)
    industries: dict[str, list[PolicyWikiRow]] = defaultdict(list)
    applicant_types: dict[str, list[PolicyWikiRow]] = defaultdict(list)
    agencies: dict[str, list[PolicyWikiRow]] = defaultdict(list)

    for row in rows:
        for topic in row.topics:
            topics[topic].append(row)
        for category in row.categories or ["未归类"]:
            industries[category].append(row)
        for applicant in row.applicant_types:
            applicant_types[applicant].append(row)
        agency = row.metadata.get("发文单位", "未提取到")
        agencies[agency].append(row)

    for topic, topic_rows in topics.items():
        write_text(
            wiki_dir / "topics" / f"{slug(topic)}.md",
            make_collection_page(title=topic, description=f"与“{topic}”相关的政策汇总。", rows=topic_rows, folder="topics", stem=slug(topic)),
        )
    for category, category_rows in industries.items():
        write_text(
            wiki_dir / "industries" / f"{slug(category)}.md",
            make_collection_page(title=category, description=f"产业方向“{category}”下的政策汇总。", rows=category_rows, folder="industries", stem=slug(category)),
        )
    for applicant, applicant_rows in applicant_types.items():
        write_text(
            wiki_dir / "applicant_types" / f"{slug(applicant)}.md",
            make_collection_page(title=applicant, description=f"申报主体“{applicant}”相关政策。", rows=applicant_rows, folder="applicant_types", stem=slug(applicant)),
        )
    for agency, agency_rows in agencies.items():
        write_text(
            wiki_dir / "agencies" / f"{slug(agency)}.md",
            make_collection_page(title=agency, description=f"发文单位“{agency}”相关政策。", rows=agency_rows, folder="agencies", stem=slug(agency)),
        )

    write_text(wiki_dir / "WIKI_SCHEMA.md", make_schema())
    write_text(wiki_dir / "index.md", make_index(rows, generated_at))
    write_text(wiki_dir / "overview.md", make_overview(rows, generated_at, as_of))
    write_text(wiki_dir / "gaps.md", make_gaps(rows))
    write_text(wiki_dir / "contradictions.md", make_contradictions(rows, as_of))

    manifest_rows = [
        {
            "policy_id": row.rule.get("policy_id"),
            "title": row.rule.get("title"),
            "status": row.status,
            "is_application": row.is_application,
            "page": str(Path("policies") / f"{row.page_stem}.md"),
            "source_policy_md": relpath(row.policy_md_path, wiki_dir.parent),
            "source_guide": relpath(row.guide_path, wiki_dir.parent),
        }
        for row in rows
    ]
    write_text(wiki_dir / "wiki_manifest.json", json.dumps(manifest_rows, ensure_ascii=False, indent=2))

    log_path = wiki_dir / "log.md"
    old_log = log_path.read_text(encoding="utf-8") if log_path.exists() else "# 维护日志\n"
    log_entry = (
        f"\n## [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ingest | build_llm_wiki\n\n"
        f"- 政策数：{len(rows)}\n"
        f"- 当前可关注：{sum(1 for row in rows if row.status == '当前可关注')}\n"
        f"- 已过期：{sum(1 for row in rows if row.status == '已过期')}\n"
        f"- 非申报类：{sum(1 for row in rows if row.status == '非申报类')}\n"
    )
    write_text(log_path, old_log.rstrip() + "\n" + log_entry)

    print(f"Wiki built: {wiki_dir.resolve()}")
    print(f"Policy pages: {len(rows)}")
    print(f"Topic pages: {len(topics)}")
    print(f"Industry pages: {len(industries)}")
    print(f"Applicant type pages: {len(applicant_types)}")
    print(f"Agency pages: {len(agencies)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
