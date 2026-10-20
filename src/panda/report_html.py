"""A self-contained HTML view of a job's JSON reports.

The page is built from the same JSON reports a reviewer could read directly, so it
holds nothing they do not: kinds, counts, locations, detectors and dummy values,
never an original value. The template carries all of its CSS and script inline and
the page loads nothing, so it can be generated and opened with no network access.

Every visible string is written in English and Simplified Chinese side by side, and
the page shows one at a time. Switching language is then a CSS change with nothing
to fetch or re-render, and the page still reads in English with scripts disabled.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

from panda import __version__

REPORT_PAGE = "report.html"
RUN_REPORT = "run.json"

_KIND_LABELS = {
    "name": ("Names", "姓名"),
    "dob": ("Dates of birth", "出生日期"),
    "address": ("Addresses", "地址"),
    "phone": ("Phone numbers", "电话号码"),
    "email": ("Email addresses", "电子邮箱"),
    "account": ("Account numbers", "账号"),
    "id_number": ("ID numbers", "证件号码"),
}
_CONFIDENCE_LABELS = {
    "VALIDATED": ("Validated", "已校验"),
    "PATTERN": ("Pattern", "规则匹配"),
    "NER": ("Model guess", "模型推测"),
}
# Unknown outcomes fall back to the warning tone: a status this page does not
# recognise should never read as a clean result.
_OUTCOMES = {
    "sanitised": ("good", "Sanitised", "已脱敏"),
    "residual PII": ("bad", "PII still found", "仍含个人信息"),
    "processing error": ("bad", "Processing error", "处理出错"),
    "skipped/unsupported": ("warn", "Skipped", "已跳过"),
}
_OUTCOME_ORDER = ("sanitised", "skipped/unsupported", "processing error", "residual PII")


class ReportError(Exception):
    """The folder does not hold a readable job report. Messages are safe to print."""


def find_report_dir(folder: Path) -> Path:
    """Accept a report folder or the `<folder>-SANITISED` results folder around it."""
    if not folder.is_dir():
        raise ReportError("folder not found")
    if (folder / RUN_REPORT).is_file():
        return folder
    candidates = sorted(
        child for child in folder.iterdir()
        if child.is_dir() and not child.is_symlink() and (child / RUN_REPORT).is_file()
    )
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ReportError("more than one report folder found; name the one you want")
    raise ReportError(f"no {RUN_REPORT} found; give a results folder or its report folder")


def load_reports(report_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        run = json.loads((report_dir / RUN_REPORT).read_text(encoding="utf-8"))
        files = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(report_dir.glob("file-*.json"))
        ]
    except (OSError, UnicodeError, ValueError) as error:
        raise ReportError("report files could not be read as JSON") from error
    if not isinstance(run, dict) or not all(isinstance(item, dict) for item in files):
        raise ReportError("report files are not in the expected format")
    return run, files


def write_html_report(folder: Path, *, batch: str | None = None) -> Path:
    """Render `report.html` into the job's report folder and return its path.

    `batch` names the job in the heading; by default it comes from the report
    folder's name, which is wrong while a job is still staged.
    """
    report_dir = find_report_dir(folder)
    run, files = load_reports(report_dir)
    page = report_dir / REPORT_PAGE
    content = render(run, files, batch=batch_name(report_dir) if batch is None else batch)
    # Replace atomically so a browser never shows a half-written page.
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=report_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, page)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return page


def batch_name(report_dir: Path) -> str:
    return re.sub(r"-report$", "", report_dir.name) if report_dir.name.endswith("-report") else ""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _t(en: str, zh: str) -> str:
    """Both languages of one piece of text; CSS shows the selected one."""
    return f'<span lang="en">{_e(en)}</span><span lang="zh-Hans">{_e(zh)}</span>'


def _empty(en: str, zh: str, *, filtered: bool = False) -> str:
    # The filtered variant starts hidden; the page script shows it when filters
    # leave a file with no matching rows.
    attrs = ' class="empty nomatch" hidden' if filtered else ' class="empty"'
    return f"<p{attrs}>{_t(en, zh)}</p>"


def _aria(en: str, zh: str) -> str:
    """An aria-label the page script switches with the language."""
    return f'aria-label="{_e(en)}" data-aria-en="{_e(en)}" data-aria-zh="{_e(zh)}"'


def _number(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "–"


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _kind_label(kind: str) -> tuple[str, str]:
    fallback = kind.replace("_", " ").capitalize()
    return _KIND_LABELS.get(kind, (fallback, fallback))


def _kind_sort(kind: str) -> str:
    return _kind_label(kind)[0]


def _banner(run: dict[str, Any]) -> str:
    status = run.get("batch_status")
    if status == "complete":
        tone, mark = "good", "✓"
        title = ("Verified", "已核验")
        text = (
            "Every file was sanitised and a rescan found no remaining PII with the "
            "configured recognisers.",
            "所有文件均已脱敏，复检未发现已配置识别器可识别的个人信息。",
        )
    elif status == "partial":
        tone, mark = "warn", "!"
        title = ("Verified, but some files were left out", "已核验，但部分文件未纳入")
        text = (
            "The published files passed the rescan. Skipped or failed files are not in "
            "the output; see the Files table.",
            "已发布的文件通过了复检。被跳过或处理失败的文件不在输出中，详见“文件”列表。",
        )
    else:
        # Anything other than a known good status, including a missing one, is
        # presented as unverified.
        tone, mark = "bad", "✕"
        title = ("UNVERIFIED – may contain PII. Do not share.",
                 "未核验：可能含有个人信息，请勿分享。")
        text = (
            "The rescan found possible PII or could not check a file. Output was kept "
            "under UNVERIFIED for debugging only.",
            "复检发现可能的个人信息，或无法检查某个文件。输出保存在 UNVERIFIED "
            "文件夹中，仅供排查问题使用。",
        )
    caution = _t(
        "Automated detection can miss things: read the output before handing it off.",
        "自动检测可能有遗漏，交付前请人工通读输出内容。",
    )
    return (
        f'<div class="banner {tone}" role="status"><span class="icon" aria-hidden="true">'
        f'{mark}</span><div><h2 class="banner-title">{_t(*title)}</h2>'
        f'<p>{_t(*text)}</p><p class="caution">{caution}</p></div></div>'
    )


def _tiles(run: dict[str, Any], guesses: int) -> str:
    verification = run.get("verification") or {}
    unverified = run.get("batch_status") not in ("complete", "partial")
    published = (
        ("Files kept for debugging", "保留供排查的文件") if unverified
        else ("Files published", "已发布文件")
    )
    tiles = (
        (run.get("files"), published, ""),
        (run.get("replacements"), ("Replacements made", "替换次数"), ""),
        (verification.get("files_scanned"), ("Files rescanned", "复检文件"), ""),
        (guesses, ("Model guesses to review", "待复核的模型推测"),
         " attention" if guesses else ""),
    )
    return "".join(
        f'<div class="tile{extra}"><div class="n">{_number(value)}</div>'
        f'<div class="l">{_t(*label)}</div></div>'
        for value, label, extra in tiles
    )


def _kind_chart(totals: Counter[str]) -> str:
    if not totals:
        return _empty("No replacements were made.", "未进行任何替换。")
    largest = max(totals.values())
    overall = sum(totals.values())
    rows = []
    for kind, count in totals.most_common():
        # The count sits at the end of its bar, so the bar scales within the room
        # left for the label (see .bar-line in the template).
        share = 100 * count / overall
        rows.append(
            f'<span class="k">{_t(*_kind_label(kind))}</span>'
            f'<span class="bar-line" style="--w:{count / largest:.4f}">'
            f'<span class="fill" aria-hidden="true"></span>'
            f'<span class="v"><b>{count:,}</b> <small>{share:.0f}%</small></span></span>'
        )
    en = "; ".join(f"{_kind_label(k)[0]} {v}" for k, v in totals.most_common())
    zh = "；".join(f"{_kind_label(k)[1]} {v}" for k, v in totals.most_common())
    return f'<div class="bars" role="img" {_aria(en, zh)}>' + "".join(rows) + "</div>"


def _output_label(report: dict[str, Any] | None, safe_id: str) -> str:
    """The published file name when the job kept source names, else the ordinal."""
    outputs = (report or {}).get("outputs") or []
    if not outputs:
        return f'<span class="path mono">{_e(safe_id)}</span>'
    extra = (
        f' <span class="more">{_t(f"+{len(outputs) - 1} more", f"另有 {len(outputs) - 1} 个")}'
        "</span>" if len(outputs) > 1 else ""
    )
    return (
        f'<span class="path">{_e(outputs[0])}</span>{extra}'
        f'<span class="id mono">{_e(safe_id)}</span>'
    )


def _status(outcome: str) -> str:
    tone, en, zh = _OUTCOMES.get(outcome, ("warn", outcome, outcome))
    return f'<span class="status {tone}">{_t(en, zh)}</span>'


def _kinds_inline(counts: dict[str, Any]) -> str:
    items = sorted(
        ((str(kind), count) for kind, count in counts.items() if isinstance(count, int)),
        key=lambda item: (-item[1], _kind_sort(item[0])),
    )
    return "".join(
        f'<span class="kc">{_t(*_kind_label(kind))} <b>{count:,}</b></span>'
        for kind, count in items
    )


def _outcome_summary(outcomes: dict[str, str]) -> str:
    tally = Counter(outcomes.values())
    ordered = [key for key in _OUTCOME_ORDER if tally[key]] + sorted(
        key for key in tally if key not in _OUTCOME_ORDER
    )
    return "".join(
        f'<span class="tally">{_status(key)}<b>{tally[key]:,}</b></span>' for key in ordered
    )


def _file_table(outcomes: dict[str, str], by_id: dict[str, dict[str, Any]]) -> str:
    if not outcomes:
        return _empty("No files were recorded for this job.", "此任务没有记录任何文件。")
    rows = []
    for safe_id in sorted(outcomes):
        report = by_id.get(safe_id)
        total = _number(report.get("total")) if report else "–"
        kinds = _kinds_inline(report.get("counts") or {}) if report else ""
        label = _output_label(report, safe_id)
        name = f'<a href="#{_e(safe_id)}">{label}</a>' if report else label
        rows.append(
            f'<tr><td class="file-cell">{name}</td><td>{_status(outcomes[safe_id])}</td>'
            f'<td class="num">{total}</td><td class="kinds">{kinds}</td></tr>'
        )
    head = (
        f'<th scope="col">{_t("File", "文件")}</th>'
        f'<th scope="col">{_t("Outcome", "结果")}</th>'
        f'<th scope="col" class="num">{_t("Replacements", "替换")}</th>'
        f'<th scope="col">{_t("Kinds", "类型")}</th>'
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _confidence(item: dict[str, Any]) -> tuple[str, str]:
    confidence = str(item.get("confidence", ""))
    en, zh = _CONFIDENCE_LABELS.get(confidence, (confidence, confidence))
    score = item.get("score")
    if isinstance(score, int | float) and not isinstance(score, bool):
        en, zh = f"{en} ({score:.2f})", f"{zh}（{score:.2f}）"
    return en, zh


def _detection_rows(report: dict[str, Any]) -> str:
    rows = []
    for index, item in enumerate(report.get("detections") or [], start=1):
        kind = str(item.get("kind", ""))
        confidence = str(item.get("confidence", ""))
        start, end = item.get("start"), item.get("end")
        rows.append(
            f'<tr class="det" data-kind="{_e(kind)}" data-conf="{_e(confidence)}">'
            f'<td class="num idx">{index}</td>'
            f"<td>{_t(*_kind_label(kind))}</td>"
            f'<td><span class="surrogate">{_e(item.get("surrogate", ""))}</span></td>'
            f'<td class="mono loc">{_e(item.get("locator", ""))}</td>'
            f'<td class="num mono">{_e(start)}–{_e(end)}</td>'
            f'<td class="mono det-name">{_e(item.get("detector", ""))}</td>'
            f'<td class="conf conf-{_e(confidence)}">{_t(*_confidence(item))}</td></tr>'
        )
    return "".join(rows)


def _file_details(outcomes: dict[str, str], files: list[dict[str, Any]]) -> str:
    sections = []
    for report in sorted(files, key=lambda item: str(item.get("source", ""))):
        safe_id = str(report.get("source", ""))
        rows = _detection_rows(report)
        total = report.get("total", 0)
        if rows:
            head = "".join(
                f'<th scope="col"{cls}>{_t(en, zh)}</th>'
                for cls, en, zh in (
                    (' class="num"', "#", "#"),
                    ("", "Kind", "类型"),
                    ("", "Replaced with", "替换为"),
                    ("", "Where", "位置"),
                    (' class="num"', "Characters", "字符位置"),
                    ("", "Detector", "检测器"),
                    ("", "Confidence", "置信度"),
                )
            )
            body = (
                f'<div class="scroll"><table class="detections"><thead><tr>{head}</tr></thead>'
                f"<tbody>{rows}</tbody></table></div>"
                + _empty(
                    "No replacements in this file match the filters.",
                    "此文件中没有符合筛选条件的替换。",
                    filtered=True,
                )
            )
        else:
            body = _empty("Nothing was replaced in this file.", "此文件未进行任何替换。")
        count = total if isinstance(total, int) else 0
        sections.append(
            f'<details class="file" id="{_e(safe_id)}"><summary>'
            f'<span class="name">{_output_label(report, safe_id)}</span>'
            f'{_status(outcomes.get(safe_id, "sanitised"))}'
            f'<span class="count">{_t(_plural(count, "replacement"), f"{count:,} 处替换")}'
            f"</span></summary>"
            f'<div class="inner">{body}</div></details>'
        )
    if not sections:
        return _empty("No per-file reports were found.", "未找到单个文件的报告。")
    return "".join(sections)


def _completed(run: dict[str, Any]) -> str:
    raw = run.get("completed_utc")
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return _t("Completion time not recorded", "未记录完成时间")
    readable = moment.strftime("%d %b %Y, %H:%M UTC")
    return (
        f'{_t("Completed", "完成于")} '
        f'<time datetime="{_e(moment.isoformat())}">{_e(readable)}</time>'
    )


def _verification_note(run: dict[str, Any]) -> str:
    verification = run.get("verification") or {}
    dummies = verification.get("recognised_dummies")
    status = verification.get("status")
    if status == "clean_for_configured_recognisers":
        line = _t("Rescan result: clean for the configured recognisers.",
                  "复检结果：已配置的识别器未发现个人信息。")
    elif status == "failed":
        line = _t("Rescan result: failed.", "复检结果：未通过。")
    else:
        line = _t("Rescan result was not recorded.", "未记录复检结果。")
    if isinstance(dummies, int) and dummies:
        line += " " + _t(
            f"It recognised {_plural(dummies, 'dummy value')} it expects to see.",
            f"复检识别出 {dummies:,} 个预期中的替换值。",
        )
    return line


def render(run: dict[str, Any], files: list[dict[str, Any]], *, batch: str = "") -> str:
    outcomes = {str(key): str(value) for key, value in (run.get("outcomes") or {}).items()}
    by_id = {str(report.get("source", "")): report for report in files}
    totals: Counter[str] = Counter()
    for report in files:
        for kind, count in (report.get("counts") or {}).items():
            if isinstance(count, int):
                totals[str(kind)] += count
    guesses = sum(
        1 for report in files for item in report.get("detections") or []
        if item.get("confidence") == "NER"
    )
    if guesses:
        review_note = _t(
            f"{_plural(guesses, 'replacement')} came from the language model rather than "
            "a fixed pattern; filter by “Model guess” to check those first.",
            f"有 {guesses:,} 处替换来自语言模型的推测，而非固定规则；请先按“模型推测”"
            "筛选并核对。",
        )
    else:
        review_note = _t(
            "Every replacement came from a fixed pattern or a value found elsewhere in "
            "the batch.",
            "所有替换均来自固定规则，或来自本批次其他文件中已发现的值。",
        )
    kind_options = (
        '<option value="" data-en="All kinds" data-zh="全部类型">All kinds</option>'
        + "".join(
            f'<option value="{_e(kind)}" data-en="{_e(_kind_label(kind)[0])}" '
            f'data-zh="{_e(_kind_label(kind)[1])}">{_e(_kind_label(kind)[0])}</option>'
            for kind in sorted(totals, key=_kind_sort)
        )
    )
    heading_en = f"{batch} – sanitisation report" if batch else "Sanitisation report"
    heading_zh = f"{batch} 脱敏报告" if batch else "脱敏报告"
    if run.get("output_names") == "source":
        files_note = _t(
            "Files keep their source folders and names, with any personal details in the "
            "names replaced. A name that could not be made safe keeps an opaque "
            "file-NNNN name. The small grey ordinal matches the JSON report.",
            "文件保留原有的文件夹和文件名，名称中的个人信息已被替换。无法安全处理的名称"
            "改用不透明的 file-NNNN 名称。灰色小字编号与 JSON 报告对应。",
        )
    else:
        files_note = _t(
            "Output files use opaque names (file-0001 and so on), in the order the source "
            "folder was read. The same name appears in the files folder.",
            "输出文件使用不透明名称（file-0001 等），按读取源文件夹的顺序编号，"
            "与文件夹中的名称一致。",
        )
    recognisers = run.get("recognisers") or []
    template = Template(
        resources.files("panda").joinpath("report_template.html").read_text(encoding="utf-8")
    )
    return template.substitute(
        title=_e(heading_en),
        title_zh=_e(heading_zh),
        heading=_t(heading_en, heading_zh),
        subtitle=_completed(run),
        banner=_banner(run),
        tiles=_tiles(run, guesses),
        kind_chart=_kind_chart(totals),
        review_note=review_note,
        outcome_summary=_outcome_summary(outcomes),
        file_table=_file_table(outcomes, by_id),
        files_note=files_note,
        kind_options=kind_options,
        file_details=_file_details(outcomes, files),
        footer_note=_t(
            f"Generated by Panda {__version__} from the JSON reports in this folder. "
            "It lists dummy values and locations only; original values are never recorded.",
            f"由 Panda {__version__} 根据本文件夹中的 JSON 报告生成。报告只列出替换值和"
            "位置，从不记录原始内容。",
        ),
        verification_note=_verification_note(run),
        recognisers=(
            "".join(f"<li><code>{_e(name)}</code></li>" for name in recognisers)
            or f"<li>{_t('None recorded', '未记录')}</li>"
        ),
    )
