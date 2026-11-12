"""Compare the requirement tables of two notice detail pages.

A procurement agent pastes the old and the new detail addresses of the same
site.  Both pages are fetched live, their requirement tables are read, the
package numbers hidden in merged cells are restored, and rows are aligned by
(package no, item no).  Row order never counts as a change; an item that
moved to another package is reported as a removal from the old package plus
an addition to the new one.

When a single page repeats the same key with conflicting content, only that
item is flagged for manual review (待核): the remaining items are still
compared, and no addition/removal is inferred from the conflicting rows.
If either page cannot be read, the round yields no confirmed differences at
all -- only the failed addresses.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .crawler import (
    FETCH_TIMEOUT,
    USER_AGENT,
    FetchIssue,
    _fetch,
    canonical_section,
    normalize_space,
    normalize_url,
    normalize_width,
    page_title,
)

MAX_HEADER_SCAN_ROWS = 8
MAX_SPAN = 500

# ---------------------------------------------------------------------------
# Header recognition
# ---------------------------------------------------------------------------

_PACKAGE_HEADER_RE = re.compile(r"^(?:合同包|分包|包件|标段|包)(?:编号|号|序号)?$")
_ITEM_HEADER_RE = re.compile(r"^(?:品目|采购品目|条目|项)(?:序号|编号|号|代码)?$|^(?:序号|编号)$")
_NAME_HEADER_WORDS = {"品目", "标的", "采购标的", "货物", "设备"}
_SKIP_ITEM_RE = re.compile(r"^(?:合计|小计|总计|备注|说明|注)[：:]?$")


@dataclass(frozen=True)
class ColumnMap:
    package: int | None
    item: int
    name: int
    quantity: int
    unit: int


def _map_header(cells: Sequence[str]) -> ColumnMap | None:
    """Map grid columns to requirement fields; None if this is not a header."""

    package = item = name = quantity = unit = None
    for idx, raw in enumerate(cells):
        header = re.sub(r"\s+", "", normalize_width(raw))
        if not header:
            continue
        if package is None and _PACKAGE_HEADER_RE.match(header):
            package = idx
        elif item is None and _ITEM_HEADER_RE.match(header):
            item = idx
        elif name is None and ("名称" in header or header in _NAME_HEADER_WORDS):
            name = idx
        elif quantity is None and "数量" in header:
            quantity = idx
        elif unit is None and "单位" in header and "数量" not in header and "名称" not in header:
            unit = idx
    if item is None or name is None or quantity is None or unit is None:
        return None
    return ColumnMap(package=package, item=item, name=name, quantity=quantity, unit=unit)


# ---------------------------------------------------------------------------
# Grid expansion (restores rowspan/colspan merged cells)
# ---------------------------------------------------------------------------


def _span_value(cell, attr: str) -> int:
    try:
        value = int(str(cell.get(attr) or "1").strip())
    except ValueError:
        return 1
    return max(1, min(value, MAX_SPAN))


def expand_table(table) -> list[list[str]]:
    """Expand a <table> into a grid, filling merged cells with their text."""

    grid: list[list[str]] = []
    carry: dict[int, list[Any]] = {}  # column -> [rows_left, text]
    for tr in table.find_all("tr"):
        row: list[str] = []
        cells = tr.find_all(["td", "th"], recursive=False)
        col = 0
        index = 0
        while col in carry or index < len(cells):
            if col in carry:
                entry = carry[col]
                row.append(entry[1])
                entry[0] -= 1
                if entry[0] <= 0:
                    del carry[col]
                col += 1
                continue
            cell = cells[index]
            index += 1
            text = normalize_space(cell.get_text(" ", strip=True))
            colspan = _span_value(cell, "colspan")
            rowspan = _span_value(cell, "rowspan")
            for _ in range(colspan):
                row.append(text)
                if rowspan > 1:
                    carry[col] = [rowspan - 1, text]
                col += 1
        grid.append(row)
    return grid


# ---------------------------------------------------------------------------
# Row model and normalisation
# ---------------------------------------------------------------------------


def _canon_item(value: str) -> str:
    text = normalize_width(value).upper()
    text = re.sub(r"\s+", "", text)
    text = text.strip("，。；;、：:()（）")
    text = re.sub(r"^第", "", text)
    text = re.sub(r"(?:号|项)$", "", text)
    if text.isdigit():
        return str(int(text))
    return text


def _canon_text(value: str) -> str:
    return re.sub(r"\s+", "", normalize_width(value)).upper()


def _canon_qty(value: str) -> str:
    text = normalize_width(value)
    text = re.sub(r"[\s,，、]", "", text)
    match = re.fullmatch(r"(\d+)\.0+", text)
    if match:
        text = match.group(1)
    return text


@dataclass
class RequirementRow:
    package_no: str
    item_no: str
    name: str
    quantity: str
    unit: str
    raw_text: str
    row_no: int
    source_url: str

    @property
    def key(self) -> tuple[str, str]:
        return (canonical_section(self.package_no), _canon_item(self.item_no))

    @property
    def name_key(self) -> str:
        return _canon_text(self.name)

    @property
    def qty_key(self) -> str:
        return _canon_qty(self.quantity)

    @property
    def unit_key(self) -> str:
        return _canon_text(self.unit)

    def content_key(self) -> tuple[str, str, str]:
        return (self.name_key, self.qty_key, self.unit_key)

    def as_dict(self, pub=None) -> dict[str, Any]:
        url = self.source_url
        return {
            "package_no": self.package_no,
            "item_no": self.item_no,
            "name": self.name,
            "quantity": self.quantity,
            "unit": self.unit,
            "raw_text": self.raw_text,
            "row_no": self.row_no,
            "source_url": pub(url) if pub else url,
        }


@dataclass
class ParsedPage:
    url: str
    title: str
    rows: list[RequirementRow]
    table_count: int


@dataclass
class ModifiedEntry:
    old: RequirementRow
    new: RequirementRow
    changes: list[dict[str, str]]

    def as_dict(self, pub=None) -> dict[str, Any]:
        return {
            "package_no": self.new.package_no,
            "item_no": self.new.item_no,
            "changes": self.changes,
            "old": self.old.as_dict(pub),
            "new": self.new.as_dict(pub),
        }


@dataclass
class PendingEntry:
    package_no: str
    item_no: str
    old_rows: list[RequirementRow]
    new_rows: list[RequirementRow]
    reasons: list[str]

    def as_dict(self, pub=None) -> dict[str, Any]:
        return {
            "package_no": self.package_no,
            "item_no": self.item_no,
            "reasons": self.reasons,
            "old_rows": [row.as_dict(pub) for row in self.old_rows],
            "new_rows": [row.as_dict(pub) for row in self.new_rows],
        }


# ---------------------------------------------------------------------------
# Table parsing
# ---------------------------------------------------------------------------


def _cell(cells: Sequence[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index]


def parse_requirements(html: str, url: str) -> ParsedPage:
    """Extract requirement rows from every qualifying table on the page."""

    soup = BeautifulSoup(html, "html.parser")
    title = page_title(soup)
    rows: list[RequirementRow] = []
    table_count = 0

    for table in soup.find_all("table"):
        grid = expand_table(table)
        if not grid:
            continue
        header_index = -1
        column_map: ColumnMap | None = None
        for index, cells in enumerate(grid[:MAX_HEADER_SCAN_ROWS]):
            column_map = _map_header(cells)
            if column_map is not None:
                header_index = index
                break
        if column_map is None:
            continue
        table_count += 1
        header_cells = tuple(re.sub(r"\s+", "", normalize_width(c)) for c in grid[header_index])

        last_package = ""
        for cells in grid[header_index + 1 :]:
            non_empty = [cell for cell in cells if cell]
            if not non_empty:
                continue
            # A row produced by a full-width colspan note carries the same
            # text in every column; it is not a data row.
            if len(cells) >= 2 and len(set(non_empty)) == 1:
                continue
            normalized = tuple(re.sub(r"\s+", "", normalize_width(c)) for c in cells)
            if normalized == header_cells:
                continue  # repeated header inside the body
            item_no = _cell(cells, column_map.item)
            if not item_no:
                continue
            if _SKIP_ITEM_RE.match(re.sub(r"\s+", "", normalize_width(item_no))):
                continue
            name = _cell(cells, column_map.name)
            quantity = _cell(cells, column_map.quantity)
            unit = _cell(cells, column_map.unit)
            if not name and not quantity and not unit:
                continue
            if column_map.package is None:
                package_no = ""
            else:
                package_no = _cell(cells, column_map.package)
                if package_no:
                    last_package = package_no
                else:
                    # Hand-made tables often leave the package cell blank
                    # instead of using rowspan; inherit the previous value.
                    package_no = last_package
            trimmed = list(cells)
            while trimmed and not trimmed[-1]:
                trimmed.pop()
            rows.append(
                RequirementRow(
                    package_no=package_no,
                    item_no=item_no,
                    name=name,
                    quantity=quantity,
                    unit=unit,
                    raw_text=" ｜ ".join(trimmed),
                    row_no=len(rows) + 1,
                    source_url=url,
                )
            )
    return ParsedPage(url=url, title=title, rows=rows, table_count=table_count)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _group_rows(rows: Sequence[RequirementRow]) -> tuple[dict[tuple[str, str], list[RequirementRow]], list[tuple[str, str]]]:
    groups: dict[tuple[str, str], list[RequirementRow]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = row.key
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    return groups, order


def _key_label(key: tuple[str, str]) -> str:
    package, item = key
    package_text = f"包 {package}" if package else "（未分包）"
    return f"{package_text} · 品目 {item}"


def compare_pages(old_page: ParsedPage, new_page: ParsedPage) -> dict[str, Any]:
    """Align rows of two parsed pages by (package no, item no)."""

    warnings: list[str] = []
    old_groups, old_order = _group_rows(old_page.rows)
    new_groups, new_order = _group_rows(new_page.rows)

    def split_clean(
        groups: dict[tuple[str, str], list[RequirementRow]], side_label: str
    ) -> tuple[dict[tuple[str, str], RequirementRow], dict[tuple[str, str], list[RequirementRow]]]:
        clean: dict[tuple[str, str], RequirementRow] = {}
        conflicts: dict[tuple[str, str], list[RequirementRow]] = {}
        for key, group_rows in groups.items():
            if len(group_rows) == 1:
                clean[key] = group_rows[0]
            elif len({row.content_key() for row in group_rows}) == 1:
                clean[key] = group_rows[0]
                warnings.append(
                    f"{side_label}中{_key_label(key)}有 {len(group_rows)} 行重复，但内容一致，已按一行处理。"
                )
            else:
                conflicts[key] = group_rows
        return clean, conflicts

    old_clean, old_conflicts = split_clean(old_groups, "旧版")
    new_clean, new_conflicts = split_clean(new_groups, "新版")
    conflict_keys = set(old_conflicts) | set(new_conflicts)

    added: list[RequirementRow] = []
    removed: list[RequirementRow] = []
    modified: list[ModifiedEntry] = []
    pending: list[PendingEntry] = []
    unchanged = 0

    ordered_keys = old_order + [key for key in new_order if key not in old_groups]
    for key in ordered_keys:
        if key in conflict_keys:
            # Conflicting rows never imply an addition or a removal; the
            # item is simply flagged for manual review.
            old_rows = old_groups.get(key, [])
            new_rows = new_groups.get(key, [])
            reasons: list[str] = []
            if key in old_conflicts:
                reasons.append(
                    f"旧版中该品目出现 {len(old_conflicts[key])} 行且内容互相冲突"
                )
            if key in new_conflicts:
                reasons.append(
                    f"新版中该品目出现 {len(new_conflicts[key])} 行且内容互相冲突"
                )
            sample = (new_rows or old_rows)[0]
            pending.append(
                PendingEntry(
                    package_no=sample.package_no,
                    item_no=sample.item_no,
                    old_rows=list(old_rows),
                    new_rows=list(new_rows),
                    reasons=reasons,
                )
            )
            continue

        old_row = old_clean.get(key)
        new_row = new_clean.get(key)
        if old_row is not None and new_row is not None:
            if old_row.content_key() == new_row.content_key():
                unchanged += 1
                continue
            changes: list[dict[str, str]] = []
            if old_row.name_key != new_row.name_key:
                changes.append({"field": "品目名称", "old": old_row.name, "new": new_row.name})
            if old_row.qty_key != new_row.qty_key:
                changes.append({"field": "数量", "old": old_row.quantity, "new": new_row.quantity})
            if old_row.unit_key != new_row.unit_key:
                changes.append({"field": "单位", "old": old_row.unit, "new": new_row.unit})
            modified.append(ModifiedEntry(old=old_row, new=new_row, changes=changes))
        elif old_row is not None:
            removed.append(old_row)
        elif new_row is not None:
            added.append(new_row)

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "pending": pending,
        "unchanged": unchanged,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Outcome and live fetch
# ---------------------------------------------------------------------------


@dataclass
class DiffOutcome:
    old_url: str
    new_url: str
    determinable: bool
    reason: str
    failures: list[FetchIssue] = field(default_factory=list)
    old_page: ParsedPage | None = None
    new_page: ParsedPage | None = None
    added: list[RequirementRow] = field(default_factory=list)
    removed: list[RequirementRow] = field(default_factory=list)
    modified: list[ModifiedEntry] = field(default_factory=list)
    pending: list[PendingEntry] = field(default_factory=list)
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self, public_url_fn=None) -> dict[str, Any]:
        def pub(url: str | None) -> str | None:
            if not url:
                return None
            return public_url_fn(url) if public_url_fn else url

        def page_dict(page: ParsedPage | None) -> dict[str, Any] | None:
            if page is None:
                return None
            return {
                "url": pub(page.url),
                "title": page.title,
                "row_count": len(page.rows),
                "table_count": page.table_count,
            }

        return {
            "old_url": pub(self.old_url),
            "new_url": pub(self.new_url),
            "determinable": self.determinable,
            "reason": self.reason,
            "failures": [
                issue.as_dict() | {"url": pub(issue.url) or issue.url}
                for issue in self.failures
            ],
            "old_page": page_dict(self.old_page),
            "new_page": page_dict(self.new_page),
            "summary": {
                "added": len(self.added),
                "removed": len(self.removed),
                "modified": len(self.modified),
                "pending": len(self.pending),
                "unchanged": self.unchanged,
            },
            "added": [row.as_dict(pub) for row in self.added],
            "removed": [row.as_dict(pub) for row in self.removed],
            "modified": [entry.as_dict(pub) for entry in self.modified],
            "pending": [entry.as_dict(pub) for entry in self.pending],
            "warnings": self.warnings,
        }


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


async def diff_lists(old_url: str, new_url: str, *, client_factory=None) -> DiffOutcome:
    """Fetch both detail pages and compare their requirement tables."""

    issues: list[FetchIssue] = []
    try:
        old_norm = normalize_url(old_url)
    except ValueError as exc:
        old_norm = ""
        issues.append(FetchIssue(url=old_url or "(未填写)", reason=f"旧版地址无效：{exc}", stage="validation"))
    try:
        new_norm = normalize_url(new_url)
    except ValueError as exc:
        new_norm = ""
        issues.append(FetchIssue(url=new_url or "(未填写)", reason=f"新版地址无效：{exc}", stage="validation"))

    if old_norm and new_norm and _origin(old_norm) != _origin(new_norm):
        issues.append(
            FetchIssue(
                url=new_norm,
                reason="新版地址与旧版地址不在同一站点，本轮不予比较",
                stage="validation",
            )
        )
    if issues:
        return DiffOutcome(
            old_url=old_norm or old_url,
            new_url=new_norm or new_url,
            determinable=False,
            reason=issues[0].reason,
            failures=issues,
        )

    timeout = httpx.Timeout(FETCH_TIMEOUT, connect=min(8.0, FETCH_TIMEOUT))
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    if client_factory is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers)
        owns_client = True
    else:
        client = client_factory()
        owns_client = False

    try:
        (old_html, old_error, old_final), (new_html, new_error, new_final) = await asyncio.gather(
            _fetch(client, old_norm, "old"),
            _fetch(client, new_norm, "new"),
        )
    finally:
        if owns_client:
            await client.aclose()

    if old_html is None:
        issues.append(FetchIssue(url=old_norm, reason=f"旧版页面读取失败：{old_error}", stage="fetch"))
    if new_html is None:
        issues.append(FetchIssue(url=new_norm, reason=f"新版页面读取失败：{new_error}", stage="fetch"))
    if issues:
        return DiffOutcome(
            old_url=old_norm,
            new_url=new_norm,
            determinable=False,
            reason=issues[0].reason,
            failures=issues,
        )

    old_page: ParsedPage | None = None
    new_page: ParsedPage | None = None
    try:
        old_page = parse_requirements(old_html, old_final or old_norm)
    except Exception as exc:
        issues.append(FetchIssue(url=old_norm, reason=f"旧版页面解析失败：{exc.__class__.__name__}", stage="parse"))
        old_page = None
    try:
        new_page = parse_requirements(new_html, new_final or new_norm)
    except Exception as exc:
        issues.append(FetchIssue(url=new_norm, reason=f"新版页面解析失败：{exc.__class__.__name__}", stage="parse"))
        new_page = None

    if old_page is not None and old_page.table_count == 0:
        issues.append(
            FetchIssue(url=old_page.url, reason="旧版页面中未找到包含品目号、名称、数量、单位列的需求表", stage="parse")
        )
    if new_page is not None and new_page.table_count == 0:
        issues.append(
            FetchIssue(url=new_page.url, reason="新版页面中未找到包含品目号、名称、数量、单位列的需求表", stage="parse")
        )
    if issues:
        return DiffOutcome(
            old_url=old_norm,
            new_url=new_norm,
            determinable=False,
            reason=issues[0].reason,
            failures=issues,
            old_page=old_page if old_page and old_page.table_count else None,
            new_page=new_page if new_page and new_page.table_count else None,
        )

    assert old_page is not None and new_page is not None
    warnings: list[str] = []
    if old_page.url == new_page.url:
        warnings.append("两个地址指向同一页面，差异应为空；请核对是否选错版本。")
    if not old_page.rows:
        warnings.append("旧版需求表没有读取到任何数据行。")
    if not new_page.rows:
        warnings.append("新版需求表没有读取到任何数据行。")

    result = compare_pages(old_page, new_page)
    warnings.extend(result["warnings"])

    return DiffOutcome(
        old_url=old_norm,
        new_url=new_norm,
        determinable=True,
        reason="两版需求表均已读取，按包号与品目号完成对齐。",
        old_page=old_page,
        new_page=new_page,
        added=result["added"],
        removed=result["removed"],
        modified=result["modified"],
        pending=result["pending"],
        unchanged=result["unchanged"],
        warnings=warnings,
    )
