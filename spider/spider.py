#!/usr/bin/env python3
"""Human-supervised collector for publicly disclosed MOHURD company records.

Opens https://jzsc.mohurd.gov.cn/data/company, applies the public filters
(建筑工程施工总承包一级, 企业注册属地 山东), then opens each result and reads
the detail sections. Verification dialogs are never solved or bypassed; the
script waits until a person finishes them in the visible browser.

Detail tables use rowspan. Cells are mapped by Element UI column class and
rowspan is carried forward, so a qualification name on a later row keeps the
certificate number and dates from the merged cells above it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

TARGET_URL = "https://jzsc.mohurd.gov.cn/data/company"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "mohurd_shandong_level1.json"
DEFAULT_STATE = Path(__file__).resolve().parent / "mohurd_shandong_level1.state.json"
DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "playwright_profile"
SETTLE_SECONDS = 3.0
QUALIFICATION_NAME = "建筑工程施工总承包一级"
REGION_NAME = "山东"

SECTIONS = (
    "企业资质资格",
    "注册人员",
    "工程项目",
    "业绩技术指标",
    "不良行为",
    "黑名单记录",
    "失信联合惩戒记录",
    "变更记录",
)
CREDIT_CODE_RE = re.compile(r"^[0-9A-Z]{15,18}$")

CAPTCHA_JS = """
() => {
  function shown(el) {
    let node = el;
    while (node && node !== document.documentElement) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      node = node.parentElement;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8;
  }
  const dialog = document.querySelector('.captchaDilaog');
  const wrap = dialog && dialog.closest('.el-dialog__wrapper');
  if (wrap && shown(wrap)) return true;
  const geetest = document.querySelector('.geetest_panel, .nc_wrapper');
  return !!(geetest && shown(geetest));
}
"""

EXTRACT_TABLE_JS = """
(paneId) => {
  const pane = document.getElementById(paneId);
  if (!pane) return {headers: [], rows: [], loading: false};
  const mask = pane.querySelector('.el-loading-mask');
  const loading = !!(mask && getComputedStyle(mask).display !== 'none');
  const table = pane.querySelector('.el-table');
  if (!table) return {headers: [], rows: [], loading};
  const headers = {};
  for (const th of table.querySelectorAll('.el-table__header-wrapper thead th')) {
    if (th.classList.contains('gutter')) continue;
    const match = (th.className || '').match(/column_(\\d+)/);
    if (!match) continue;
    headers[match[1]] = (th.innerText || '').replace(/\\s+/g, ' ').trim();
  }
  const headerList = Object.keys(headers).sort((a, b) => Number(a) - Number(b)).map((key) => headers[key]);
  const pending = {};
  const rows = [];
  for (const tr of table.querySelectorAll('.el-table__body-wrapper tbody tr')) {
    if (tr.querySelector('.el-table__empty-block, .el-table__empty-text')) continue;
    const cells = [...tr.querySelectorAll('td')].filter((td) => !td.classList.contains('gutter'));
    if (!cells.length) continue;
    const record = {};
    const seen = new Set();
    for (const td of cells) {
      const match = (td.className || '').match(/column_(\\d+)/);
      if (!match) continue;
      const key = match[1];
      const label = headers[key] || ('column_' + key);
      const text = ((td.querySelector('.cell') || td).innerText || '').replace(/\\s+/g, ' ').trim();
      record[label] = text;
      seen.add(key);
      const span = parseInt(td.getAttribute('rowspan') || '1', 10);
      if (span > 1) pending[key] = {label, text, left: span - 1};
    }
    for (const key of Object.keys(pending)) {
      if (seen.has(key)) continue;
      record[pending[key].label] = pending[key].text;
      pending[key].left -= 1;
      if (pending[key].left <= 0) delete pending[key];
    }
    if (Object.values(record).some((value) => value && value !== '无数据')) rows.push(record);
  }
  return {headers: headerList, rows, loading};
}
"""

BASIC_INFO_JS = """
() => {
  const root = document.querySelector('.detaile-header');
  if (!root) return {};
  const info = {};
  const name = root.querySelector('.name');
  if (name && name.innerText.trim()) info['企业名称'] = name.innerText.trim();
  for (const col of root.querySelectorAll('.el-col')) {
    const label = col.querySelector('.label');
    const value = col.querySelector('.value');
    if (!label) continue;
    info[label.innerText.trim()] = value ? value.innerText.replace(/\\s+/g, ' ').trim() : '';
  }
  return info;
}
"""

LIST_ROWS_JS = """
() => {
  const table = [...document.querySelectorAll('.el-table')].find((item) =>
    (item.innerText || '').includes('统一社会信用代码') && item.querySelector('span.link')
  );
  if (!table) return [];
  const headers = [...table.querySelectorAll('.el-table__header-wrapper thead th')]
    .filter((th) => !th.classList.contains('gutter'))
    .map((th) => (th.innerText || '').replace(/\\s+/g, ' ').trim());
  return [...table.querySelectorAll('.el-table__body-wrapper tbody tr')].map((tr) => {
    const cells = [...tr.querySelectorAll('td')].filter((td) => !td.classList.contains('gutter'));
    const record = {};
    cells.forEach((td, index) => {
      const label = headers[index] || ('column_' + (index + 1));
      record[label] = ((td.querySelector('.cell') || td).innerText || '').replace(/\\s+/g, ' ').trim();
    });
    return record;
  }).filter((row) => row['企业名称']);
}
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"{path} must contain a JSON array")
    return value


