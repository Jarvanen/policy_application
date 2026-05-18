#!/usr/bin/env python3
"""
Local tool-style search for the Xiongan policy LLM wiki.

This intentionally avoids vector search and LLM reranking. It uses the same
basic pattern as local coding agents: inspect index pages, grep exact terms,
follow wiki links, and read only the relevant sections.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


DEFAULT_WIKI_DIR = Path("data/xiongan_policy_files/llm_wiki")

IGNORE_FILES = {"WIKI_SCHEMA.md", "log.md"}

STOP_TERMS = {
    "企业",
    "政策",
    "申请",
    "申报",
    "可以",
    "哪些",
    "什么",
    "如何",
    "怎么",
    "条件",
    "时间",
    "截止",
    "相关",
    "支持",
    "通知",
    "指南",
    "现在",
    "当前",
    "这个",
    "一下",
}

DOMAIN_TERMS = [
    "高新技术企业",
    "科技型中小企业",
    "创新型中小企业",
    "专精特新",
    "小巨人",
    "企业技术中心",
    "工业设计中心",
    "孵化器",
    "众创空间",
    "知识产权",
    "专利",
    "标准",
    "研发费用",
    "技术合同",
    "成果转化",
    "技术转移",
    "服务外包",
    "人工智能",
    "大模型",
    "软件",
    "数字技术",
    "信息技术",
    "新一代信息技术",
    "新材料",
    "生物医药",
    "生命科学",
    "医疗器械",
    "机器人",
    "智能网联",
    "低空经济",
    "物流",
    "会展",
    "外贸",
    "外资",
    "上市",
    "挂牌",
    "贷款贴息",
    "租房补贴",
    "办公用房",
    "雄安新区",
    "河北省",
    "中小微",
    "科技服务",
    "研发",
]

CURRENT_INTENT_TERMS = ["当前可关注", "未明确期限", "有效期", "截止日期"]
CONDITION_INTENT_TERMS = ["申请对象", "硬性条件", "申报条件", "匹配时需要补充的信息"]
CONTACT_INTENT_TERMS = ["联系方式", "咨询电话", "联系人", "电话"]
BENEFIT_INTENT_TERMS = ["支持内容", "奖励", "补贴", "额度", "奖补"]

POLICY_SECTIONS = [
    "快速结论",
    "政策简介",
    "支持内容",
    "申请对象",
    "申报条件",
    "材料与流程",
    "时间与联系方式",
    "匹配时需要补充的信息",
    "来源",
]


class PolicyWikiSearchError(RuntimeError):
    pass


@dataclass
class GrepMatch:
    path: Path
    line: int
    content: str
    term: str = ""

    def to_dict(self, wiki_dir: Path) -> dict[str, Any]:
        return {
            "path": str(self.path.relative_to(wiki_dir)),
            "line": self.line,
            "content": self.content,
            "term": self.term,
        }


@dataclass
class EvidencePage:
    path: Path
    kind: str
    title: str = ""
    status: str = ""
    reasons: list[str] = field(default_factory=list)
    matches: list[GrepMatch] = field(default_factory=list)
    priorities: list[int] = field(default_factory=list)
    linked_from: list[str] = field(default_factory=list)

    def add_reason(self, reason: str, priority: int) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.priorities.append(priority)

    @property
    def priority(self) -> int:
        return min(self.priorities) if self.priorities else 99


@dataclass
class EvidenceBundle:
    question: str
    query_terms: list[str]
    intent_terms: list[str]
    trace: list[str]
    pages: list[EvidencePage]


SearchEventCallback = Callable[[dict[str, Any]], None]


def emit_search_event(callback: SearchEventCallback | None, event: str, name: str, **payload: Any) -> None:
    if callback:
        callback({"event": event, "name": name, **payload})


def rel(path: Path, wiki_dir: Path) -> str:
    return str(path.relative_to(wiki_dir))


def validate_wiki_dir(wiki_dir: Path) -> None:
    if not wiki_dir.exists():
        raise PolicyWikiSearchError(f"Wiki dir not found: {wiki_dir}")
    if not (wiki_dir / "index.md").exists():
        raise PolicyWikiSearchError(f"Missing wiki index: {wiki_dir / 'index.md'}")


def iter_markdown_pages(wiki_dir: Path, roots: list[str] | None = None) -> list[Path]:
    validate_wiki_dir(wiki_dir)
    base_dirs = [wiki_dir / root for root in roots] if roots else [wiki_dir]
    pages: list[Path] = []
    for base in base_dirs:
        if not base.exists():
            continue
        if base.is_file():
            candidates = [base]
        else:
            candidates = sorted(base.rglob("*.md"))
        for path in candidates:
            if path.name in IGNORE_FILES:
                continue
            pages.append(path)
    return sorted(set(pages))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def title_from_markdown(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def page_kind(path: Path, wiki_dir: Path) -> str:
    relative = rel(path, wiki_dir)
    if relative.startswith("policies/"):
        return "policy"
    if relative.startswith(("topics/", "industries/", "applicant_types/", "agencies/")):
        return "collection"
    if relative.startswith("qa/"):
        return "qa"
    return "meta"


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    block = text[4:end]
    data: dict[str, Any] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            try:
                data[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        data[key] = value.strip('"')
    return data


def extract_wiki_links(text: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", text):
        target = match.group(1).strip()
        if target:
            links.append(target)
    return list(dict.fromkeys(links))


def resolve_link(wiki_dir: Path, target: str) -> Path | None:
    target = target.strip().removesuffix(".md")
    path = wiki_dir / f"{target}.md"
    return path if path.exists() else None


def extract_query_terms(question: str) -> list[str]:
    terms: list[str] = []
    for term in sorted(DOMAIN_TERMS, key=len, reverse=True):
        if term in question and term not in terms:
            terms.append(term)

    for term in re.split(r"[\s,，。；;、：:（）()【】\[\]\"'“”]+", question):
        term = term.strip()
        if 2 <= len(term) <= 18 and term not in STOP_TERMS and term not in terms:
            terms.append(term)

    if not terms:
        compact = "".join(ch for ch in question if "\u4e00" <= ch <= "\u9fff")
        for size in (4, 3, 2):
            for index in range(max(0, len(compact) - size + 1)):
                token = compact[index : index + size]
                if token not in STOP_TERMS and token not in terms:
                    terms.append(token)
                if len(terms) >= 12:
                    break
            if terms:
                break

    return terms[:30]


def extract_intent_terms(question: str) -> list[str]:
    terms: list[str] = []
    if any(item in question for item in ["现在", "当前", "还能", "可申报", "有效", "还可以"]):
        terms.extend(CURRENT_INTENT_TERMS)
    if any(item in question for item in ["条件", "要求", "资格", "对象", "材料", "流程", "怎么申报"]):
        terms.extend(CONDITION_INTENT_TERMS)
    if any(item in question for item in ["电话", "咨询", "联系", "联系人"]):
        terms.extend(CONTACT_INTENT_TERMS)
    if any(item in question for item in ["奖励", "补贴", "额度", "奖补", "扶持", "多少钱"]):
        terms.extend(BENEFIT_INTENT_TERMS)
    if any(item in question for item in ["匹配", "适合", "能申请", "可申请", "公司", "企业"]):
        terms.extend(["申请对象", "硬性条件", "状态：当前可关注", "状态：未明确期限"])
    return list(dict.fromkeys(terms))


def make_regex(terms: list[str]) -> str:
    escaped = [re.escape(term) for term in terms if term.strip()]
    if not escaped:
        return r"$^"
    return "|".join(escaped)


def grep_with_rg(wiki_dir: Path, pattern: str, roots: list[Path], max_matches: int) -> list[GrepMatch] | None:
    rg = shutil.which("rg")
    if not rg:
        return None
    args = [rg, "-n", "--color", "never", "--no-heading", "--glob", "*.md", pattern]
    args.extend(str(root) for root in roots if root.exists())
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    except Exception:
        return None
    if result.returncode not in (0, 1):
        return None
    matches: list[GrepMatch] = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path_text, line_text, content = parts
        try:
            line_number = int(line_text)
        except ValueError:
            continue
        path = Path(path_text)
        if path.name in IGNORE_FILES:
            continue
        matches.append(GrepMatch(path=path, line=line_number, content=content.rstrip()))
        if len(matches) >= max_matches:
            break
    return matches


def grep_fallback(pattern: str, pages: list[Path], max_matches: int) -> list[GrepMatch]:
    regex = re.compile(pattern, re.IGNORECASE)
    matches: list[GrepMatch] = []
    for path in pages:
        try:
            lines = read_text(path).splitlines()
        except Exception:
            continue
        for line_number, line in enumerate(lines, start=1):
            if regex.search(line):
                matches.append(GrepMatch(path=path, line=line_number, content=line.rstrip()))
                if len(matches) >= max_matches:
                    return matches
    return matches


def grep_pages(wiki_dir: Path, terms: list[str], roots: list[str], max_matches: int = 200) -> list[GrepMatch]:
    pattern = make_regex(terms)
    root_paths = [wiki_dir / root for root in roots]
    rg_matches = grep_with_rg(wiki_dir, pattern, root_paths, max_matches=max_matches)
    if rg_matches is not None:
        return rg_matches
    return grep_fallback(pattern, iter_markdown_pages(wiki_dir, roots), max_matches=max_matches)


def group_matches(matches: list[GrepMatch]) -> dict[Path, list[GrepMatch]]:
    grouped: dict[Path, list[GrepMatch]] = defaultdict(list)
    for match in matches:
        grouped[match.path].append(match)
    return dict(grouped)


def get_or_create(pages: dict[Path, EvidencePage], path: Path, wiki_dir: Path) -> EvidencePage:
    if path not in pages:
        text = read_text(path)
        frontmatter = parse_frontmatter(text)
        pages[path] = EvidencePage(
            path=path,
            kind=page_kind(path, wiki_dir),
            title=title_from_markdown(text, path.stem),
            status=str(frontmatter.get("status") or ""),
        )
    return pages[path]


def path_or_title_matches(path: Path, title: str, terms: list[str], wiki_dir: Path) -> list[str]:
    haystack = (rel(path, wiki_dir) + " " + title).lower()
    return [term for term in terms if term.lower() in haystack]


def status_rank(status: str) -> int:
    return {
        "当前可关注": 0,
        "未明确期限": 1,
        "已过期": 4,
        "非申报类": 5,
    }.get(status, 2)


def evidence_sort_key(page: EvidencePage, wiki_dir: Path) -> tuple[int, int, int, str]:
    kind_rank = {"collection": 0, "policy": 1, "qa": 2, "meta": 3}.get(page.kind, 4)
    return (page.priority, status_rank(page.status), kind_rank, rel(page.path, wiki_dir))


def policy_sort_key(page: EvidencePage, wiki_dir: Path, current_sensitive: bool) -> tuple[int, int, int, str]:
    if current_sensitive:
        adjusted_priority = page.priority
        if page.status == "当前可关注":
            adjusted_priority -= 3
        elif page.status == "未明确期限":
            adjusted_priority -= 1
        elif page.status in {"已过期", "非申报类"}:
            adjusted_priority += 40
        return (adjusted_priority, status_rank(page.status), len(page.matches) * -1, rel(page.path, wiki_dir))
    return (page.priority, status_rank(page.status), len(page.matches) * -1, rel(page.path, wiki_dir))


def choose_evidence_pages(
    evidence_pages: dict[Path, EvidencePage],
    wiki_dir: Path,
    top_k: int,
    current_sensitive: bool,
) -> list[EvidencePage]:
    """Keep collection pages as navigation, but reserve most context for policies."""
    all_pages = list(evidence_pages.values())
    collections = sorted(
        [page for page in all_pages if page.kind == "collection"],
        key=lambda page: evidence_sort_key(page, wiki_dir),
    )
    policies = sorted(
        [page for page in all_pages if page.kind == "policy"],
        key=lambda page: policy_sort_key(page, wiki_dir, current_sensitive),
    )
    exact_qa = sorted(
        [page for page in all_pages if page.kind == "qa" and page.priority <= 18],
        key=lambda page: evidence_sort_key(page, wiki_dir),
    )
    rest = sorted(
        [page for page in all_pages if page.kind not in {"collection", "policy"} or page.priority > 18],
        key=lambda page: evidence_sort_key(page, wiki_dir),
    )

    selected: list[EvidencePage] = []
    seen: set[Path] = set()

    def add_many(candidates: list[EvidencePage], limit: int) -> None:
        added = 0
        for candidate in candidates:
            if candidate.path in seen:
                continue
            selected.append(candidate)
            seen.add(candidate.path)
            added += 1
            if len(selected) >= top_k or added >= limit:
                break

    collection_limit = min(3, max(1, top_k // 4))
    add_many(collections, collection_limit)
    add_many(exact_qa, 1)
    add_many(policies, top_k - len(selected))
    if len(selected) < top_k:
        add_many(collections + rest + policies, top_k - len(selected))
    return selected[:top_k]


def add_policy_links_from_page(
    *,
    evidence_pages: dict[Path, EvidencePage],
    source_page: EvidencePage,
    wiki_dir: Path,
    limit: int,
) -> None:
    text = read_text(source_page.path)
    added = 0
    for target in extract_wiki_links(text):
        if not target.startswith("policies/"):
            continue
        resolved = resolve_link(wiki_dir, target)
        if not resolved:
            continue
        page = get_or_create(evidence_pages, resolved, wiki_dir)
        source_rel = rel(source_page.path, wiki_dir)
        if source_rel not in page.linked_from:
            page.linked_from.append(source_rel)
        page.add_reason(f"linked_from:{source_rel}", 40)
        added += 1
        if added >= limit:
            break


def line_window(path: Path, line_number: int, radius: int = 2) -> str:
    lines = read_text(path).splitlines()
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    output = []
    for index in range(start, end + 1):
        prefix = ">" if index == line_number else " "
        output.append(f"{prefix}{index}: {lines[index - 1]}")
    return "\n".join(output)


def extract_sections(text: str, section_names: list[str]) -> dict[str, str]:
    lines = text.splitlines()
    sections: dict[str, list[str]] = {}
    current_name = ""
    for line in lines:
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            name = heading.group(1).strip()
            current_name = name if name in section_names else ""
            if current_name and current_name not in sections:
                sections[current_name] = [line]
            continue
        if current_name:
            sections[current_name].append(line)
    return {name: "\n".join(value).strip() for name, value in sections.items()}


def page_excerpt(page: EvidencePage, wiki_dir: Path, per_page_chars: int) -> str:
    text = read_text(page.path).strip()
    relative = rel(page.path, wiki_dir)
    header = [
        f"## Evidence: {relative}",
        f"- kind: {page.kind}",
        f"- title: {page.title}",
    ]
    if page.status:
        header.append(f"- status: {page.status}")
    if page.reasons:
        header.append("- reasons: " + "；".join(page.reasons[:8]))
    if page.linked_from:
        header.append("- linked_from: " + "；".join(page.linked_from[:6]))

    body_parts: list[str] = []
    if page.matches:
        body_parts.append("### 命中行")
        for match in page.matches[:8]:
            body_parts.append(line_window(match.path, match.line, radius=1))

    if page.kind == "policy":
        sections = extract_sections(text, POLICY_SECTIONS)
        for section_name in POLICY_SECTIONS:
            if section_name in sections:
                body_parts.append(sections[section_name])
    else:
        body_parts.append(text)

    body = "\n\n".join(body_parts).strip()
    combined = "\n".join(header) + "\n\n" + body
    if len(combined) > per_page_chars:
        combined = combined[:per_page_chars].rstrip() + "\n\n[页面证据已截断]"
    return combined


def build_evidence_bundle(
    *,
    wiki_dir: Path,
    question: str,
    top_k: int = 12,
    max_collection_pages: int = 8,
    links_per_collection: int = 15,
    event_callback: SearchEventCallback | None = None,
) -> EvidenceBundle:
    validate_wiki_dir(wiki_dir)
    emit_search_event(
        event_callback,
        "tool_call",
        "read_wiki_entrypoints",
        files=["index.md", "overview.md"],
    )
    query_terms = extract_query_terms(question)
    intent_terms = extract_intent_terms(question)
    search_terms = list(dict.fromkeys(query_terms + intent_terms))
    trace: list[str] = [
        "read:index.md",
        "read:overview.md",
        "extract_query_terms:" + "、".join(query_terms),
    ]
    if intent_terms:
        trace.append("extract_intent_terms:" + "、".join(intent_terms))

    evidence_pages: dict[Path, EvidencePage] = {}

    for seed_name in ["index.md", "overview.md"]:
        seed_path = wiki_dir / seed_name
        if seed_path.exists():
            page = get_or_create(evidence_pages, seed_path, wiki_dir)
            page.add_reason(f"seed:{seed_name}", 80)
    emit_search_event(
        event_callback,
        "tool_result",
        "read_wiki_entrypoints",
        files=[name for name in ["index.md", "overview.md"] if (wiki_dir / name).exists()],
    )
    emit_search_event(
        event_callback,
        "tool_result",
        "extract_query_terms",
        query_terms=query_terms,
        intent_terms=intent_terms,
    )

    collection_roots = ["industries", "topics", "applicant_types"]
    if any(term in question for term in ["发文单位", "部门", "谁发", "工信", "改革发展局", "财政局", "公共服务局", "营商环境局"]):
        collection_roots.append("agencies")
    collection_search_terms = query_terms or intent_terms
    emit_search_event(
        event_callback,
        "tool_call",
        "grep_collections",
        roots=collection_roots,
        terms=collection_search_terms,
        max_matches=300,
    )
    collection_matches = grep_pages(wiki_dir, collection_search_terms, collection_roots, max_matches=300)
    trace.append(f"grep:collections matches={len(collection_matches)}")
    collection_pages = group_matches(collection_matches)
    emit_search_event(
        event_callback,
        "tool_result",
        "grep_collections",
        matches=len(collection_matches),
        pages=len(collection_pages),
    )

    for path, matches in collection_pages.items():
        page = get_or_create(evidence_pages, path, wiki_dir)
        page.matches.extend(matches[:10])
        exact_terms = path_or_title_matches(path, page.title, query_terms, wiki_dir)
        if exact_terms:
            page.add_reason("collection_title_or_path:" + "、".join(exact_terms), 10)
        else:
            page.add_reason("collection_grep", 20)

    top_collections = sorted(
        [page for page in evidence_pages.values() if page.kind == "collection"],
        key=lambda page: evidence_sort_key(page, wiki_dir),
    )[:max_collection_pages]
    trace.append("follow_links_from:" + "、".join(rel(page.path, wiki_dir) for page in top_collections))
    emit_search_event(
        event_callback,
        "tool_call",
        "follow_wiki_links",
        pages=[rel(page.path, wiki_dir) for page in top_collections],
        links_per_page=links_per_collection,
    )
    before_linked = len([page for page in evidence_pages.values() if page.kind == "policy"])
    for page in top_collections:
        add_policy_links_from_page(
            evidence_pages=evidence_pages,
            source_page=page,
            wiki_dir=wiki_dir,
            limit=links_per_collection,
        )
    after_linked = len([page for page in evidence_pages.values() if page.kind == "policy"])
    emit_search_event(
        event_callback,
        "tool_result",
        "follow_wiki_links",
        linked_policy_pages=max(0, after_linked - before_linked),
    )

    policy_search_terms = query_terms or search_terms
    emit_search_event(
        event_callback,
        "tool_call",
        "grep_policies",
        roots=["policies"],
        terms=policy_search_terms,
        max_matches=500,
    )
    policy_matches = grep_pages(wiki_dir, policy_search_terms, ["policies"], max_matches=500)
    trace.append(f"grep:policies matches={len(policy_matches)}")
    policy_pages = group_matches(policy_matches)
    emit_search_event(
        event_callback,
        "tool_result",
        "grep_policies",
        matches=len(policy_matches),
        pages=len(policy_pages),
    )
    for path, matches in policy_pages.items():
        page = get_or_create(evidence_pages, path, wiki_dir)
        page.matches.extend(matches[:12])
        exact_terms = path_or_title_matches(path, page.title, query_terms, wiki_dir)
        if exact_terms:
            page.add_reason("policy_title_or_path:" + "、".join(exact_terms), 25)
        else:
            page.add_reason("policy_grep", 30)

    qa_dir = wiki_dir / "qa"
    if qa_dir.exists():
        emit_search_event(
            event_callback,
            "tool_call",
            "grep_saved_qa",
            roots=["qa"],
            terms=query_terms or intent_terms,
            max_matches=100,
        )
        qa_matches = grep_pages(wiki_dir, query_terms or intent_terms, ["qa"], max_matches=100)
        trace.append(f"grep:qa matches={len(qa_matches)}")
        qa_pages = group_matches(qa_matches)
        emit_search_event(
            event_callback,
            "tool_result",
            "grep_saved_qa",
            matches=len(qa_matches),
            pages=len(qa_pages),
        )
        for path, matches in qa_pages.items():
            page = get_or_create(evidence_pages, path, wiki_dir)
            page.matches.extend(matches[:8])
            exact_terms = path_or_title_matches(path, page.title, query_terms, wiki_dir)
            if exact_terms:
                page.add_reason("qa_title_or_path:" + "、".join(exact_terms), 18)
            else:
                page.add_reason("qa_grep", 65)

    current_sensitive = any(term in intent_terms for term in CURRENT_INTENT_TERMS)
    selected_pages = choose_evidence_pages(
        evidence_pages=evidence_pages,
        wiki_dir=wiki_dir,
        top_k=top_k,
        current_sensitive=current_sensitive,
    )
    trace.append("selected_pages:" + "、".join(rel(page.path, wiki_dir) for page in selected_pages))
    emit_search_event(
        event_callback,
        "tool_result",
        "select_evidence_pages",
        pages=[rel(page.path, wiki_dir) for page in selected_pages],
    )
    return EvidenceBundle(
        question=question,
        query_terms=query_terms,
        intent_terms=intent_terms,
        trace=trace,
        pages=selected_pages,
    )


def evidence_context(bundle: EvidenceBundle, wiki_dir: Path, max_context_chars: int) -> str:
    pages = bundle.pages
    if not pages:
        return ""
    per_page = max(1800, max_context_chars // max(1, len(pages)))
    parts = [
        "# Local Search Trace",
        "\n".join(f"- {step}" for step in bundle.trace),
        "",
        "# Evidence Pages",
    ]
    used = len("\n".join(parts))
    for page in pages:
        excerpt = page_excerpt(page, wiki_dir, per_page_chars=per_page)
        if used + len(excerpt) + 8 > max_context_chars:
            break
        parts.append(excerpt)
        parts.append("\n---\n")
        used += len(excerpt) + 8
    return "\n".join(parts).rstrip()


def print_bundle(bundle: EvidenceBundle, wiki_dir: Path, *, include_trace: bool = True) -> None:
    if include_trace:
        print("Search trace:")
        for step in bundle.trace:
            print(f"- {step}")
        print()
    print("Evidence pages:")
    for index, page in enumerate(bundle.pages, start=1):
        print(f"{index}. [{page.kind}] {rel(page.path, wiki_dir)}")
        print(f"   title: {page.title}")
        if page.status:
            print(f"   status: {page.status}")
        if page.reasons:
            print(f"   reasons: {'; '.join(page.reasons)}")
        if page.matches:
            print("   matches:")
            for match in page.matches[:5]:
                content = match.content.strip()
                if len(content) > 180:
                    content = content[:180] + "..."
                print(f"     L{match.line}: {content}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local grep/link search over llm_wiki.")
    parser.add_argument("question", nargs="*", help="Question text.")
    parser.add_argument("--question", dest="question_option", default="")
    parser.add_argument("--wiki-dir", type=Path, default=DEFAULT_WIKI_DIR)
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--max-context-chars", type=int, default=50000)
    parser.add_argument("--context", action="store_true", help="Print the evidence context sent to the LLM.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    question = args.question_option.strip() or " ".join(args.question).strip()
    if not question:
        raise SystemExit("Question is required.")
    bundle = build_evidence_bundle(wiki_dir=args.wiki_dir, question=question, top_k=args.top_k)
    print_bundle(bundle, args.wiki_dir)
    if args.context:
        print("\n\n==== CONTEXT ====\n")
        print(evidence_context(bundle, args.wiki_dir, args.max_context_chars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
