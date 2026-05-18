#!/usr/bin/env python3
"""
Match one company's profile against structured policy rules.

Workflow:
1. Parse the company text into a small machine-readable profile.
2. Use deterministic code to prefilter policies: expired, non-application,
   region mismatch, obvious industry mismatch, missing hard qualifications, etc.
3. Send only remaining candidate policies to the LLM one by one.
4. Save JSONL prefilter/results and generate a Markdown report.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


DEFAULT_RULES_FILE = Path("data/xiongan_policy_files/policy_rules.jsonl")
DEFAULT_REPORT_FILE = Path("data/xiongan_policy_files/company_policy_match_report.md")
DEFAULT_RESULTS_FILE = Path("data/xiongan_policy_files/company_policy_match_results.jsonl")
DEFAULT_PREFILTER_FILE = Path("data/xiongan_policy_files/company_policy_prefilter.jsonl")
DEFAULT_COMPANY_PROFILE_FILE = Path("data/xiongan_policy_files/company_profile.json")
DEFAULT_API_BASE = "http://192.168.211.108:8000/v1"
DEFAULT_API_KEY = "EMPTY"
DEFAULT_MODEL = "/data3/yangsien/models/Qwen3.6-27B"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class MatchError(RuntimeError):
    pass


@dataclass
class PrefilterDecision:
    policy_id: str
    title: str
    candidate: bool
    code_level: str
    score_hint: int
    reasons: list[str]
    matched_facts: list[str]
    missing_info: list[str]
    tags: list[str]


LEVEL_ORDER = {
    "推荐申报": 0,
    "可进一步核实": 1,
    "暂不符合": 2,
    "不适用": 3,
    "模型失败": 4,
}

QUALIFICATION_TERMS = [
    "高新技术企业",
    "科技型中小企业",
    "创新型中小企业",
    "专精特新中小企业",
    "专精特新",
    "小巨人",
    "规上工业企业",
    "规上企业",
    "省级科技创新平台",
    "省级创新平台",
    "企业技术中心",
    "工业设计中心",
    "孵化器",
    "众创空间",
    "上市后备",
    "外商投资企业",
    "外资企业",
]

CATEGORY_KEYWORDS = {
    "software_ai": ["软件开发", "人工智能", "大模型", "数字技术", "信息系统集成", "计算机系统", "数据服务", "算法", "信息技术", "新一代信息技术", "卫星互联网", "量子计算", "鸿蒙", "RISC-V"],
    "tech_service": ["技术服务", "技术开发", "技术咨询", "技术转让", "技术推广", "科技服务", "成果转化", "技术转移", "研发活动", "研发设计", "科学研究", "研究和试验发展", "服务外包", "信息技术外包", "知识流程外包", "未来产业科技服务"],
    "manufacturing": ["制造业", "高端高新制造", "先进制造", "智能制造", "生产制造", "制造能力", "共享工厂", "工业企业", "规上工业", "工业诊断", "工业诊所", "技改"],
    "robotics": ["机器人"],
    "biomed": ["生物", "医药", "医疗", "医疗器械", "生命科学", "药品"],
    "new_material": ["新材料", "材料"],
    "logistics": ["物流", "仓储", "供应链", "快递"],
    "trade": ["外贸", "跨境", "出口", "进口", "汇率避险", "人民币结算"],
    "conference": ["会展业", "展会", "会议项目", "会展活动"],
    "tourism_culture": ["旅游", "文化", "体育"],
    "finance": ["金融", "贷款", "贴息", "上市", "挂牌", "融资"],
    "construction": ["建筑", "工程建设", "施工"],
    "education": ["教育培训", "职业培训", "培训基地", "研修活动", "学校"],
    "accounting": ["会计"],
    "low_altitude": ["低空经济", "航空", "无人机"],
    "standards_ip": ["标准", "知识产权", "专利"],
}

CATEGORY_LABELS = {
    "software_ai": "软件/人工智能/数字技术",
    "tech_service": "科技服务/研发",
    "manufacturing": "制造业/工业",
    "robotics": "机器人",
    "biomed": "生命科学/医药医疗",
    "new_material": "新材料",
    "logistics": "物流/供应链",
    "trade": "外贸/跨境",
    "conference": "会展",
    "tourism_culture": "文旅体育",
    "finance": "金融/上市/贴息",
    "construction": "建筑工程",
    "education": "教育培训",
    "accounting": "会计",
    "low_altitude": "低空经济",
    "standards_ip": "标准/知识产权",
}

RESTRICTIVE_CATEGORIES = {
    "manufacturing",
    "robotics",
    "biomed",
    "logistics",
    "trade",
    "conference",
    "tourism_culture",
    "construction",
    "education",
    "accounting",
    "low_altitude",
}

COMPATIBLE_CATEGORY_GROUPS = [
    {"software_ai", "tech_service"},
    {"manufacturing", "robotics"},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Match company profile against policy rules.")
    parser.add_argument("--rules-file", type=Path, default=DEFAULT_RULES_FILE)
    parser.add_argument("--company-file", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_FILE)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_FILE)
    parser.add_argument("--prefilter-file", type=Path, default=DEFAULT_PREFILTER_FILE)
    parser.add_argument("--company-profile-file", type=Path, default=DEFAULT_COMPANY_PROFILE_FILE)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--ignore-deadline", action="store_true")
    parser.add_argument("--skip-llm", action="store_true", help="Only run code prefilter and report.")
    parser.add_argument("--limit", type=int, default=0, help="Only inspect first N usable rules.")
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=0,
        help="Only send first N prefiltered candidate policies to the LLM.",
    )
    parser.add_argument("--delay", type=float, default=0.1)
    return parser.parse_args()


def read_rules(path: Path, *, limit: int = 0) -> list[dict[str, Any]]:
    if not path.exists():
        raise MatchError(f"Rules file not found: {path}")
    rules: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if value.get("status") == "error":
            continue
        rules.append(value)
        if limit and len(rules) >= limit:
            break
    return rules


def normalize_text(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(normalize_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def field_after(text: str, label: str) -> str:
    match = re.search(rf"(?m)^{re.escape(label)}[：:][ \t]*(.*)$", text)
    return match.group(1).strip() if match else ""


def parse_bool_text(value: str) -> bool | None:
    value = value.strip()
    if value in {"是", "true", "True", "1", "有"}:
        return True
    if value in {"否", "false", "False", "0", "无"}:
        return False
    return None


def categories_for_text(text: str) -> list[str]:
    categories: set[str] = set()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            categories.add(category)
    return ordered_categories(categories)


def ordered_categories(categories: set[str]) -> list[str]:
    return [category for category in CATEGORY_KEYWORDS if category in categories]


def category_labels(categories: set[str] | list[str]) -> str:
    return "、".join(CATEGORY_LABELS.get(category, category) for category in ordered_categories(set(categories)))


def categories_for_company(text: str) -> list[str]:
    categories = set(categories_for_text(text))
    if "教育咨询服务" in text and not has_any(text, ["职业培训", "培训服务", "教育培训服务", "学校"]):
        categories.discard("education")
    return ordered_categories(categories)


def categories_for_rule(rule: dict[str, Any], *, core_only: bool = False) -> list[str]:
    keys = ["title", "applicant_objects", "industry_keywords", "company_stage_requirements"]
    if not core_only:
        keys.extend(["policy_summary", "benefit", "hard_conditions", "region_requirements", "notes"])
    focused_text = " ".join(
        normalize_text(rule.get(key))
        for key in keys
    )
    return categories_for_text(focused_text)


def parse_company_profile(company_text: str) -> dict[str, Any]:
    name = ""
    for line in company_text.splitlines():
        stripped = line.strip()
        if stripped.endswith("有限公司") or stripped.endswith("公司"):
            name = stripped
            break

    risk_match = re.search(r"经营风险\s*(\d+)\s*条", company_text)
    capital_match = re.search(r"注册资本[：:]\s*([0-9.]+)\s*万", company_text)
    established = field_after(company_text, "成立日期")
    industry = field_after(company_text, "所属行业")
    address = field_after(company_text, "注册地址")
    business_scope = field_after(company_text, "经营范围")
    company_size = field_after(company_text, "企业规模")
    is_beijing_company = parse_bool_text(field_after(company_text, "在京企业"))
    is_digital_economy = parse_bool_text(field_after(company_text, "数字经济"))
    products = field_after(company_text, "主要产品")
    main_business = field_after(company_text, "主营业务")

    region = ""
    region_text = address + " " + field_after(company_text, "登记机关")
    if "雄安" in region_text:
        region = "雄安新区"
    elif "河北" in region_text:
        region = "河北省"

    profile_text = " ".join([company_text, industry, business_scope, products, main_business])
    categories = categories_for_company(profile_text)
    qualifications = [term for term in QUALIFICATION_TERMS if term in company_text]

    return {
        "company_name": name,
        "credit_code": next((line.strip() for line in company_text.splitlines() if re.fullmatch(r"[0-9A-Z]{18}", line.strip())), ""),
        "legal_representative": field_after(company_text, "法人代表"),
        "registered_capital_wan": float(capital_match.group(1)) if capital_match else None,
        "industry": industry,
        "established_date": established,
        "company_size": company_size,
        "risk_count": int(risk_match.group(1)) if risk_match else None,
        "is_beijing_company": is_beijing_company,
        "is_digital_economy": is_digital_economy,
        "region": region,
        "registered_address": address,
        "products": products,
        "main_business": main_business,
        "business_scope": business_scope,
        "categories": categories,
        "qualifications": qualifications,
        "raw_text": company_text,
    }


def parse_iso_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def infer_rule_year(rule: dict[str, Any]) -> int | None:
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
            candidate = date(fallback_year, int(month), int(day))
            if candidate not in dates:
                dates.append(candidate)
    return dates


def rule_text(rule: dict[str, Any]) -> str:
    keys = [
        "title",
        "policy_summary",
        "benefit",
        "applicant_objects",
        "hard_conditions",
        "soft_conditions",
        "exclusion_conditions",
        "required_materials",
        "deadline",
        "missing_info_needed",
        "industry_keywords",
        "region_requirements",
        "company_stage_requirements",
        "notes",
    ]
    return " ".join(normalize_text(rule.get(key)) for key in keys)


def has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def compatible_categories(company_categories: set[str], rule_categories: set[str]) -> bool:
    if company_categories & rule_categories:
        return True
    for group in COMPATIBLE_CATEGORY_GROUPS:
        if company_categories & group and rule_categories & group:
            return True
    return False


def compatible_restrictive_categories(company_categories: set[str], restrictive_categories: set[str]) -> bool:
    if company_categories & restrictive_categories:
        return True
    for group in COMPATIBLE_CATEGORY_GROUPS:
        if company_categories & group and restrictive_categories & group:
            return True
    return False


def non_application_reason(rule: dict[str, Any], text: str) -> str | None:
    notes = normalize_text(rule.get("notes"))
    title = normalize_text(rule.get("title"))
    if "非常规政策申报" in notes or "非传统资金补贴类政策申报" in notes:
        return notes or "该政策不是常规企业申报政策。"
    if has_any(title, ["延期", "名单", "公示", "声明", "发布认定名单", "公布"]):
        return "标题显示为名单、公示、延期、声明或结果发布类事项。"
    if has_any(title, ["培训会", "研修活动", "培训活动"]) and "申报" not in title:
        return "该政策更像培训/会议活动，不是企业项目申报政策。"
    if "答辩会延期" in text:
        return "该政策是答辩延期通知，不是新申报入口。"
    return None


def region_mismatch_reason(rule: dict[str, Any], company: dict[str, Any]) -> str | None:
    requirements = [str(item) for item in rule.get("region_requirements", []) if str(item).strip()]
    if not requirements:
        return None
    company_region = normalize_text(company.get("region"))
    company_address = normalize_text(company.get("registered_address"))
    if not company_region and not company_address:
        return None
    for requirement in requirements:
        if requirement in {"河北省", "河北"} and ("河北" in company_address or company_region == "雄安新区"):
            return None
        if "雄安" in requirement and "雄安" in (company_region + company_address):
            return None
        if requirement and requirement in company_address:
            return None
    if any(req for req in requirements if req not in {"河北省", "河北", "雄安新区"}):
        return "地域要求与企业注册地址不匹配：" + "、".join(requirements)
    return None


def subject_mismatch_reason(rule: dict[str, Any], text: str) -> str | None:
    applicant_text = normalize_text(rule.get("applicant_objects"))
    if not applicant_text:
        return None
    person_terms = ["个人", "人员", "从业者", "负责人", "人才", "毕业生"]
    institution_terms = ["高校", "科研院所", "事业单位", "机关", "医疗机构", "学校"]
    enterprise_terms = ["企业", "单位", "公司", "机构"]
    if has_any(applicant_text, person_terms) and not has_any(applicant_text, enterprise_terms):
        return "申报对象主要是个人/人员，不是企业主体。"
    if has_any(applicant_text, institution_terms) and "企业" not in applicant_text:
        return "申报对象主要是高校、科研院所、事业单位或特定机构，不是普通企业。"
    if "外资企业" in applicant_text:
        return "申报对象要求外资企业，当前企业信息未显示外资属性。"
    return None


def industry_mismatch_reason(rule: dict[str, Any], company: dict[str, Any]) -> str | None:
    company_categories = set(company.get("categories", []))
    rule_categories = set(categories_for_rule(rule, core_only=True))
    restrictive = rule_categories & RESTRICTIVE_CATEGORIES
    applicant_text = normalize_text(rule.get("applicant_objects"))
    if "tech_service" in company_categories and has_any(applicant_text, ["服务机构", "诊断服务机构", "第三方服务机构", "服务外包企业"]):
        return None
    if not restrictive:
        return None
    if compatible_restrictive_categories(company_categories, restrictive):
        return None
    if "software_ai" in company_categories and rule_categories <= {"manufacturing", "robotics"}:
        return "政策行业方向偏制造业/机器人，企业信息主要为软件、人工智能和技术服务。"
    category_names = category_labels(restrictive)
    return f"政策行业方向({category_names})与企业主营方向不匹配。"


def missing_required_qualifications(rule: dict[str, Any], company: dict[str, Any], text: str) -> list[str]:
    company_quals = set(company.get("qualifications", []))
    missing: list[str] = []
    hard_text = normalize_text(rule.get("hard_conditions")) + " " + normalize_text(rule.get("company_stage_requirements"))
    for term in QUALIFICATION_TERMS:
        if term in hard_text and term not in company_quals:
            if term == "专精特新" and any(q in company_quals for q in ["专精特新中小企业", "专精特新"]):
                continue
            missing.append(term)
    return sorted(set(missing))


def age_mismatch_reason(rule: dict[str, Any], company: dict[str, Any], as_of: date) -> str | None:
    established = parse_iso_date(normalize_text(company.get("established_date")))
    if not established:
        return None
    years = (as_of - established).days / 365.25
    text = rule_text(rule)
    if has_any(text, ["近三个会计年度", "近三年", "三年内", "满3年"]) and years < 2.7:
        return f"政策涉及近三年/三个会计年度数据，企业成立于{established.isoformat()}，年限不足。"
    if has_any(text, ["近两年", "两个会计年度", "满2年"]) and years < 1.7:
        return f"政策涉及近两年数据，企业成立于{established.isoformat()}，年限可能不足。"
    if has_any(text, ["运营时间须满1年", "运营时间满1年"]) and years < 1:
        return f"政策要求运营满1年，企业成立于{established.isoformat()}，可能不足。"
    return None


def prefilter_policy(
    rule: dict[str, Any],
    company: dict[str, Any],
    *,
    as_of: date,
    ignore_deadline: bool,
) -> PrefilterDecision:
    policy_id = normalize_text(rule.get("policy_id"))
    title = normalize_text(rule.get("title"))
    text = rule_text(rule)
    reasons: list[str] = []
    matched: list[str] = []
    missing: list[str] = []
    tags: list[str] = []

    if company.get("region") == "雄安新区":
        matched.append("企业注册地在雄安新区。")
    if company.get("risk_count") == 0:
        matched.append("企业经营风险为0条。")
    if company.get("company_size"):
        matched.append(f"企业规模为{company['company_size']}。")

    non_app = non_application_reason(rule, text)
    if non_app:
        return PrefilterDecision(policy_id, title, False, "不适用", 0, [non_app], matched, [], ["non_application"])

    if not ignore_deadline:
        deadline_dates = extract_deadline_dates(normalize_text(rule.get("deadline")), infer_rule_year(rule))
        if deadline_dates and max(deadline_dates) < as_of:
            return PrefilterDecision(
                policy_id,
                title,
                False,
                "不适用",
                0,
                [f"申报/办理时间已过，最晚日期为{max(deadline_dates).isoformat()}，当前日期为{as_of.isoformat()}。"],
                matched,
                [],
                ["expired"],
            )

    reason = region_mismatch_reason(rule, company)
    if reason:
        return PrefilterDecision(policy_id, title, False, "不适用", 0, [reason], matched, [], ["region_mismatch"])

    reason = subject_mismatch_reason(rule, text)
    if reason:
        return PrefilterDecision(policy_id, title, False, "不适用", 0, [reason], matched, [], ["subject_mismatch"])

    reason = industry_mismatch_reason(rule, company)
    if reason:
        return PrefilterDecision(policy_id, title, False, "暂不符合", 15, [reason], matched, [], ["industry_mismatch"])

    reason = age_mismatch_reason(rule, company, as_of)
    if reason:
        return PrefilterDecision(policy_id, title, False, "暂不符合", 20, [reason], matched, [], ["age_mismatch"])

    missing_quals = missing_required_qualifications(rule, company, text)
    if missing_quals:
        missing.extend(f"是否具备{term}资质" for term in missing_quals)
        if any(term in {"专精特新中小企业", "专精特新", "小巨人", "高新技术企业", "规上工业企业", "外商投资企业", "外资企业"} for term in missing_quals):
            reasons.append("政策包含明确前置资质要求，企业信息未显示：" + "、".join(missing_quals))
            return PrefilterDecision(
                policy_id,
                title,
                False,
                "暂不符合",
                25,
                reasons,
                matched,
                missing,
                ["missing_required_qualification"],
            )
        tags.append("qualification_to_verify")

    company_categories = set(company.get("categories", []))
    rule_categories = set(categories_for_rule(rule))
    if compatible_categories(company_categories, rule_categories):
        matched.append("企业经营范围与政策产业关键词存在匹配：" + category_labels(company_categories & rule_categories or rule_categories))

    if normalize_text(rule.get("missing_info_needed")):
        missing.extend(rule.get("missing_info_needed", [])[:8])

    score = 55 + min(20, len(matched) * 5) - min(20, len(missing) * 2)
    score = max(35, min(85, score))
    return PrefilterDecision(policy_id, title, True, "可进一步核实", score, reasons, matched, missing, tags or ["candidate"])


def prefilter_to_row(decision: PrefilterDecision) -> dict[str, Any]:
    return {
        "policy_id": decision.policy_id,
        "title": decision.title,
        "candidate": decision.candidate,
        "code_level": decision.code_level,
        "score_hint": decision.score_hint,
        "reasons": decision.reasons,
        "matched_facts": decision.matched_facts,
        "missing_info": decision.missing_info,
        "tags": decision.tags,
    }


def code_result(rule: dict[str, Any], decision: PrefilterDecision) -> dict[str, Any]:
    return {
        "policy_id": decision.policy_id,
        "title": decision.title,
        "match_level": decision.code_level,
        "score": decision.score_hint,
        "matched_facts": decision.matched_facts,
        "failed_conditions": decision.reasons,
        "missing_info": decision.missing_info,
        "reason": "；".join(decision.reasons) if decision.reasons else "代码预筛未进入模型精判。",
        "suggested_action": "无需进入模型精判。" if decision.code_level == "不适用" else "暂不建议申报，除非企业能补充证明材料。",
        "source": "code_prefilter",
    }


def llm_system_prompt() -> str:
    return (
        "你是企业政策申报匹配分析师。你根据一家企业信息、一条政策规则和代码预筛结果，"
        "判断企业是否可能申报。只根据给定信息分析，不要编造企业不存在的资质、收入、专利或人员数据。"
        "必须输出合法 JSON 对象，不要输出 Markdown、解释或代码块。"
    )


def llm_user_prompt(company: dict[str, Any], rule: dict[str, Any], prefilter: PrefilterDecision) -> str:
    compact_rule = {
        "policy_id": rule.get("policy_id"),
        "title": rule.get("title"),
        "policy_summary": rule.get("policy_summary"),
        "benefit": rule.get("benefit"),
        "applicant_objects": rule.get("applicant_objects", []),
        "hard_conditions": rule.get("hard_conditions", []),
        "soft_conditions": rule.get("soft_conditions", []),
        "exclusion_conditions": rule.get("exclusion_conditions", []),
        "required_materials": rule.get("required_materials", []),
        "deadline": rule.get("deadline"),
        "contacts": rule.get("contacts", []),
        "missing_info_needed": rule.get("missing_info_needed", []),
        "industry_keywords": rule.get("industry_keywords", []),
        "region_requirements": rule.get("region_requirements", []),
        "company_stage_requirements": rule.get("company_stage_requirements", []),
        "notes": rule.get("notes"),
    }
    schema = {
        "policy_id": "string",
        "title": "string",
        "match_level": "推荐申报|可进一步核实|暂不符合|不适用",
        "score": "0-100 integer",
        "matched_facts": ["企业已满足或方向匹配的事实"],
        "failed_conditions": ["明确不满足的条件"],
        "missing_info": ["还需要企业补充的信息"],
        "reason": "简要判断理由",
        "suggested_action": "建议动作",
    }
    return f"""请对这一条政策进行企业匹配判断，只输出 JSON。

