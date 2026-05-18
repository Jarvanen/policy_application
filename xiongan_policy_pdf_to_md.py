#!/usr/bin/env python3
"""
Download Xiongan New Area policy-file records from xaiip.org.cn and convert
each policy record to one Markdown file.

The public web page is a Vue app. For the current "Xiongan New Area" policy
file tab, the frontend calls:

  GET https://api.xaiip.org.cn/policyFile/list?issueLevel=市级
  GET https://api.xaiip.org.cn/admin/policyFile/info?id=<infoId>
  GET https://api.xaiip.org.cn/files/view?url=<file.url>
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import http.client
import html
from html.parser import HTMLParser
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


API_BASE = "https://api.xaiip.org.cn"
DEFAULT_OUTPUT_DIR = Path("data/xiongan_policy_files")
DEFAULT_AREA_RANGE = "雄安新区"
DEFAULT_ISSUE_LEVEL = "市级"
DEFAULT_EXPECTED_COUNT = 152
DEFAULT_MINERU_KEY_FILE = Path("/Users/turing/PycharmProjects/PythonProject/ic/minerutomarkdown.py")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class PolicyDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str


@dataclass(frozen=True)
class PolicyFile:
    title: str
    list_id: int | None
    info_id: str
    publish_time: str | None
    publish_unit: str | None
    issue_no: str | None
    issue_level: str | None
    policy_label: str | None
    etp_label: str | None
    content: str | None
    attachments: tuple[Attachment, ...]


@dataclass
class PolicyMarkdownJob:
    policy: PolicyFile
    index: int
    md_path: Path
    row: dict[str, Any]
    sections: list[str]
    errors: list[str]


@dataclass
class PdfConversionJob:
    policy_job: PolicyMarkdownJob
    attachment: Attachment
    attachment_index: int
    pdf_path: Path
    source_url: str


@dataclass(frozen=True)
class MinerUTask:
    data_id: str
    job_index: int
    chunk_index: int
    total_chunks: int
    page_range: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download current Xiongan New Area policy files and convert each policy to Markdown."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--area-range",
        default=DEFAULT_AREA_RANGE,
        help='Area in the site query. Default: "雄安新区".',
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="API page size for list pagination. Default: 100.",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=DEFAULT_EXPECTED_COUNT,
        help="Warn when API rowCount differs. Use 0 to disable. Default: 152.",
    )
    parser.add_argument(
        "--strict-count",
        action="store_true",
        help="Exit if rowCount differs from --expected-count.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Delay between detail/download requests in seconds. Default: 0.2.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="HTTP and conversion timeout in seconds. Default: 60.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N policies. Useful for testing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Redownload PDFs and reconvert Markdown even if files already exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch list/details and write manifest only; do not download PDFs.",
    )
    parser.add_argument(
        "--include-non-pdf",
        action="store_true",
        default=True,
        help="Download and convert non-PDF attachments too. Enabled by default.",
    )
    parser.add_argument(
        "--skip-non-pdf",
        action="store_true",
        help="Skip non-PDF attachments.",
    )
    parser.add_argument(
        "--converter",
        choices=("mineru", "local"),
        default="mineru",
        help="Markdown conversion backend. Default: mineru.",
    )
    parser.add_argument(
        "--mineru-api-key",
        default="",
        help="MinerU API key. If omitted, MINERU_API_KEY or --mineru-key-file is used.",
    )
    parser.add_argument(
        "--mineru-key-file",
        type=Path,
        default=DEFAULT_MINERU_KEY_FILE,
        help=f"Python file containing API_KEY = ... . Default: {DEFAULT_MINERU_KEY_FILE}",
    )
    parser.add_argument(
        "--mineru-model-version",
        default="vlm",
        help='MinerU model_version. Default: "vlm".',
    )
    parser.add_argument(
        "--mineru-chunk-size",
        type=int,
        default=200,
        help="Split PDFs into page ranges of at most this many pages. Default: 200.",
    )
    parser.add_argument(
        "--mineru-batch-task-limit",
        type=int,
        default=50,
        help="Maximum MinerU parse tasks submitted in one batch. Default: 50.",
    )
    parser.add_argument(
        "--mineru-poll-interval",
        type=float,
        default=10,
        help="Seconds between MinerU batch status checks. Default: 10.",
    )
    parser.add_argument(
        "--mineru-max-poll-seconds",
        type=float,
        default=5 * 60 * 60,
        help="Maximum seconds to wait for each MinerU batch. Default: 18000.",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="With --converter local, run Tesseract OCR on pages with little or no extractable text.",
    )
    parser.add_argument(
        "--ocr-lang",
        default="chi_sim+eng",
        help="Tesseract language list for OCR. Default: chi_sim+eng.",
    )
    parser.add_argument(
        "--ocr-dpi",
        type=int,
        default=300,
        help="Render DPI for OCR. Default: 300.",
    )
    parser.add_argument(
        "--ocr-min-chars",
        type=int,
        default=20,
        help="OCR pages whose extracted text has fewer than this many characters. Default: 20.",
    )
    return parser.parse_args()


def make_request(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> bytes:
    merged_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://xaiip.org.cn/",
        "Accept": "*/*",
    }
    if headers:
        merged_headers.update(headers)
    request = urllib.request.Request(url, headers=merged_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PolicyDownloadError(f"HTTP {exc.code} for {url}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise PolicyDownloadError(f"Request failed for {url}: {exc.reason}") from exc


def request_json_url(
    url: str,
    *,
    timeout: float,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    merged_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"
    if headers:
        merged_headers.update(headers)

    request = urllib.request.Request(url, data=data, headers=merged_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PolicyDownloadError(f"HTTP {exc.code} for {url}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise PolicyDownloadError(f"Request failed for {url}: {exc.reason}") from exc


def request_json(path: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None},
        doseq=True,
        safe="/",
    )
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    data = make_request(
        url,
        timeout=timeout,
        headers={"Accept": "application/json, text/plain, */*"},
    )
    payload = json.loads(data.decode("utf-8"))
    if payload.get("status") != 1:
        raise PolicyDownloadError(f"API returned non-success for {url}: {payload}")
    return payload


def list_params(area_range: str, page_num: int, page_size: int) -> dict[str, Any]:
    if area_range == DEFAULT_AREA_RANGE:
        return {
            "pageNum": page_num,
            "pageSize": page_size,
            "issueLevel": DEFAULT_ISSUE_LEVEL,
            "areaRange": "",
        }
    return {
        "pageNum": page_num,
        "pageSize": page_size,
        "issueLevel": "区县级",
        "areaRange": area_range,
    }


def fetch_policy_summaries(
    *,
    area_range: str,
    page_size: int,
    expected_count: int,
    strict_count: bool,
    timeout: float,
) -> list[dict[str, Any]]:
    first_payload = request_json(
        "/policyFile/list",
        list_params(area_range, 1, page_size),
        timeout=timeout,
    )
    first_data = first_payload["data"]
    row_count = int(first_data.get("rowCount", 0))
    total_pages = int(first_data.get("totalPage", 1))

    if expected_count and row_count != expected_count:
        message = f"API rowCount is {row_count}, expected {expected_count}."
        if strict_count:
            raise PolicyDownloadError(message)
        print(f"Warning: {message}", file=sys.stderr)

    summaries = list(first_data.get("pageList") or [])
    for page_num in range(2, total_pages + 1):
        payload = request_json(
            "/policyFile/list",
            list_params(area_range, page_num, page_size),
            timeout=timeout,
        )
        summaries.extend(payload["data"].get("pageList") or [])

    return summaries


def fetch_policy_detail(info_id: str, *, timeout: float) -> PolicyFile:
    payload = request_json(
        "/admin/policyFile/info",
        {"id": info_id},
        timeout=timeout,
    )
    data = payload["data"]
    files = tuple(
        Attachment(name=str(item.get("name") or ""), url=str(item.get("url") or ""))
        for item in (data.get("file") or [])
        if item.get("url")
    )
    return PolicyFile(
        title=str(data.get("title") or ""),
        list_id=data.get("id"),
        info_id=str(data.get("infoId") or info_id),
        publish_time=data.get("publishTime"),
        publish_unit=data.get("publishUnit"),
        issue_no=data.get("issueNo"),
        issue_level=data.get("issueLevel"),
        policy_label=data.get("policyLabel"),
        etp_label=data.get("etpLabel"),
        content=data.get("content"),
        attachments=files,
    )


def is_pdf_attachment(attachment: Attachment) -> bool:
    name = attachment.name.lower()
    url = attachment.url.lower()
    return name.endswith(".pdf") or ".pdf" in url


def file_view_url(relative_url: str) -> str:
    query = urllib.parse.urlencode({"url": relative_url}, safe="/")
    return f"{API_BASE}/files/view?{query}"


def sanitize_filename(value: str, *, fallback: str, max_length: int = 120) -> str:
    value = value.strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if len(value) <= max_length:
        return value
    suffix = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    keep = max_length - len(suffix) - 1
    return f"{value[:keep].rstrip()}_{suffix}"


def unique_path(path: Path, used: set[Path]) -> Path:
    if path not in used:
        used.add(path)
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if candidate not in used and not candidate.exists():
            used.add(candidate)
            return candidate
        counter += 1


def is_valid_pdf(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 5:
        return False
    with path.open("rb") as file:
        return file.read(5) == b"%PDF-"


def download_attachment(
    attachment: Attachment,
    destination: Path,
    *,
    timeout: float,
    force: bool,
) -> None:
    if destination.exists() and not force:
        if destination.suffix.lower() != ".pdf" or is_valid_pdf(destination):
            return

    url = file_view_url(attachment.url)
    temp_path = destination.with_suffix(destination.suffix + ".part")
    data = make_request(url, timeout=timeout, headers={"Accept": "application/pdf,*/*"})
    temp_path.write_bytes(data)

    if destination.suffix.lower() == ".pdf" and not is_valid_pdf(temp_path):
        preview = data[:200].decode("utf-8", errors="replace")
        temp_path.unlink(missing_ok=True)
        raise PolicyDownloadError(f"Downloaded file is not a PDF: {url}. Preview: {preview}")

    temp_path.replace(destination)


def load_mineru_api_key(cli_key: str, key_file: Path | None) -> str:
    if cli_key.strip():
        return cli_key.strip()

    env_key = os.environ.get("MINERU_API_KEY", "").strip()
    if env_key:
        return env_key

    if key_file and key_file.exists():
        tree = ast.parse(key_file.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "API_KEY":
                        value = ast.literal_eval(node.value)
                        if isinstance(value, str) and value.strip():
                            return value.strip()

    raise PolicyDownloadError(
        "MinerU API key not found. Set MINERU_API_KEY, pass --mineru-api-key, "
        "or pass --mineru-key-file pointing to a Python file with API_KEY = '...'."
    )


def get_pdf_page_count(pdf_path: Path, *, timeout: float) -> int:
    if importlib.util.find_spec("PyPDF2") is not None:
        try:
            from PyPDF2 import PdfReader  # type: ignore

            reader = PdfReader(str(pdf_path))
            return len(reader.pages)
        except Exception:
            pass

    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        completed = subprocess.run(
            [pdfinfo, str(pdf_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode == 0:
            match = re.search(r"^Pages:\s+(\d+)\s*$", completed.stdout, re.MULTILINE)
            if match:
                return int(match.group(1))

    return 0


def iter_page_ranges(page_count: int, chunk_size: int) -> list[str | None]:
    if page_count <= 0:
        return [None]
    ranges: list[str | None] = []
    for start_page in range(1, page_count + 1, chunk_size):
        end_page = min(start_page + chunk_size - 1, page_count)
        ranges.append(f"{start_page}-{end_page}")
    return ranges


def extract_pages_with_pymupdf(pdf_path: Path) -> list[str]:
    import fitz  # type: ignore

    doc = fitz.open(str(pdf_path))
    try:
        return [page.get_text("text", sort=True).strip() for page in doc]
    finally:
        doc.close()


def extract_pages_with_pdftotext(pdf_path: Path, *, timeout: float) -> list[str]:
    executable = shutil.which("pdftotext")
    if not executable:
        raise PolicyDownloadError(
            "No PDF text extractor found. Install pymupdf with `python3 -m pip install pymupdf` "
            "or install Poppler so `pdftotext` is on PATH."
        )

    completed = subprocess.run(
        [executable, "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise PolicyDownloadError(
            f"pdftotext failed for {pdf_path}: {completed.stderr.strip()}"
        )
    return [page.strip() for page in completed.stdout.split("\f")]


def extract_pdf_pages(pdf_path: Path, *, timeout: float) -> tuple[str, list[str]]:
    if importlib.util.find_spec("fitz") is not None:
        return "pymupdf", extract_pages_with_pymupdf(pdf_path)
    return "pdftotext", extract_pages_with_pdftotext(pdf_path, timeout=timeout)


def ensure_tesseract_langs(lang_expr: str) -> None:
    executable = shutil.which("tesseract")
    if not executable:
        raise PolicyDownloadError("OCR requested but `tesseract` is not on PATH.")

    completed = subprocess.run(
        [executable, "--list-langs"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise PolicyDownloadError(f"Could not list tesseract languages: {completed.stderr}")

    available = {
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip() and not line.lower().startswith("list of available")
    }
    requested = {part for part in re.split(r"[+,\s]+", lang_expr) if part}
    missing = sorted(requested - available)
    if missing:
        raise PolicyDownloadError(
            "OCR language data missing: "
            + ", ".join(missing)
            + ". Install Chinese language data, for example `brew install tesseract-lang`, "
            + "or pass a language that exists in `tesseract --list-langs`."
        )


def ocr_pdf_pages(
    pdf_path: Path,
    page_numbers: list[int],
    *,
    lang: str,
    dpi: int,
    timeout: float,
) -> dict[int, str]:
    if not page_numbers:
        return {}
    pdftoppm = shutil.which("pdftoppm")
    tesseract = shutil.which("tesseract")
    if not pdftoppm:
        raise PolicyDownloadError("OCR requested but `pdftoppm` is not on PATH.")
    if not tesseract:
        raise PolicyDownloadError("OCR requested but `tesseract` is not on PATH.")

    ensure_tesseract_langs(lang)
    results: dict[int, str] = {}
    with tempfile.TemporaryDirectory(prefix="xiongan_policy_ocr_") as temp_dir:
        temp_path = Path(temp_dir)
        for page_number in page_numbers:
            image_prefix = temp_path / f"page_{page_number}"
            render = subprocess.run(
                [
                    pdftoppm,
                    "-r",
                    str(dpi),
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-png",
                    str(pdf_path),
                    str(image_prefix),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if render.returncode != 0:
                raise PolicyDownloadError(
                    f"pdftoppm failed for {pdf_path} page {page_number}: {render.stderr.strip()}"
                )

            images = sorted(temp_path.glob(f"page_{page_number}-*.png"))
            if not images:
                raise PolicyDownloadError(f"pdftoppm produced no image for page {page_number}.")

            ocr = subprocess.run(
                [tesseract, str(images[0]), "stdout", "-l", lang, "--psm", "6"],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if ocr.returncode != 0:
                raise PolicyDownloadError(
                    f"tesseract failed for {pdf_path} page {page_number}: {ocr.stderr.strip()}"
                )
            results[page_number] = ocr.stdout.strip()
    return results


def compact_blank_lines(lines: Iterable[str]) -> list[str]:
    output: list[str] = []
    blank = False
    for line in lines:
        cleaned = line.rstrip()
        if not cleaned:
            if not blank:
                output.append("")
            blank = True
            continue
        output.append(cleaned)
        blank = False
    while output and output[-1] == "":
        output.pop()
    return output


def markdown_metadata(policy: PolicyFile, source_rel_path: str, source_url: str) -> str:
    rows = [
        ("infoId", policy.info_id),
        ("发布时间", policy.publish_time),
        ("发文单位", policy.publish_unit),
        ("文件级别", policy.issue_level),
        ("发文文号", policy.issue_no),
        ("政策标签", policy.policy_label),
        ("适用企业", policy.etp_label),
        ("原始文件", source_rel_path),
        ("来源 URL", source_url),
    ]
    lines = ["| 字段 | 值 |", "| --- | --- |"]
    for key, value in rows:
        value_text = "" if value is None else str(value)
        value_text = value_text.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {key} | {value_text} |")
    return "\n".join(lines)


def write_markdown(
    *,
    policy: PolicyFile,
    pdf_path: Path,
    md_path: Path,
    source_url: str,
    timeout: float,
    force: bool,
    ocr: bool,
    ocr_lang: str,
    ocr_dpi: int,
    ocr_min_chars: int,
) -> str:
    if md_path.exists() and not force:
        return "skipped"

    extractor_name, pages = extract_pdf_pages(pdf_path, timeout=timeout)
    if ocr:
        page_numbers = [
            index
            for index, text in enumerate(pages, start=1)
            if len(re.sub(r"\s+", "", text or "")) < ocr_min_chars
        ]
        if page_numbers:
            ocr_results = ocr_pdf_pages(
                pdf_path,
                page_numbers,
                lang=ocr_lang,
                dpi=ocr_dpi,
                timeout=timeout,
            )
            for page_number, ocr_text in ocr_results.items():
                if ocr_text:
                    pages[page_number - 1] = ocr_text
            extractor_name = f"{extractor_name} + tesseract({ocr_lang})"
    rel_pdf = os.path.relpath(pdf_path, start=md_path.parent)

    lines: list[str] = [
        f"# {policy.title or pdf_path.stem}",
        "",
        markdown_metadata(policy, rel_pdf, source_url),
        "",
        f"> Extracted with `{extractor_name}`.",
        "",
    ]

    non_empty_pages = 0
    for index, page_text in enumerate(pages, start=1):
        if page_text.strip():
            non_empty_pages += 1
        lines.append(f"## Page {index}")
        lines.append("")
        lines.extend(compact_blank_lines(page_text.splitlines()))
        if not page_text.strip():
            lines.append("_No extractable text on this page._")
        lines.append("")

    if non_empty_pages == 0:
        lines.append(
            "> No extractable text was found. This PDF may be scanned; OCR is required for text output."
        )
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return "converted"


class SimpleHTMLToMarkdown(HTMLParser):
    block_tags = {
        "p", "div", "section", "article", "br", "tr", "table", "ul", "ol",
        "li", "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.link_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.block_tags:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            attrs_dict = dict(attrs)
            self.link_href = attrs_dict.get("href")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a":
            self.link_href = None
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        text = html.unescape(data)
        if not text.strip():
            return
        if self.link_href:
            self.parts.append(f"[{text.strip()}]({self.link_href})")
        else:
            self.parts.append(text)

    def markdown(self) -> str:
        text = "".join(self.parts)
        lines = compact_blank_lines(line.strip() for line in text.splitlines())
        return "\n".join(lines)


def html_to_markdown(value: str) -> str:
    parser = SimpleHTMLToMarkdown()
    parser.feed(value or "")
    return parser.markdown()


def write_content_markdown(
    *,
    policy: PolicyFile,
    md_path: Path,
    force: bool,
) -> str:
    if md_path.exists() and not force:
        return "skipped"
    content_md = html_to_markdown(policy.content or "")
    lines = [
        f"# {policy.title}",
        "",
        markdown_metadata(policy, "", ""),
        "",
        "> Extracted from policy detail `content` field.",
        "",
    ]
    if content_md.strip():
        lines.extend(content_md.splitlines())
    else:
        lines.append("_No extractable detail content._")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return "converted"


def policy_metadata_markdown(policy: PolicyFile, *, attachment_count: int) -> str:
    rows = [
        ("infoId", policy.info_id),
        ("发布时间", policy.publish_time),
        ("发文单位", policy.publish_unit),
        ("文件级别", policy.issue_level),
        ("发文文号", policy.issue_no),
        ("政策标签", policy.policy_label),
        ("适用企业", policy.etp_label),
        ("附件数量", attachment_count),
    ]
    lines = ["| 字段 | 值 |", "| --- | --- |"]
    for key, value in rows:
        value_text = "" if value is None else str(value)
        value_text = value_text.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {key} | {value_text} |")
    return "\n".join(lines)


def policy_markdown_filename(index: int, policy: PolicyFile) -> str:
    return sanitize_filename(
        f"{index:03d}_{policy.publish_time or 'unknown'}_{policy.title}",
        fallback=f"{index:03d}_{policy.info_id}",
    )


def append_policy_content_section(job: PolicyMarkdownJob) -> None:
    content_md = html_to_markdown(job.policy.content or "")
    if not content_md.strip():
        return
    job.sections.append("\n".join(["## 正文", "", content_md.strip()]))


def policy_detail_content_markdown(policy: PolicyFile) -> str:
    return html_to_markdown(policy.content or "").strip()


def compact_match_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def is_main_pdf_document(policy: PolicyFile, attachment: Attachment, attachment_index: int) -> bool:
    if policy_detail_content_markdown(policy) or not is_pdf_attachment(attachment):
        return False

    pdf_attachments = [item for item in policy.attachments if is_pdf_attachment(item)]
    if len(policy.attachments) == 1 or len(pdf_attachments) == 1:
        return True

    policy_key = compact_match_text(policy.title)
    attachment_key = compact_match_text(Path(attachment.name or attachment.url).stem)
    if policy_key and attachment_key and (
        policy_key in attachment_key or attachment_key in policy_key
    ):
        return True

    return attachment_index == 1 and attachment == pdf_attachments[0]


def relpath_for_markdown(path: Path, *, start: Path) -> str:
    return os.path.relpath(path, start=start).replace(os.sep, "/")


def markdown_source_section(
    *,
    heading: str,
    job: PolicyMarkdownJob,
    source_path: Path,
    source_url: str,
    extractor: str,
    body: str,
) -> str:
    rel_source = relpath_for_markdown(source_path, start=job.md_path.parent)
    lines = [
        heading,
        "",
        f"- 原始文件：`{rel_source}`",
        f"- 来源 URL：{source_url}",
        f"- 解析方式：`{extractor}`",
        "",
    ]
    body = body.strip()
    if body:
        lines.extend(body.splitlines())
    else:
        lines.append("_No extractable content._")
    return "\n".join(lines).strip()


def markdown_main_document_section(
    *,
    job: PolicyMarkdownJob,
    source_path: Path,
    source_url: str,
    extractor: str,
    body: str,
) -> str:
    return markdown_source_section(
        heading="## 正文",
        job=job,
        source_path=source_path,
        source_url=source_url,
        extractor=extractor,
        body=body,
    )


def markdown_attachment_section(
    *,
    job: PolicyMarkdownJob,
    attachment_index: int,
    attachment: Attachment,
    source_path: Path,
    source_url: str,
    extractor: str,
    body: str,
) -> str:
    title = attachment.name or source_path.name
    return markdown_source_section(
        heading=f"## 附件 {attachment_index}：{title}",
        job=job,
        source_path=source_path,
        source_url=source_url,
        extractor=extractor,
        body=body,
    )


def append_pdf_policy_section(
    *,
    job: PolicyMarkdownJob,
    attachment_index: int,
    attachment: Attachment,
    source_path: Path,
    source_url: str,
    extractor: str,
    body: str,
) -> None:
    if is_main_pdf_document(job.policy, attachment, attachment_index):
        job.sections.append(
            markdown_main_document_section(
                job=job,
                source_path=source_path,
                source_url=source_url,
                extractor=extractor,
                body=body,
            )
        )
        return
    job.sections.append(
        markdown_attachment_section(
            job=job,
            attachment_index=attachment_index,
            attachment=attachment,
            source_path=source_path,
            source_url=source_url,
            extractor=extractor,
            body=body,
        )
    )


def append_attachment_error_section(
    *,
    job: PolicyMarkdownJob,
    attachment_index: int,
    attachment: Attachment,
    source_path: Path,
    source_url: str,
    error: str,
    as_main_document: bool = False,
) -> None:
    job.errors.append(f"{attachment.name or source_path.name}: {error}")
    if as_main_document:
        job.sections.append(
            markdown_main_document_section(
                job=job,
                source_path=source_path,
                source_url=source_url,
                extractor="error",
                body=f"> 解析失败：{error}",
            )
        )
        return
    job.sections.append(
        markdown_attachment_section(
            job=job,
            attachment_index=attachment_index,
            attachment=attachment,
            source_path=source_path,
            source_url=source_url,
            extractor="error",
            body=f"> 解析失败：{error}",
        )
    )


def strip_generated_markdown_header(markdown: str) -> str:
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("> Extracted with") or line.startswith("> Converted with"):
            body = lines[index + 1 :]
            while body and not body[0].strip():
                body.pop(0)
            return "\n".join(body).strip()
    return markdown.strip()


def rewrite_relative_markdown_links(markdown: str, *, from_dir: Path, to_dir: Path) -> str:
    rel_from_dir = relpath_for_markdown(from_dir, start=to_dir)
    if rel_from_dir == ".":
        return markdown

    def should_rewrite(destination: str) -> bool:
        destination = destination.strip()
        if not destination:
            return False
        if destination.startswith(("#", "/", "<#")):
            return False
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", destination):
            return False
        return True

    def markdown_link_repl(match: re.Match[str]) -> str:
        prefix = match.group(1)
        destination = match.group(2)
        if not should_rewrite(destination):
            return match.group(0)
        return f"{prefix}{rel_from_dir}/{destination}"

    markdown = re.sub(r"(!?\[[^\]]*\]\()([^)\s]+)", markdown_link_repl, markdown)

    def html_src_repl(match: re.Match[str]) -> str:
        prefix = match.group(1)
        destination = match.group(2)
        if not should_rewrite(destination):
            return match.group(0)
        return f'{prefix}{rel_from_dir}/{destination}'

    return re.sub(r'(src=")([^"]+)', html_src_repl, markdown)


def read_existing_pdf_markdown(
    *,
    existing_md_path: Path,
    target_md_path: Path,
) -> str:
    markdown = existing_md_path.read_text(encoding="utf-8", errors="replace")
    markdown = strip_generated_markdown_header(markdown)
    return rewrite_relative_markdown_links(
        markdown,
        from_dir=existing_md_path.parent,
        to_dir=target_md_path.parent,
    )


def attachment_extension(attachment: Attachment) -> str:
    raw = (attachment.name or attachment.url).split("?", 1)[0]
    suffix = Path(raw).suffix.lower()
    return suffix or ".bin"


def convert_with_pandoc(source_path: Path, md_path: Path, *, timeout: float) -> bool:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return False
    completed = subprocess.run(
        [pandoc, str(source_path), "-t", "gfm", "-o", str(md_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.returncode == 0 and md_path.exists()


def extract_with_textutil(source_path: Path, *, timeout: float) -> str | None:
    textutil = shutil.which("textutil")
    if not textutil:
        return None
    completed = subprocess.run(
        [textutil, "-convert", "txt", "-stdout", str(source_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def extract_docx_plain(source_path: Path) -> str:
    import xml.etree.ElementTree as ET

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(source_path) as archive:
        document = archive.read("word/document.xml")
    root = ET.fromstring(document)
    lines: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        texts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        line = "".join(texts).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


def convert_xlsx_to_markdown(source_path: Path) -> str:
    try:
        import openpyxl  # type: ignore
    except Exception as exc:
        raise PolicyDownloadError("openpyxl is required to convert .xlsx files.") from exc

    workbook = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(f"## Sheet: {sheet.title}")
        rows = []
        for row in sheet.iter_rows(values_only=True):
            values = ["" if value is None else str(value).replace("\n", " ") for value in row]
            if any(value.strip() for value in values):
                rows.append(values)
        if not rows:
            parts.append("")
            parts.append("_Empty sheet._")
            continue
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        header = normalized[0]
        parts.append("")
        parts.append("| " + " | ".join(cell.replace("|", "\\|") for cell in header) + " |")
        parts.append("| " + " | ".join("---" for _ in header) + " |")
        for row in normalized[1:]:
            parts.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
        parts.append("")
    return "\n".join(parts).strip()


def convert_attachment_to_markdown_text(
    source_path: Path,
    *,
    timeout: float,
) -> tuple[str, str]:
    ext = source_path.suffix.lower()

    if ext in {".docx", ".doc", ".rtf"}:
        with tempfile.TemporaryDirectory(prefix="xiongan_policy_doc_") as temp_dir:
            temp_md_path = Path(temp_dir) / "converted.md"
            if convert_with_pandoc(source_path, temp_md_path, timeout=timeout):
                return "pandoc", temp_md_path.read_text(encoding="utf-8", errors="replace")

    if ext == ".docx":
        try:
            return "DOCX XML", extract_docx_plain(source_path)
        except Exception as exc:
            text = extract_with_textutil(source_path, timeout=timeout)
            if text is not None:
                return "textutil", text
            raise PolicyDownloadError(
                f"Could not convert .docx with DOCX XML or textutil: {exc}"
            ) from exc

    if ext in {".doc", ".rtf"}:
        text = extract_with_textutil(source_path, timeout=timeout)
        if text is not None:
            return "textutil", text
        raise PolicyDownloadError(f"Could not convert {ext} with pandoc or textutil.")

    if ext == ".xlsx":
        return "openpyxl", convert_xlsx_to_markdown(source_path)

    if ext in {".txt", ".csv", ".md"}:
        return "plain text", source_path.read_text(encoding="utf-8", errors="replace")

    if ext in {".html", ".htm"}:
        text = source_path.read_text(encoding="utf-8", errors="replace")
        return "html", html_to_markdown(text)

    if ext == ".json":
        text = source_path.read_text(encoding="utf-8", errors="replace")
        return "plain text", f"```json\n{text.strip()}\n```"

    raise PolicyDownloadError(f"Unsupported attachment type: {ext}")


def write_attachment_markdown(
    *,
    policy: PolicyFile,
    source_path: Path,
    md_path: Path,
    source_url: str,
    timeout: float,
    force: bool,
) -> str:
    if md_path.exists() and not force:
        return "skipped"

    ext = source_path.suffix.lower()
    rel_source = os.path.relpath(source_path, start=md_path.parent)
    header = [
        f"# {policy.title or source_path.stem}",
        "",
        markdown_metadata(policy, rel_source, source_url),
        "",
    ]

    if ext in {".docx", ".doc"} and convert_with_pandoc(source_path, md_path, timeout=timeout):
        existing = md_path.read_text(encoding="utf-8", errors="replace")
        md_path.write_text("\n".join(header + ["> Converted with `pandoc`.", "", existing]), encoding="utf-8")
        return "converted"

    if ext == ".docx":
        try:
            text = extract_docx_plain(source_path)
            extractor = "DOCX XML"
        except Exception:
            text = extract_with_textutil(source_path, timeout=timeout)
            extractor = "textutil"
            if text is None:
                raise
        md_path.write_text(
            "\n".join(header + [f"> Extracted with `{extractor}`.", "", text.strip(), ""]),
            encoding="utf-8",
        )
        return "converted"

    if ext == ".doc":
        text = extract_with_textutil(source_path, timeout=timeout)
        if text is None:
            raise PolicyDownloadError("Could not convert .doc with pandoc or textutil.")
        md_path.write_text(
            "\n".join(header + ["> Extracted with `textutil`.", "", text.strip(), ""]),
            encoding="utf-8",
        )
        return "converted"

    if ext == ".xlsx":
        text = convert_xlsx_to_markdown(source_path)
        md_path.write_text(
            "\n".join(header + ["> Extracted with `openpyxl`.", "", text, ""]),
            encoding="utf-8",
        )
        return "converted"

    if ext in {".txt", ".csv"}:
        text = source_path.read_text(encoding="utf-8", errors="replace")
        md_path.write_text("\n".join(header + ["", text, ""]), encoding="utf-8")
        return "converted"

    raise PolicyDownloadError(f"Unsupported attachment type: {ext}")


def build_mineru_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def submit_mineru_batch(
    files_payload: list[dict[str, Any]],
    file_paths: list[Path],
    *,
    api_key: str,
    model_version: str,
    timeout: float,
) -> str:
    payload = {"files": files_payload, "model_version": model_version}
    result = request_json_url(
        "https://mineru.net/api/v4/file-urls/batch",
        method="POST",
        payload=payload,
        headers=build_mineru_headers(api_key),
        timeout=timeout,
    )
    if result.get("code") != 0:
        raise PolicyDownloadError(f"MinerU upload-url request failed: {result.get('msg') or result}")

    data = result["data"]
    batch_id = str(data["batch_id"])
    upload_urls = data["file_urls"]
    if len(upload_urls) != len(file_paths):
        raise PolicyDownloadError(
            f"MinerU returned {len(upload_urls)} upload URLs for {len(file_paths)} files."
        )

    for index, upload_url in enumerate(upload_urls, start=1):
        pdf_path = file_paths[index - 1]
        put_file_to_presigned_url(upload_url, pdf_path, timeout=max(timeout, 300))

    return batch_id


def put_file_to_presigned_url(upload_url: str, pdf_path: Path, *, timeout: float) -> None:
    parsed = urllib.parse.urlsplit(upload_url)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    if parsed.scheme != "https":
        raise PolicyDownloadError(f"Unexpected MinerU upload URL scheme: {parsed.scheme}")

    data = pdf_path.read_bytes()
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
    try:
        connection.request(
            "PUT",
            target,
            body=data,
            headers={
                "Host": parsed.netloc,
                "Content-Length": str(len(data)),
            },
        )
        response = connection.getresponse()
        body = response.read()
        if response.status not in (200, 201, 204):
            preview = body[:500].decode("utf-8", errors="replace")
            raise PolicyDownloadError(
                f"MinerU upload failed for {pdf_path.name}: HTTP {response.status}: {preview}"
            )
    finally:
        connection.close()


def poll_mineru_batch(
    batch_id: str,
    *,
    api_key: str,
    timeout: float,
    poll_interval: float,
    max_poll_seconds: float,
) -> list[dict[str, Any]]:
    url = f"https://mineru.net/api/v4/extract-results/batch/{batch_id}"
    running_states = {
        "running",
        "processing",
        "init",
        "waiting",
        "waiting-file",
        "pending",
        "queueing",
    }
    started_at = time.time()
    while True:
        if time.time() - started_at > max_poll_seconds:
            raise PolicyDownloadError(f"MinerU batch {batch_id} timed out.")

        result = request_json_url(
            url,
            headers=build_mineru_headers(api_key),
            timeout=timeout,
        )
        if result.get("code") != 0:
            raise PolicyDownloadError(f"MinerU polling failed: {result.get('msg') or result}")

        extract_results = result.get("data", {}).get("extract_result") or []
        state_counts: dict[str, int] = {}
        is_finished = True
        for item in extract_results:
            state = str(item.get("state") or "")
            state_counts[state] = state_counts.get(state, 0) + 1
            if state in running_states:
                is_finished = False

        status = ", ".join(f"{key}: {value}" for key, value in sorted(state_counts.items()))
        print(f"MinerU batch {batch_id}: {status or 'no result yet'}")
        if extract_results and is_finished:
            return extract_results

        time.sleep(poll_interval)


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        for member in zip_file.infolist():
            target = (destination / member.filename).resolve()
            if not str(target).startswith(str(base)):
                raise PolicyDownloadError(f"Unsafe zip member path: {member.filename}")
        zip_file.extractall(destination)


def find_mineru_full_md(extract_dir: Path) -> Path | None:
    matches = list(extract_dir.rglob("full.md"))
    return matches[0] if matches else None


def find_mineru_images_dir(full_md_path: Path) -> Path | None:
    images_dir = full_md_path.parent / "images"
    return images_dir if images_dir.exists() and images_dir.is_dir() else None


def mineru_markdown_body(
    *,
    job: PdfConversionJob,
    chunk_infos: list[dict[str, Any]],
    temp_root: Path,
    force: bool,
) -> str:
    chunk_infos.sort(key=lambda item: int(item["chunk_index"]))
    if not all(item.get("state") == "done" and item.get("zip_path") for item in chunk_infos):
        states = ", ".join(str(item.get("state")) for item in chunk_infos)
        raise PolicyDownloadError(f"MinerU did not finish all chunks for {job.pdf_path.name}: {states}")

    attachment_assets_name = sanitize_filename(
        f"attachment_{job.attachment_index}_{job.pdf_path.stem}",
        fallback=f"attachment_{job.attachment_index}",
        max_length=80,
    )
    assets_dir = job.policy_job.md_path.parent / f"{job.policy_job.md_path.stem}_assets" / attachment_assets_name
    if assets_dir.exists() and force:
        shutil.rmtree(assets_dir)

    combined: list[str] = []

    for chunk in chunk_infos:
        extract_dir = temp_root / str(chunk["data_id"])
        full_md_path = find_mineru_full_md(extract_dir)
        if not full_md_path:
            raise PolicyDownloadError(f"MinerU result missing full.md for {job.pdf_path.name}")

        content = full_md_path.read_text(encoding="utf-8")
        chunk_index = int(chunk["chunk_index"])
        chunk_assets_dir = assets_dir / f"chunk_{chunk_index}"
        images_dir = find_mineru_images_dir(full_md_path)
        if images_dir:
            if chunk_assets_dir.exists():
                shutil.rmtree(chunk_assets_dir)
            shutil.copytree(images_dir, chunk_assets_dir)
            rel_chunk_assets = relpath_for_markdown(
                chunk_assets_dir,
                start=job.policy_job.md_path.parent,
            )
            content = content.replace("](images/", f"]({rel_chunk_assets}/")
            content = content.replace('src="images/', f'src="{rel_chunk_assets}/')

        if len(chunk_infos) > 1:
            combined.extend(
                [
                    f"## MinerU Chunk {chunk_index + 1}",
                    "",
                    f"_Pages: {chunk.get('page_range') or 'unknown'}_",
                    "",
                ]
            )
        combined.extend(compact_blank_lines(content.splitlines()))
        combined.extend(["", "---", ""])

    while combined and combined[-1] in ("", "---"):
        combined.pop()
    return "\n".join(combined).strip()


def append_mineru_markdown(
    *,
    job: PdfConversionJob,
    chunk_infos: list[dict[str, Any]],
    temp_root: Path,
    force: bool,
) -> str:
    body = mineru_markdown_body(
        job=job,
        chunk_infos=chunk_infos,
        temp_root=temp_root,
        force=force,
    )
    append_pdf_policy_section(
        job=job.policy_job,
        attachment_index=job.attachment_index,
        attachment=job.attachment,
        source_path=job.pdf_path,
        source_url=job.source_url,
        extractor="MinerU",
        body=body,
    )
    return "converted"


def convert_jobs_with_mineru(
    jobs: list[PdfConversionJob],
    *,
    api_key: str,
    model_version: str,
    chunk_size: int,
    batch_task_limit: int,
    timeout: float,
    poll_interval: float,
    max_poll_seconds: float,
    force: bool,
) -> tuple[int, int]:
    pending_jobs = list(jobs)
    if not pending_jobs:
        return 0, 0

    tasks: list[MinerUTask] = []
    files_payload: list[dict[str, Any]] = []
    file_paths: list[Path] = []
    for job_index, job in enumerate(pending_jobs):
        page_count = get_pdf_page_count(job.pdf_path, timeout=timeout)
        ranges = iter_page_ranges(page_count, chunk_size)
        total_chunks = len(ranges)
        for chunk_index, page_range in enumerate(ranges):
            data_id = uuid.uuid4().hex
            payload = {"name": job.pdf_path.name, "data_id": data_id}
            if page_range:
                payload["page_ranges"] = page_range
            files_payload.append(payload)
            file_paths.append(job.pdf_path)
            tasks.append(
                MinerUTask(
                    data_id=data_id,
                    job_index=job_index,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    page_range=page_range,
                )
            )

    task_by_id = {task.data_id: task for task in tasks}
    result_chunks_by_job: dict[int, list[dict[str, Any]]] = {
        index: [] for index in range(len(pending_jobs))
    }
    converted_count = 0
    error_count = 0

    with tempfile.TemporaryDirectory(prefix="xiongan_mineru_") as temp_dir:
        temp_root = Path(temp_dir)
        for start in range(0, len(tasks), batch_task_limit):
            end = min(start + batch_task_limit, len(tasks))
            batch_payload = files_payload[start:end]
            batch_paths = file_paths[start:end]
            print(f"Submitting MinerU tasks {start + 1}-{end} of {len(tasks)}...")
            batch_id = submit_mineru_batch(
                batch_payload,
                batch_paths,
                api_key=api_key,
                model_version=model_version,
                timeout=timeout,
            )
            extract_results = poll_mineru_batch(
                batch_id,
                api_key=api_key,
                timeout=timeout,
                poll_interval=poll_interval,
                max_poll_seconds=max_poll_seconds,
            )

            for item in extract_results:
                data_id = str(item.get("data_id") or "")
                task = task_by_id.get(data_id)
                if not task:
                    continue
                chunk_info = {
                    "data_id": data_id,
                    "chunk_index": task.chunk_index,
                    "total_chunks": task.total_chunks,
                    "page_range": task.page_range,
                    "state": item.get("state"),
                    "zip_url": item.get("full_zip_url"),
                    "zip_path": None,
                }
                if chunk_info["state"] == "done" and chunk_info["zip_url"]:
                    zip_path = temp_root / f"{data_id}.zip"
                    zip_bytes = make_request(
                        str(chunk_info["zip_url"]),
                        timeout=max(timeout, 300),
                    )
                    zip_path.write_bytes(zip_bytes)
                    extract_dir = temp_root / data_id
                    safe_extract_zip(zip_path, extract_dir)
                    chunk_info["zip_path"] = str(zip_path)
                result_chunks_by_job[task.job_index].append(chunk_info)

        for job_index, job in enumerate(pending_jobs):
            try:
                status = append_mineru_markdown(
                    job=job,
                    chunk_infos=result_chunks_by_job.get(job_index, []),
                    temp_root=temp_root,
                    force=force,
                )
                if status == "converted":
                    converted_count += 1
            except Exception as exc:
                append_attachment_error_section(
                    job=job.policy_job,
                    attachment_index=job.attachment_index,
                    attachment=job.attachment,
                    source_path=job.pdf_path,
                    source_url=job.source_url,
                    error=str(exc),
                    as_main_document=is_main_pdf_document(
                        job.policy_job.policy,
                        job.attachment,
                        job.attachment_index,
                    ),
                )
                error_count += 1

    return converted_count, error_count


def write_policy_markdown(job: PolicyMarkdownJob, *, force: bool) -> str:
    lines: list[str] = [
        f"# {job.policy.title or job.md_path.stem}",
        "",
        policy_metadata_markdown(job.policy, attachment_count=len(job.policy.attachments)),
        "",
    ]
    if job.sections:
        for section in job.sections:
            lines.extend(section.strip().splitlines())
            lines.append("")
    else:
        lines.append("_No extractable detail content or supported attachments._")
        lines.append("")

    if job.errors:
        lines.extend(["## 解析问题", ""])
        for error in job.errors:
            lines.append(f"- {error}")
        lines.append("")

    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("")
    job.md_path.write_text("\n".join(lines), encoding="utf-8")
    job.row["status"] = "converted_with_errors" if job.errors else "converted"
    job.row["error"] = "；".join(job.errors)
    return str(job.row["status"])


def write_manifests(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    manifest_jsonl = output_dir / "manifest.jsonl"
    with manifest_jsonl.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    manifest_json = output_dir / "manifest.json"
    manifest_json.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    manifest_csv = output_dir / "manifest.csv"
    fields = [
        "index",
        "title",
        "info_id",
        "publish_time",
        "publish_unit",
        "issue_level",
        "attachment_count",
        "source_paths",
        "md_path",
        "status",
        "error",
    ]
    with manifest_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def process() -> int:
    args = parse_args()
    output_dir: Path = args.output_dir
    pdf_dir = output_dir / "pdfs"
    files_dir = output_dir / "files"
    legacy_md_dir = output_dir / "md"
    policy_md_dir = output_dir / "policy_md"
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)
    policy_md_dir.mkdir(parents=True, exist_ok=True)

    mineru_api_key = ""
    if args.converter == "mineru" and not args.dry_run:
        mineru_api_key = load_mineru_api_key(args.mineru_api_key, args.mineru_key_file)

    summaries = fetch_policy_summaries(
        area_range=args.area_range,
        page_size=args.page_size,
        expected_count=args.expected_count,
        strict_count=args.strict_count,
        timeout=args.timeout,
    )
    if args.limit:
        summaries = summaries[: args.limit]

    print(f"Fetched {len(summaries)} policy summaries.")

    rows: list[dict[str, Any]] = []
    policy_jobs: list[PolicyMarkdownJob] = []
    used_source_paths: set[Path] = set()
    used_policy_md_paths: set[Path] = set()
    mineru_jobs: list[PdfConversionJob] = []
    attachment_count = 0
    error_count = 0

    for index, summary in enumerate(summaries, start=1):
        info_id = str(summary.get("infoId") or "")
        fallback_policy = PolicyFile(
            title=str(summary.get("title") or f"policy_{index:03d}"),
            list_id=summary.get("id"),
            info_id=info_id or f"missing_info_id_{index:03d}",
            publish_time=summary.get("publishTime"),
            publish_unit=summary.get("publishUnit"),
            issue_no=summary.get("issueNo"),
            issue_level=summary.get("issueLevel"),
            policy_label=summary.get("policyLabel"),
            etp_label=summary.get("etpLabel"),
            content=None,
            attachments=(),
        )

        if not info_id:
            error_count += 1
            policy = fallback_policy
            md_path = unique_path(
                policy_md_dir / f"{policy_markdown_filename(index, policy)}.md",
                used_policy_md_paths,
            )
            row = {
                "index": index,
                "title": policy.title,
                "info_id": policy.info_id,
                "publish_time": policy.publish_time,
                "publish_unit": policy.publish_unit,
                "issue_level": policy.issue_level,
                "attachment_count": 0,
                "source_paths": "",
                "md_path": str(md_path),
                "status": "dry_run" if args.dry_run else "",
                "error": "Missing infoId",
            }
            job = PolicyMarkdownJob(
                policy=policy,
                index=index,
                md_path=md_path,
                row=row,
                sections=["## 抓取失败\n\n> Missing infoId"],
                errors=["Missing infoId"],
            )
            policy_jobs.append(job)
            rows.append(row)
            continue

        try:
            policy = fetch_policy_detail(info_id, timeout=args.timeout)
        except Exception as exc:
            error_count += 1
            policy = fallback_policy
            md_path = unique_path(
                policy_md_dir / f"{policy_markdown_filename(index, policy)}.md",
                used_policy_md_paths,
            )
            row = {
                "index": index,
                "title": policy.title,
                "info_id": policy.info_id,
                "publish_time": policy.publish_time,
                "publish_unit": policy.publish_unit,
                "issue_level": policy.issue_level,
                "attachment_count": 0,
                "source_paths": "",
                "md_path": str(md_path),
                "status": "dry_run" if args.dry_run else "",
                "error": str(exc),
            }
            job = PolicyMarkdownJob(
                policy=policy,
                index=index,
                md_path=md_path,
                row=row,
                sections=[f"## 抓取失败\n\n> {exc}"],
                errors=[str(exc)],
            )
            policy_jobs.append(job)
            rows.append(row)
            continue

        md_path = unique_path(
            policy_md_dir / f"{policy_markdown_filename(index, policy)}.md",
            used_policy_md_paths,
        )
        row = {
            "index": index,
            "title": policy.title,
            "info_id": policy.info_id,
            "publish_time": policy.publish_time,
            "publish_unit": policy.publish_unit,
            "issue_level": policy.issue_level,
            "attachment_count": len(policy.attachments),
            "source_paths": "",
            "md_path": str(md_path),
            "status": "dry_run" if args.dry_run else "",
            "error": "",
        }
        job = PolicyMarkdownJob(
            policy=policy,
            index=index,
            md_path=md_path,
            row=row,
            sections=[],
            errors=[],
        )
        append_policy_content_section(job)

        source_paths: list[str] = []
        attachment_count += len(policy.attachments)

        for attachment_index, attachment in enumerate(policy.attachments, start=1):
            is_pdf = is_pdf_attachment(attachment)
            source_url = file_view_url(attachment.url)
            extension = ".pdf" if is_pdf else attachment_extension(attachment)
            name_source = attachment.name or policy.title
            base_name = sanitize_filename(
                f"{index:03d}_{policy.publish_time or 'unknown'}_{name_source}",
                fallback=f"{index:03d}_{policy.info_id}_{attachment_index}",
            )
            if not base_name.lower().endswith(extension.lower()):
                base_name = f"{base_name}{extension}"
            source_dir = pdf_dir if is_pdf else files_dir
            source_path = unique_path(source_dir / base_name, used_source_paths)
            source_paths.append(str(source_path))

            if not is_pdf and args.skip_non_pdf:
                job.sections.append(
                    markdown_attachment_section(
                        job=job,
                        attachment_index=attachment_index,
                        attachment=attachment,
                        source_path=source_path,
                        source_url=source_url,
                        extractor="skipped",
                        body="> 已按 --skip-non-pdf 跳过该非 PDF 附件。",
                    )
                )
                continue

            try:
                if args.dry_run:
                    continue

                download_attachment(
                    attachment,
                    source_path,
                    timeout=args.timeout,
                    force=args.force,
                )
                if is_pdf:
                    cached_md_path = legacy_md_dir / f"{source_path.stem}.md"
                    if cached_md_path.exists() and not args.force:
                        cached_body = read_existing_pdf_markdown(
                            existing_md_path=cached_md_path,
                            target_md_path=job.md_path,
                        )
                        append_pdf_policy_section(
                            job=job,
                            attachment_index=attachment_index,
                            attachment=attachment,
                            source_path=source_path,
                            source_url=source_url,
                            extractor="MinerU (cached)",
                            body=cached_body,
                        )
                    elif args.converter == "mineru":
                        mineru_jobs.append(
                            PdfConversionJob(
                                policy_job=job,
                                attachment=attachment,
                                attachment_index=attachment_index,
                                pdf_path=source_path,
                                source_url=source_url,
                            )
                        )
                    else:
                        with tempfile.TemporaryDirectory(prefix="xiongan_policy_pdf_") as temp_dir:
                            temp_md_path = Path(temp_dir) / "pdf.md"
                            write_markdown(
                                policy=policy,
                                pdf_path=source_path,
                                md_path=temp_md_path,
                                source_url=source_url,
                                timeout=args.timeout,
                                force=True,
                                ocr=args.ocr,
                                ocr_lang=args.ocr_lang,
                                ocr_dpi=args.ocr_dpi,
                                ocr_min_chars=args.ocr_min_chars,
                            )
                            pdf_body = strip_generated_markdown_header(
                                temp_md_path.read_text(encoding="utf-8", errors="replace")
                            )
                        append_pdf_policy_section(
                            job=job,
                            attachment_index=attachment_index,
                            attachment=attachment,
                            source_path=source_path,
                            source_url=source_url,
                            extractor="local PDF",
                            body=pdf_body,
                        )
                else:
                    extractor, body = convert_attachment_to_markdown_text(
                        source_path,
                        timeout=args.timeout,
                    )
                    job.sections.append(
                        markdown_attachment_section(
                            job=job,
                            attachment_index=attachment_index,
                            attachment=attachment,
                            source_path=source_path,
                            source_url=source_url,
                            extractor=extractor,
                            body=body,
                        )
                    )
            except Exception as exc:
                error_count += 1
                append_attachment_error_section(
                    job=job,
                    attachment_index=attachment_index,
                    attachment=attachment,
                    source_path=source_path,
                    source_url=source_url,
                    error=str(exc),
                    as_main_document=is_pdf
                    and is_main_pdf_document(policy, attachment, attachment_index),
                )

            if args.delay:
                time.sleep(args.delay)

        row["source_paths"] = ";".join(source_paths)
        policy_jobs.append(job)
        rows.append(row)
        print(
            f"[{index}/{len(summaries)}] {policy.title} "
            f"({len(policy.attachments)} attachment(s))"
        )

    if mineru_jobs:
        try:
            mineru_converted, mineru_errors = convert_jobs_with_mineru(
                mineru_jobs,
                api_key=mineru_api_key,
                model_version=args.mineru_model_version,
                chunk_size=args.mineru_chunk_size,
                batch_task_limit=args.mineru_batch_task_limit,
                timeout=args.timeout,
                poll_interval=args.mineru_poll_interval,
                max_poll_seconds=args.mineru_max_poll_seconds,
                force=args.force,
            )
            error_count += mineru_errors
        except Exception as exc:
            for mineru_job in mineru_jobs:
                append_attachment_error_section(
                    job=mineru_job.policy_job,
                    attachment_index=mineru_job.attachment_index,
                    attachment=mineru_job.attachment,
                    source_path=mineru_job.pdf_path,
                    source_url=mineru_job.source_url,
                    error=str(exc),
                    as_main_document=is_main_pdf_document(
                        mineru_job.policy_job.policy,
                        mineru_job.attachment,
                        mineru_job.attachment_index,
                    ),
                )
            error_count += len(mineru_jobs)

    markdown_ready_count = 0
    if not args.dry_run:
        for job in policy_jobs:
            try:
                status = write_policy_markdown(job, force=args.force)
                if status in {"converted", "converted_with_errors", "skipped"}:
                    markdown_ready_count += 1
            except Exception as exc:
                job.row["status"] = "error"
                job.row["error"] = str(exc)
                error_count += 1
    else:
        markdown_ready_count = len(policy_jobs)

    write_manifests(output_dir, rows)

    print(f"Attachments seen: {attachment_count}")
    print(f"Policy Markdown files ready: {markdown_ready_count}")
    print(f"Errors: {error_count}")
    print(f"Policy Markdown directory: {policy_md_dir.resolve()}")
    print(f"Output directory: {output_dir.resolve()}")
    return 1 if error_count else 0


if __name__ == "__main__":
    try:
        raise SystemExit(process())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