def captcha_present(page: Page) -> bool:
    try:
        return bool(page.evaluate(CAPTCHA_JS))
    except Exception:
        return False


def require_human_captcha_resolution(page: Page) -> None:
    if not captcha_present(page):
        return
    print("\n[verification] 验证已过期或安全验证已出现。")
    print("请在可见浏览器中人工完成验证。脚本不会点击验证码，完成后会自动继续。")
    while captcha_present(page):
        time.sleep(2)
    print("[verification] 验证界面已关闭，继续。")
    page.wait_for_timeout(int(SETTLE_SECONDS * 1000))


def settle(page: Page, reason: str) -> None:
    """Wait out the detail/tab loading window before reading text."""
    print(f"[wait] {SETTLE_SECONDS:.0f}s so {reason} can finish rendering.")
    page.wait_for_timeout(int(SETTLE_SECONDS * 1000))
    require_human_captcha_resolution(page)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        require_human_captcha_resolution(page)
        try:
            loading = page.evaluate(
                """() => [...document.querySelectorAll('.el-loading-mask')]
                  .some((mask) => {
                    const pane = mask.closest('.el-tab-pane');
                    const active = !pane || pane.getAttribute('aria-hidden') !== 'true';
                    return active && getComputedStyle(mask).display !== 'none';
                  })"""
            )
        except Exception:
            loading = False
        if not loading:
            return
        time.sleep(0.4)


class RateLimiter:
    def __init__(self, minimum_seconds: float) -> None:
        self.minimum_seconds = minimum_seconds

    def wait(self, reason: str) -> None:
        print(f"[rate-limit] Waiting {self.minimum_seconds:.1f}s before {reason}.")
        time.sleep(self.minimum_seconds)


def active_pane_id(page: Page) -> str:
    pane_id = page.locator(".el-tabs__item.is-active").first.get_attribute("aria-controls")
    if not pane_id:
        raise RuntimeError("Could not find the active detail tab")
    return pane_id


def read_active_table(page: Page) -> dict[str, Any]:
    return page.evaluate(EXTRACT_TABLE_JS, active_pane_id(page))


def read_basic_info(page: Page) -> dict[str, str]:
    info = page.evaluate(BASIC_INFO_JS)
    return info if isinstance(info, dict) else {}