判断等级定义：
- 推荐申报：硬性条件已基本满足，且没有明显排除条件。
- 可进一步核实：方向相关，但缺少关键判断信息。
- 暂不符合：已知信息与硬性条件明显不符，或明显缺少前置资质。
- 不适用：不是企业申报政策，或主体/区域/行业完全不适用。

输出 JSON schema：
{json.dumps(schema, ensure_ascii=False)}

企业结构化信息：
{json.dumps(company, ensure_ascii=False)}

代码预筛结果：
{json.dumps(prefilter_to_row(prefilter), ensure_ascii=False)}

政策规则：
{json.dumps(compact_rule, ensure_ascii=False)}
"""


def chat_completion(
    *,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    max_tokens: int,
    retries: int,
    enable_thinking: bool,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False,
        "enable_thinking": enable_thinking,
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_url = api_base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(request_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            return str(result["choices"][0]["message"]["content"]).strip()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise MatchError(f"LLM request failed: HTTP {exc.code}: {body[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise MatchError(f"LLM request failed: {exc}") from exc
    raise MatchError("LLM request failed after retries.")


def parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise MatchError(f"Model did not return a JSON object: {raw[:300]}")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise MatchError("Model JSON is not an object.")
    return value


def normalize_match_result(rule: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    list_keys = {"matched_facts", "failed_conditions", "missing_info"}
    result: dict[str, Any] = {}
    for key in ["policy_id", "title", "match_level", "score", "matched_facts", "failed_conditions", "missing_info", "reason", "suggested_action"]:
        item = value.get(key)
        if key in list_keys:
            if item is None:
                item = []
            elif isinstance(item, str):
                item = [item]
            elif not isinstance(item, list):
                item = [str(item)]
            item = [str(entry).strip() for entry in item if str(entry).strip()]
        elif key == "score":
            try:
                item = int(item)
            except Exception:
                item = 50
        else:
            item = "" if item is None else str(item).strip()
        result[key] = item
    result["policy_id"] = normalize_text(rule.get("policy_id"))
    result["title"] = normalize_text(rule.get("title"))
    if result["match_level"] not in LEVEL_ORDER:
        result["match_level"] = "可进一步核实"
    result["score"] = max(0, min(100, int(result["score"])))
    result["source"] = "llm"
    return result


def llm_match_policy(
    *,
    company: dict[str, Any],
    rule: dict[str, Any],
    prefilter: PrefilterDecision,
    args: argparse.Namespace,
) -> dict[str, Any]:
    raw = chat_completion(
        api_base=args.api_base,
        api_key=args.api_key,
        model=args.model,
        messages=[
            {"role": "system", "content": llm_system_prompt()},
            {"role": "user", "content": llm_user_prompt(company, rule, prefilter)},
        ],
        timeout=args.timeout,
        max_tokens=args.max_output_tokens,
        retries=args.retries,
        enable_thinking=args.enable_thinking,
    )
    return normalize_match_result(rule, parse_json_object(raw))


def sort_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda row: (
            LEVEL_ORDER.get(str(row.get("match_level")), 9),
            -int(row.get("score") or 0),
            str(row.get("policy_id") or ""),
        ),
    )


def company_summary(profile: dict[str, Any]) -> str:
    lines = [
        f"- 企业名称：{profile.get('company_name') or ''}",
        f"- 注册地：{profile.get('region') or ''}",
        f"- 行业：{profile.get('industry') or ''}",
        f"- 企业规模：{profile.get('company_size') or ''}",
        f"- 成立日期：{profile.get('established_date') or ''}",
        f"- 经营风险：{profile.get('risk_count') if profile.get('risk_count') is not None else ''}",
        f"- 识别出的业务方向：{category_labels(profile.get('categories') or [])}",
        f"- 已识别资质：{'、'.join(profile.get('qualifications') or []) or '未在输入中识别到'}",
    ]
    return "\n".join(lines)


def make_report(profile: dict[str, Any], results: list[dict[str, Any]], prefilter_rows: list[dict[str, Any]]) -> str:
    sorted_rows = sort_results(results)
    counts = Counter(str(row.get("match_level")) for row in sorted_rows)
    source_counts = Counter(str(row.get("source")) for row in sorted_rows)
    missing_counter: Counter[str] = Counter()
    for row in sorted_rows:
        for item in row.get("missing_info") or []:
            missing_counter[str(item)] += 1

    lines: list[str] = [
        "# 企业政策申报匹配报告",
        "",
        "## 企业摘要",
        "",
        company_summary(profile),
        "",
        "## 匹配结果总览",
        "",
        "| 匹配等级 | 数量 |",
        "| --- | ---: |",
    ]
    for level in ["推荐申报", "可进一步核实", "暂不符合", "不适用", "模型失败"]:
        lines.append(f"| {level} | {counts.get(level, 0)} |")
    lines.extend(
        [
            "",
            "## 处理方式统计",
            "",
            "| 来源 | 数量 |",
            "| --- | ---: |",
        ]
    )
    for source, count in sorted(source_counts.items()):
        lines.append(f"| {source} | {count} |")

    lines.extend(
        [
            "",
            "## 逐项匹配结果",
            "",
            "| 政策ID | 政策名称 | 匹配等级 | 分数 | 来源 | 关键理由 | 需要补充的信息 |",
            "| --- | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for row in sorted_rows:
        reason = normalize_text(row.get("reason")).replace("|", "\\|")
        missing = "；".join(row.get("missing_info") or [])[:180].replace("|", "\\|")
        title = normalize_text(row.get("title")).replace("|", "\\|")
        lines.append(
            f"| {row.get('policy_id')} | {title} | {row.get('match_level')} | {row.get('score')} | {row.get('source')} | {reason[:220]} | {missing} |"
        )

    lines.extend(["", "## 重点政策说明", ""])
    for row in sorted_rows:
        if row.get("match_level") not in {"推荐申报", "可进一步核实"}:
            continue
        lines.extend(
            [
                f"### {row.get('policy_id')} {row.get('title')}",
                "",
                f"- 匹配结论：{row.get('match_level')}（{row.get('score')}分）",
                f"- 判断依据：{normalize_text(row.get('reason'))}",
            ]
        )
        if row.get("matched_facts"):
            lines.append("- 已匹配事实：" + "；".join(row.get("matched_facts")))
        if row.get("failed_conditions"):
            lines.append("- 不满足项：" + "；".join(row.get("failed_conditions")))
        if row.get("missing_info"):
            lines.append("- 需补充信息：" + "；".join(row.get("missing_info")))
        if row.get("suggested_action"):
            lines.append("- 建议动作：" + normalize_text(row.get("suggested_action")))
        lines.append("")

    lines.extend(["## 高频需补充信息", ""])
    if missing_counter:
        for item, count in missing_counter.most_common(25):
            lines.append(f"- {item}（{count}次）")
    else:
        lines.append("- 无")

    lines.extend(
        [
            "",
            "## 预筛说明",
            "",
            f"- 预筛政策数：{len(prefilter_rows)}",
            f"- 进入模型精判数：{sum(1 for row in prefilter_rows if row.get('candidate'))}",
            "- 代码预筛会直接排除过期、非申报、地域不符、主体不符、行业明显不符、成立年限明显不足、前置资质明显缺失等政策。",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    args = parse_args()
    as_of = parse_iso_date(args.as_of_date)
    if not as_of:
        raise MatchError(f"Invalid --as-of-date: {args.as_of_date}")

    company_text = args.company_file.read_text(encoding="utf-8")
    company = parse_company_profile(company_text)
    rules = read_rules(args.rules_file, limit=args.limit)
    if not rules:
        raise MatchError(f"No usable policy rules found in {args.rules_file}")

    args.company_profile_file.parent.mkdir(parents=True, exist_ok=True)
    args.company_profile_file.write_text(
        json.dumps(company, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    decisions: list[PrefilterDecision] = [
        prefilter_policy(rule, company, as_of=as_of, ignore_deadline=args.ignore_deadline)
        for rule in rules
    ]
    prefilter_rows = [prefilter_to_row(decision) for decision in decisions]
    write_jsonl(args.prefilter_file, prefilter_rows)

    results: list[dict[str, Any]] = []
    candidate_seen = 0
    for index, (rule, decision) in enumerate(zip(rules, decisions), start=1):
        if not decision.candidate:
            results.append(code_result(rule, decision))
            print(f"[{index}/{len(rules)}] Code filtered: {decision.policy_id} {decision.code_level}")
            continue

        candidate_seen += 1
        if args.candidate_limit and candidate_seen > args.candidate_limit:
            result = code_result(rule, decision)
            result["reason"] = "超过 --candidate-limit，未进入模型精判。"
            result["source"] = "candidate_limit"
            results.append(result)
            print(f"[{index}/{len(rules)}] Candidate limit skipped: {decision.policy_id}")
            continue

        if args.skip_llm:
            result = code_result(rule, decision)
            result["reason"] = "按 --skip-llm 仅使用代码预筛结果。"
            result["source"] = "code_candidate"
            results.append(result)
            print(f"[{index}/{len(rules)}] Candidate without LLM: {decision.policy_id}")
            continue

        try:
            result = llm_match_policy(company=company, rule=rule, prefilter=decision, args=args)
            results.append(result)
            print(f"[{index}/{len(rules)}] LLM matched: {decision.policy_id} {result.get('match_level')}")
        except Exception as exc:
            results.append(
                {
                    "policy_id": decision.policy_id,
                    "title": decision.title,
                    "match_level": "模型失败",
                    "score": 0,
                    "matched_facts": decision.matched_facts,
                    "failed_conditions": [str(exc)],
                    "missing_info": decision.missing_info,
                    "reason": str(exc),
                    "suggested_action": "可单独重试该政策。",
                    "source": "llm_error",
                }
            )
            print(f"[{index}/{len(rules)}] LLM error: {decision.policy_id}: {exc}", file=sys.stderr)

        write_jsonl(args.results_file, results)
        if args.delay:
            time.sleep(args.delay)

    write_jsonl(args.results_file, results)
    args.report_file.parent.mkdir(parents=True, exist_ok=True)
    args.report_file.write_text(make_report(company, results, prefilter_rows), encoding="utf-8")

    print(f"Rules inspected: {len(rules)}")
    print(f"Candidates for LLM: {sum(1 for row in prefilter_rows if row.get('candidate'))}")
    print(f"Results: {args.results_file.resolve()}")
    print(f"Prefilter: {args.prefilter_file.resolve()}")
    print(f"Company profile: {args.company_profile_file.resolve()}")
    print(f"Report: {args.report_file.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
