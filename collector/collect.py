#!/usr/bin/env python3
"""Human-supervised collector for publicly disclosed MOHURD company records.

Opens https://jzsc.mohurd.gov.cn/data/company, applies the public filters
(default: 建筑工程施工总承包一级, 企业注册属地 山东; see --qualification /
--city / --county / --traverse for the full Shandong traversal in
docs/COLLECTOR.md), then opens each result and reads the detail sections.
Verification dialogs are never solved or bypassed; the script waits until
a person finishes them in the visible browser.

Detail tables use rowspan. Cells are mapped by Element UI column class and
rowspan is carried forward, so a qualification name on a later row keeps the
certificate number and dates from the merged cells above it.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # --plan-only and unit tests must work without it
    Locator = Page = object  # type: ignore
    class PlaywrightTimeoutError(Exception):  # type: ignore
        pass
    sync_playwright = None  # type: ignore

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

TARGET_URL = "https://jzsc.mohurd.gov.cn/data/company"
DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_OUTPUT = DATA_DIR / "mohurd_shandong_level1.json"
DEFAULT_STATE = DATA_DIR / "mohurd_shandong_level1.state.json"
DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "playwright_profile"
BARK_URL_FILE = Path(__file__).resolve().parents[1] / "bark.url"
WAIT_MIN_SECONDS = 0.5
WAIT_MAX_SECONDS = 0.8
QUALIFICATION_NAME = "建筑工程施工总承包一级"
REGION_NAME = "山东"

# Traversal support (see filters.py / docs/COLLECTOR.md). The current combo
# is set by collect() before a run; scrape_detail stamps it on each record.
try:
    from filters import QueryCombo
except ImportError:  # when imported as a package/module from repo root
    from collector.filters import QueryCombo  # type: ignore
CURRENT_COMBO: QueryCombo | None = None

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

PERFORMANCE_DIALOG_TITLE = "业绩技术指标详情"

# Footer「关 闭」is more stable than the header X when the table repaints or paginates.

OPEN_PERFORMANCE_ROW_JS = """
({paneId, recordId}) => {
  const pane = document.getElementById(paneId);
  if (!pane) return false;
  const trs = [...pane.querySelectorAll('.el-table__body-wrapper tbody tr')];
  const openIn = (tr) => {
    const hit = [...tr.querySelectorAll('button, a, span')].find((el) =>
      /查看|详情/.test((el.innerText || '').trim())
    );
    if (hit) {
      hit.click();
      return true;
    }
    return false;
  };
  if (recordId) {
    for (const tr of trs) {
      if ((tr.innerText || '').includes(recordId) && openIn(tr)) return true;
    }
  }
  for (const tr of trs) {
    if (openIn(tr)) return true;
  }
  return false;
}
"""

PERFORMANCE_DIALOG_STATE_JS = """
(recordId) => {
  function shown(wrap) {
    if (!wrap) return false;
    const style = getComputedStyle(wrap);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = wrap.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8;
  }
  let wrap = null;
  for (const candidate of document.querySelectorAll('.el-dialog__wrapper')) {
    if (candidate.querySelector('.captchaDilaog')) continue;
    if (!shown(candidate)) continue;
    const title = (candidate.querySelector('.el-dialog__title')?.innerText || '')
      .replace(/\\s+/g, '');
    if (title.includes('业绩技术指标详情')) wrap = candidate;
  }
  if (!wrap) return {visible: false, loading: false, text: '', recordMatch: !recordId};
  const loading = [...wrap.querySelectorAll('.el-loading-mask')].some((mask) =>
    getComputedStyle(mask).display !== 'none'
  );
  const text = (wrap.innerText || '').replace(/[ \\t]+/g, ' ').trim();
  const recordMatch = !recordId || text.includes(recordId);
  return {visible: true, loading, text, recordMatch};
}
"""

EXTRACT_PERFORMANCE_DIALOG_JS = """
() => {
  function shown(wrap) {
    if (!wrap) return false;
    const style = getComputedStyle(wrap);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = wrap.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8;
  }
  let wrap = null;
  for (const candidate of document.querySelectorAll('.el-dialog__wrapper')) {
    if (candidate.querySelector('.captchaDilaog')) continue;
    if (!shown(candidate)) continue;
    const title = (candidate.querySelector('.el-dialog__title')?.innerText || '')
      .replace(/\\s+/g, '');
    if (title.includes('业绩技术指标详情')) wrap = candidate;
  }
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
}
"""

CLOSE_PERFORMANCE_DIALOG_JS = """
() => {
  function shown(wrap) {
    if (!wrap) return false;
    const style = getComputedStyle(wrap);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = wrap.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8;
  }
  let wrap = null;
  for (const candidate of document.querySelectorAll('.el-dialog__wrapper')) {
    if (candidate.querySelector('.captchaDilaog')) continue;
    if (!shown(candidate)) continue;
    const title = (candidate.querySelector('.el-dialog__title')?.innerText || '')
      .replace(/\\s+/g, '');
    if (title.includes('业绩技术指标详情')) wrap = candidate;
  }
  if (!wrap) return {present: false, method: 'absent'};
  const pick = [...wrap.querySelectorAll(
    '.el-dialog__footer button, .dialog-footer button, .el-dialog__body button, button'
  )].find((btn) => /关\\s*闭/.test((btn.innerText || '').trim()));
  if (pick) {
    pick.click();
    return {present: true, method: 'footer'};
  }
  const header = wrap.querySelector('.el-dialog__headerbtn');
  if (header) {
    header.click();
    return {present: true, method: 'header'};
  }
  return {present: true, method: 'none'};
}
"""

PERFORMANCE_DIALOG_OPEN_JS = """
() => {
  for (const wrap of document.querySelectorAll('.el-dialog__wrapper')) {
    const style = getComputedStyle(wrap);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    if (wrap.querySelector('.captchaDilaog')) continue;
    const title = (wrap.querySelector('.el-dialog__title')?.innerText || '')
      .replace(/\\s+/g, '');
    if (title.includes('业绩技术指标详情')) return true;
  }
  return false;
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


def pause(page: Page | None = None) -> float:
    """Short pause in the 0.5–0.8s range. Does not interact with verification."""
    seconds = random.uniform(WAIT_MIN_SECONDS, WAIT_MAX_SECONDS)
    if page is None:
        time.sleep(seconds)
    else:
        page.wait_for_timeout(int(seconds * 1000))
    return seconds


def bark_endpoint() -> str | None:
    configured = os.environ.get("BARK_URL", "").strip()
    if not configured and BARK_URL_FILE.exists():
        configured = BARK_URL_FILE.read_text(encoding="utf-8").strip()
    if not configured:
        return None
    parts = urllib.parse.urlsplit(configured)
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def notify_captcha() -> None:
    endpoint = bark_endpoint()
    if not endpoint:
        print("[bark] 未配置 Bark 地址，跳过通知。")
        return
    try:
        with urllib.request.urlopen(endpoint, timeout=10) as response:
            print(f"[bark] 已发送验证码提醒，状态 {response.status}。")
    except urllib.error.URLError as exc:
        print(f"[bark] 发送失败: {exc}")


def require_human_captcha_resolution(page: Page) -> None:
    if not captcha_present(page):
        return
    print("\n[verification] 验证已过期或安全验证已出现。")
    print("请在可见浏览器中人工完成验证。脚本不会点击验证码，完成后会自动继续。")
    notify_captcha()
    while captcha_present(page):
        pause()
    print("[verification] 验证界面已关闭，继续。")
    pause(page)


def settle(page: Page, reason: str) -> None:
    """Wait out the detail/tab loading window before reading text."""
    seconds = pause(page)
    print(f"[wait] {seconds:.1f}s so {reason} can finish rendering.")
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
        pause()


class RateLimiter:
    def wait(self, reason: str) -> None:
        seconds = pause()
        print(f"[rate-limit] Waiting {seconds:.1f}s before {reason}.")


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
        if section == "业绩技术指标":
            ensure_performance_dialog_closed(page)
        limiter.wait(f"opening the next {section} page")
        page.locator(f"#{pane_id} .btn-next").click()
        settle(page, f"the next {section} page")
    return {"headers": headers, "rows": collected}


def performance_dialog_open(page: Page) -> bool:
    try:
        return bool(page.evaluate(PERFORMANCE_DIALOG_OPEN_JS))
    except Exception:
        return False


def wait_performance_dialog_ready(page: Page, record_id: str = "") -> None:
    """Wait until the detail dialog is visible, loaded, and text-stable."""
    deadline = time.monotonic() + 20.0
    last_text: str | None = None
    stable_passes = 0
    while time.monotonic() < deadline:
        require_human_captcha_resolution(page)
        state = page.evaluate(PERFORMANCE_DIALOG_STATE_JS, record_id or "")
        if not state.get("visible"):
            pause(page)
            continue
        if state.get("loading"):
            stable_passes = 0
            pause(page)
            continue
        if record_id and not state.get("recordMatch"):
            stable_passes = 0
            pause(page)
            continue
        text = str(state.get("text") or "")
        if text and text == last_text:
            stable_passes += 1
            if stable_passes >= 2:
                return
        else:
            stable_passes = 0
            last_text = text
        pause(page)
    print("[warning] 业绩技术指标详情弹窗未在时限内稳定，仍尝试读取并关闭。")


def close_performance_dialog(page: Page) -> bool:
    """Close the performance dialog; return True when it is no longer open."""
    for attempt in range(10):
        if not performance_dialog_open(page):
            return True
        try:
            outcome = page.evaluate(CLOSE_PERFORMANCE_DIALOG_JS)
        except Exception:
            outcome = {"present": True, "method": "error"}
        if outcome.get("method") == "none":
            page.keyboard.press("Escape")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not performance_dialog_open(page):
                return True
            pause(page)
    if performance_dialog_open(page):
        print("[warning] 业绩技术指标详情未能自动关闭，请人工点「关闭」；脚本将等待。")
        while performance_dialog_open(page):
            require_human_captcha_resolution(page)
            pause(page)
        return True
    return not performance_dialog_open(page)


def ensure_performance_dialog_closed(page: Page) -> None:
    while performance_dialog_open(page):
        close_performance_dialog(page)


def attach_performance_details(page: Page, rows: list[dict[str, str]], limiter: RateLimiter) -> list[dict[str, str]]:
    """Open 业绩技术指标 row dialogs when the table only summarizes the record."""
    pane_id = active_pane_id(page)
    detailed: list[dict[str, str]] = []
    ensure_performance_dialog_closed(page)
    for row in rows:
        item = dict(row)
        record_id = str(row.get("业绩记录编号") or "").strip()
        limiter.wait("opening a performance-detail dialog")
        opened = page.evaluate(
            OPEN_PERFORMANCE_ROW_JS,
            {"paneId": pane_id, "recordId": record_id},
        )
        if not opened:
            detailed.append(item)
            continue
        wait_performance_dialog_ready(page, record_id)
        dialog = page.evaluate(EXTRACT_PERFORMANCE_DIALOG_JS)
        if dialog:
            item["详情"] = dialog
        if not close_performance_dialog(page):
            ensure_performance_dialog_closed(page)
        detailed.append(item)
    ensure_performance_dialog_closed(page)
    return detailed


def list_rows(page: Page) -> list[dict[str, str]]:
    rows = page.evaluate(LIST_ROWS_JS)
    return rows if isinstance(rows, list) else []


def expected_region() -> str:
    if CURRENT_COMBO is not None:
        return CURRENT_COMBO.region_label
    return REGION_NAME


def results_are_shandong(page: Page) -> bool:
    rows = list_rows(page)
    if not rows:
        return False
    expected = expected_region()
    return all(expected in str(row.get("企业注册属地", "")) for row in rows)


def _select_region_level(page: Page, placeholder: str, name: str) -> bool:
    """Pick one level of the region cascade. Returns False if the platform
    does not offer this level (scouting must confirm which levels exist;
    a missing level is reported, never silently skipped)."""
    locator = page.locator(f".region-select input[placeholder='{placeholder}']")
    if locator.count() == 0:
        return False
    locator.first.click()
    pause(page)
    page.locator(".el-select-dropdown:visible .el-select-dropdown__item").filter(
        has_text=re.compile(rf"^{re.escape(name)}$")
    ).first.click()
    pause(page)
    return True


def apply_public_filters(page: Page) -> None:
    """Use the public filter controls. Any verification still stops for a person."""
    require_human_captcha_resolution(page)
    combo = CURRENT_COMBO or QueryCombo(qualification=QUALIFICATION_NAME)
    print(f"[filter] 资质类别: 建筑业企业；资质名称: {combo.qualification}；企业注册属地: {combo.province} {combo.city} {combo.county}".rstrip())
    page.locator(".labelInPutRadio span", has_text="建筑业企业").click()
    pause(page)
    page.locator(".quality-button .popAlert").click()
    dialog = page.locator(".quality-button-dialog")
    dialog.wait_for(state="visible", timeout=10_000)
    radio = dialog.locator(".el-radio", has_text=combo.qualification).first
    for _ in range(60):  # dialog paginates; walk until the option appears
        if radio.count() and radio.is_visible():
            break
        nxt = dialog.locator(".btn-next")
        if nxt.count() == 0 or nxt.is_disabled():
            break
        nxt.click()
        pause(page)
    radio.click()
    dialog.get_by_role("button", name="确 定").click()
    pause(page)
    if not _select_region_level(page, "请选择省级", combo.province):
        raise RuntimeError("省级属地选择器未找到，已停止。")
    if combo.city and not _select_region_level(page, "请选择市级", combo.city):
        raise RuntimeError(f"平台未提供市级属地选择器，无法按地市细化「{combo.city}」，已停止该组合（见 docs/COLLECTOR.md）。")
    if combo.county and not _select_region_level(page, "请选择区县级", combo.county):
        raise RuntimeError(f"平台未提供区县级属地选择器，无法按区县细化「{combo.county}」，已停止该组合（见 docs/COLLECTOR.md）。")
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
        pause(page)
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
        "source_query": (CURRENT_COMBO or QueryCombo(qualification=QUALIFICATION_NAME)).as_dict(),
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
        pause(list_page)
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
        raise RuntimeError(f"结果页出现了非「{expected_region()}」企业，已停止，避免采到未筛选列表。")
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
    combo = CURRENT_COMBO or QueryCombo(qualification=QUALIFICATION_NAME)
    try:
        apply_public_filters(page)
    except Exception as exc:
        print(f"[filter] 自动设置筛选未完成: {exc}")
        print("请在可见浏览器中手动设置后点击查询：")
        print(f"  资质名称: {combo.qualification}")
        print(f"  企业注册属地: {combo.province} {combo.city} {combo.county}".rstrip())
    print(f"[filter] 等待结果列表中的企业注册属地全部包含“{expected_region()}”。验证码出现时请人工处理。")
    while not results_are_shandong(page):
        require_human_captcha_resolution(page)
        if ready_flag.exists():
            ready_flag.unlink()
            settle(page, "the results list")
        pause()
    rows = list_rows(page)
    print(f"[ready] {len(rows)} rows on this page, all registered in {expected_region()}.")


def upsert(records: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    key = company_key(record)
    for index, existing in enumerate(records):
        if company_key(existing) == key:
            records[index] = record
            return records
    records.append(record)
    return records


def collect(args: argparse.Namespace, limiter: RateLimiter) -> int:
    global CURRENT_COMBO
    CURRENT_COMBO = QueryCombo(
        qualification=getattr(args, "qualification", QUALIFICATION_NAME) or QUALIFICATION_NAME,
        city=getattr(args, "city", "") or "",
        county=getattr(args, "county", "") or "",
    )
    if sync_playwright is None:
        raise SystemExit("playwright 未安装：先 pip install -r collector/requirements.txt && playwright install chromium")
    pages_seen = 0
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
                pages_seen = page_number
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
                        "source_query": CURRENT_COMBO.as_dict() if CURRENT_COMBO else {},
                        "pages_seen": pages_seen,
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
    # Overflow is a first-class outcome, never a silent truncation.
    try:
        from filters import is_overflow
    except ImportError:
        from collector.filters import is_overflow  # type: ignore
    overflow = is_overflow(pages_seen, len(records), args.max_pages)
    atomic_json_write(args.state, {
        "updated_at_utc": utc_now(),
        "records": len(records),
        "complete": len(completed),
        "last_company": records[-1].get("company_name") if records else None,
        "source_query": CURRENT_COMBO.as_dict() if CURRENT_COMBO else {},
        "pages_seen": pages_seen,
        "overflow": overflow,
        "status": "OVERFLOW" if overflow else "complete",
    })
    if overflow:
        print(f"[overflow] 该组合已跑满 {args.max_pages} 页（平台 450 条上限），结果不完整，必须下钻更细条件，见 docs/COLLECTOR.md。")
    print(f"Saved {len(records)} records to {args.output}")
    return 0


def combo_paths(combo: QueryCombo) -> tuple[Path, Path]:
    name = combo.output_name()
    output = DATA_DIR / name
    return output, DATA_DIR / (Path(name).stem + ".state.json")


def run_traversal(args: argparse.Namespace, limiter: RateLimiter) -> int:
    """Traverse qualification x city; drill OVERFLOW cities down to counties.

    Each combo gets its own output+state files. A combo whose state already
    says complete is skipped. County lists come from filters.load_counties()
    (enumerated from the platform); a city that overflows without county
    data is reported in needs_county_data, not treated as done.
    """
    try:
        from filters import city_combos, drill_down, load_counties, load_qualifications
    except ImportError:
        from collector.filters import city_combos, drill_down, load_counties, load_qualifications  # type: ignore
    qualifications = load_qualifications()
    if args.qualification:
        qualifications = [q for q in qualifications if q == args.qualification] or [args.qualification]
    counties = load_counties()
    queue: list[QueryCombo] = city_combos(qualifications)
    if args.city:
        queue = [c for c in queue if c.city == args.city]
    seen: set[tuple[str, str, str]] = set()
    while queue:
        combo = queue.pop(0)
        key = (combo.qualification, combo.city, combo.county)
        if key in seen:
            continue
        seen.add(key)
        output, state = combo_paths(combo)
        if state.exists():
            try:
                prior = json.loads(state.read_text(encoding="utf-8"))
            except Exception:
                prior = {}
            if prior.get("status") == "complete":
                print(f"[traverse-skip] 已完成 {combo.as_dict()}")
                continue
            if prior.get("overflow") and combo.level == "city":
                queue.extend(drill_down(combo, counties) or [])
                if not counties.get(combo.city):
                    print(f"[needs-county-data] {combo.city} 已 OVERFLOW 但尚无区县枚举数据，跳过下钻并保留标记。")
                continue
        print(f"\n[traverse] {combo.as_dict()} -> {output}")
        combo_args = argparse.Namespace(**vars(args))
        combo_args.qualification, combo_args.city, combo_args.county = combo.qualification, combo.city, combo.county
        combo_args.output, combo_args.state = output, state
        collect(combo_args, limiter)
        try:
            after = json.loads(state.read_text(encoding="utf-8"))
        except Exception:
            after = {}
        if after.get("overflow"):
            if combo.level == "city":
                children = drill_down(combo, counties)
                if children:
                    queue.extend(children)
                else:
                    print(f"[needs-county-data] {combo.city} OVERFLOW 且无区县数据，需先探路枚举。")
            elif combo.level == "county":
                print(f"[overflow-county] {combo.as_dict()} 区县级仍 OVERFLOW，需人工定下一步。")
    return 0


def print_plan(args: argparse.Namespace) -> int:
    """Print the traversal plan without opening a browser / collecting."""
    try:
        from filters import city_combos, load_counties, load_qualifications
    except ImportError:
        from collector.filters import city_combos, load_counties, load_qualifications  # type: ignore
    qualifications = load_qualifications()
    combos = city_combos(qualifications)
    counties = load_counties()
    print(f"资质 {len(qualifications)} 个 x 山东 {len(counties)} 地市 = {len(combos)} 个市级组合")
    for q in qualifications:
        print(f"  - {q}")
    county_total = sum(len(v) for v in counties.values())
    print(f"区县数据: {len(counties)} 市 / {county_total} 区县（仅 OVERFLOW 时下钻）")
    if not counties:
        print("  区县尚未枚举（collector/shandong_counties.json 不存在），OVERFLOW 时会标记 needs-county-data。")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--ready-flag", type=Path, default=Path("start_collect.flag"))
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--qualification", default="", help="资质名称（原文）；默认沿用历史单组合")
    parser.add_argument("--city", default="", help="山东地市，如 济南；默认省级单组合")
    parser.add_argument("--county", default="", help="区县，如 历下区；需同时给 --city")
    parser.add_argument("--traverse", action="store_true", help="遍历全部资质 x 山东各地市（平台字典 17 个），OVERFLOW 自动下钻区县")
    parser.add_argument("--plan-only", action="store_true", help="只打印遍历计划，不打开浏览器、不采集")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be positive")
    if args.plan_only:
        return print_plan(args)
    if args.traverse:
        return run_traversal(args, RateLimiter())
    # Single-combo mode: honour explicit --output/--state, otherwise derive
    # per-combo paths when a non-legacy combo was requested.
    if (args.city or args.county or (args.qualification and args.qualification != QUALIFICATION_NAME)) \
            and args.output == DEFAULT_OUTPUT:
        combo = QueryCombo(qualification=args.qualification or QUALIFICATION_NAME,
                             city=args.city or "", county=args.county or "")
        args.output, args.state = combo_paths(combo)
    return collect(args, RateLimiter())


if __name__ == "__main__":
    sys.exit(main())