def collect_section(page: Page, section: str, limiter: RateLimiter) -> dict[str, Any]:
    tab = page.get_by_role("tab", name=section, exact=True)
    if tab.count() == 0:
        print(f"[warning] tab not found: {section}")
        return {"headers": [], "rows": []}
    if "is-active" not in (tab.first.get_attribute("class") or ""):
        limiter.wait(f"opening detail tab {section}")
        tab.first.click()
    settle(page, f"the {section} table")
    collected: list[dict[str, str]] = []
    headers: list[str] = []
    seen: set[str] = set()
    for _ in range(100):
        require_human_captcha_resolution(page)
        payload = read_active_table(page)
        headers = payload.get("headers") or headers
        rows = payload.get("rows") or []
        fingerprint = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        if fingerprint in seen:
            break
        seen.add(fingerprint)
        if section == "业绩技术指标":
            rows = attach_performance_details(page, rows, limiter)
        collected.extend(rows)
        pane_id = active_pane_id(page)
        has_next = page.evaluate(
            """(paneId) => {
              const button = document.querySelector('#' + paneId + ' .btn-next');
              return !!button && !button.disabled && !button.classList.contains('disabled');
            }""",
            pane_id,
        )
        if not has_next:
            break
        limiter.wait(f"opening the next {section} page")
        page.locator(f"#{pane_id} .btn-next").click()
        settle(page, f"the next {section} page")
    return {"headers": headers, "rows": collected}


def attach_performance_details(page: Page, rows: list[dict[str, str]], limiter: RateLimiter) -> list[dict[str, str]]:
    """Open 业绩技术指标 row dialogs when the table only summarizes the record."""
    pane_id = active_pane_id(page)
    buttons = page.locator(f"#{pane_id} .el-table__body-wrapper tbody tr")
    detailed: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        item = dict(row)
        button = buttons.nth(index).locator("button, a, span").filter(
            has_text=re.compile(r"查看|详情")
        ).first
        try:
            if index >= buttons.count() or not button.is_visible(timeout=300):
                detailed.append(item)
                continue
        except Exception:
            detailed.append(item)
            continue
        limiter.wait("opening a performance-detail dialog")
        button.click()
        settle(page, "the performance-detail dialog")
        dialog = page.evaluate(
            """() => {
              const wraps = [...document.querySelectorAll('.el-dialog__wrapper')].filter((wrap) => {
                const style = getComputedStyle(wrap);
                return style.display !== 'none' && !wrap.querySelector('.captchaDilaog');
              });
              const wrap = wraps.at(-1);
              if (!wrap) return null;
              const tables = [...wrap.querySelectorAll('.el-table')].map((table) => {
                const headers = [...table.querySelectorAll('.el-table__header-wrapper th')]
                  .map((th) => (th.innerText || '').replace(/\\s+/g, ' ').trim())
                  .filter(Boolean);
                const body = [...table.querySelectorAll('.el-table__body-wrapper tbody tr')].map((tr) =>
                  [...tr.querySelectorAll('td .cell')].map((cell) => cell.innerText.replace(/\\s+/g, ' ').trim())
                );
                return {headers, body};
              });
              const text = (wrap.innerText || '').replace(/[ \\t]+/g, ' ').trim();
              return {title: wrap.querySelector('.el-dialog__title')?.innerText.trim() || '', tables, text};
            }"""
        )
        if dialog:
            item["详情"] = dialog
        close = page.locator(".el-dialog__wrapper:visible .el-dialog__headerbtn").first
        try:
            if close.is_visible(timeout=500):
                close.click()
            else:
                page.keyboard.press("Escape")
        except Exception:
            page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        detailed.append(item)
    return detailed


def list_rows(page: Page) -> list[dict[str, str]]:
    rows = page.evaluate(LIST_ROWS_JS)
    return rows if isinstance(rows, list) else []


def results_are_shandong(page: Page) -> bool:
    rows = list_rows(page)
    if not rows:
        return False
    return all(REGION_NAME in str(row.get("企业注册属地", "")) for row in rows)


def apply_public_filters(page: Page) -> None:
    """Use the public filter controls. Any verification still stops for a person."""
    require_human_captcha_resolution(page)
    print(f"[filter] 资质类别: 建筑业企业；资质名称: {QUALIFICATION_NAME}；企业注册属地: {REGION_NAME}")
    page.locator(".labelInPutRadio span", has_text="建筑业企业").click()
    page.wait_for_timeout(500)
    page.locator(".quality-button .popAlert").click()
    dialog = page.locator(".quality-button-dialog")
    dialog.wait_for(state="visible", timeout=10_000)
    radio = dialog.locator(".el-radio", has_text=QUALIFICATION_NAME).first
    for _ in range(12):
        if radio.count() and radio.is_visible():
            break
        nxt = dialog.locator(".btn-next")
        if nxt.count() == 0 or nxt.is_disabled():
            break
        nxt.click()
        page.wait_for_timeout(500)
    radio.click()
    dialog.get_by_role("button", name="确 定").click()
    page.wait_for_timeout(500)
    page.locator(".region-select input[placeholder='请选择省级']").click()
    page.locator(".el-select-dropdown:visible .el-select-dropdown__item").filter(
        has_text=re.compile(rf"^{REGION_NAME}$")
    ).first.click()
    page.locator("span.ssButton", has_text="查询").click()
    settle(page, "the filtered company list")


def company_key(record: dict[str, Any]) -> str:
    info = record.get("企业基础信息") if isinstance(record.get("企业基础信息"), dict) else {}
    credit = str(info.get("统一社会信用代码") or record.get("统一社会信用代码") or "")
    if CREDIT_CODE_RE.match(credit):
        return credit
    return re.sub(r"\s+", "", str(record.get("企业名称") or record.get("company_name") or ""))


def record_is_complete(record: dict[str, Any]) -> bool:
    info = record.get("企业基础信息")
    if not isinstance(info, dict) or not CREDIT_CODE_RE.match(str(info.get("统一社会信用代码", ""))):
        return False
    return all(isinstance(record.get(section), dict) and "rows" in record[section] for section in SECTIONS)


def scrape_detail(page: Page, seed: dict[str, Any], limiter: RateLimiter) -> dict[str, Any]:
    settle(page, "the company detail page")
    deadline = time.monotonic() + 30
    info: dict[str, str] = {}
    while time.monotonic() < deadline:
        require_human_captcha_resolution(page)
        info = read_basic_info(page)
        if CREDIT_CODE_RE.match(str(info.get("统一社会信用代码", ""))):
            break
        page.wait_for_timeout(500)
    else:
        raise TimeoutError("企业基础信息 did not finish loading")
    for field in ("企业名称", "统一社会信用代码", "企业法定代表人", "企业注册属地"):
        if not info.get(field) and seed.get(field):
            info[field] = str(seed[field])
    record: dict[str, Any] = {
        "company_name": info.get("企业名称") or seed.get("企业名称") or "",
        "detail_url": page.url,
        "企业基础信息": info,
        "collected_at_utc": utc_now(),
    }
    for section in SECTIONS:
        record[section] = collect_section(page, section, limiter)
        print(f"  [section] {section}: {len(record[section]['rows'])} rows")
    record["collected_at_utc"] = utc_now()
    return record


def open_detail(list_page: Page, row_index: int) -> tuple[Page, bool]:
    link = list_page.locator(".el-table__body-wrapper span.link").nth(row_index)
    try:
        with list_page.context.expect_page(timeout=12_000) as popup_info:
            link.click()
        detail = popup_info.value
        detail.wait_for_load_state("commit", timeout=30_000)
        return detail, True
    except PlaywrightTimeoutError:
        list_page.wait_for_timeout(800)
        return list_page, False


def next_list_page(page: Page, limiter: RateLimiter) -> bool:
    button = page.locator(".el-pagination .btn-next:visible").first
    try:
        if button.count() == 0 or button.is_disabled():
            return False
    except Exception:
        return False
    limiter.wait("opening the next results page")
    button.click()
    settle(page, "the next results page")
    if not results_are_shandong(page):
        raise RuntimeError("结果页出现了非山东企业，已停止，避免采到未筛选列表。")
    return True


def open_browser(playwright: Any, profile: Path):
    profile.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=False,
        viewport={"width": 1440, "height": 960},
        locale="zh-CN",
    )


def wait_for_filtered_results(page: Page, ready_flag: Path) -> None:
    try:
        apply_public_filters(page)
    except Exception as exc:
        print(f"[filter] 自动设置筛选未完成: {exc}")
        print("请在可见浏览器中手动设置后点击查询：")
        print(f"  资质名称: {QUALIFICATION_NAME}")
        print(f"  企业注册属地: {REGION_NAME}")
    print("[filter] 等待结果列表中的企业注册属地全部包含“山东”。验证码出现时请人工处理。")
    while not results_are_shandong(page):
        require_human_captcha_resolution(page)
        if ready_flag.exists():
            ready_flag.unlink()
            settle(page, "the results list")
        time.sleep(2)
    rows = list_rows(page)
    print(f"[ready] {len(rows)} rows on this page, all registered in {REGION_NAME}.")


def upsert(records: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    key = company_key(record)
    for index, existing in enumerate(records):
        if company_key(existing) == key:
            records[index] = record
            return records
    records.append(record)
    return records


def collect(args: argparse.Namespace, limiter: RateLimiter) -> int:
    records = read_json_list(args.output)
    completed = {company_key(item) for item in records if record_is_complete(item)}
    with sync_playwright() as playwright:
        context = open_browser(playwright, args.profile)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            limiter.wait("opening the public company search page")
            page.goto(TARGET_URL, wait_until="commit", timeout=60_000)
            settle(page, "the company search page")
            wait_for_filtered_results(page, args.ready_flag)
            for page_number in range(1, args.max_pages + 1):
                rows = list_rows(page)
                print(f"\n[page {page_number}/{args.max_pages}] {len(rows)} companies")
                for index, seed in enumerate(rows):
                    key = company_key({"企业基础信息": seed, "企业名称": seed.get("企业名称", "")})
                    name = seed.get("企业名称", "")
                    if key in completed:
                        print(f"[skip] {name}")
                        continue
                    print(f"[collect] {name}")
                    limiter.wait(f"opening company detail: {name}")
                    listing_url = page.url
                    detail, popup = open_detail(page, index)
                    try:
                        require_human_captcha_resolution(detail)
                        record = scrape_detail(detail, seed, limiter)
                    except Exception as exc:
                        print(f"[warning] {name}: {exc}")
                        continue
                    finally:
                        if popup:
                            detail.close()
                        elif page.url != listing_url:
                            limiter.wait("returning to the results list")
                            page.go_back(wait_until="commit", timeout=30_000)
                            settle(page, "the results list")
                    records = upsert(records, record)
                    if record_is_complete(record):
                        completed.add(company_key(record))
                    atomic_json_write(args.output, records)
                    atomic_json_write(args.state, {
                        "updated_at_utc": utc_now(),
                        "records": len(records),
                        "complete": len(completed),
                        "last_company": record.get("company_name"),
                    })
                    info = record["企业基础信息"]
                    print(
                        f"[saved] {info.get('企业名称')} | {info.get('统一社会信用代码')} | "
                        f"资质 {len(record['企业资质资格']['rows'])}"
                    )
                if page_number == args.max_pages or not next_list_page(page, limiter):
                    break
        except KeyboardInterrupt:
            print("\n已停止。已完成的企业都已写入文件。")
        except Exception as exc:
            if "has been closed" in str(exc) or "Target closed" in str(exc):
                print("\n浏览器已关闭。已完成的企业都已写入文件，重新运行会从断点继续。")
            else:
                raise
        finally:
            try:
                context.close()
            except Exception:
                pass
    print(f"Saved {len(records)} records to {args.output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--ready-flag", type=Path, default=Path("start_collect.flag"))
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--min-delay", type=float, default=3.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_delay < 3:
        raise SystemExit("--min-delay must be at least 3 seconds")
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be positive")
    return collect(args, RateLimiter(args.min_delay))


if __name__ == "__main__":
    sys.exit(main())
