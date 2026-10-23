#!/usr/bin/env python3
"""
南京大学电费查询脚本 (异步版本)
用法: python3 nju_electric_query.py [--cookie-file COOKIE_FILE] [-d 输出目录] [-q] 宿舍ID1 宿舍ID2 ...
示例: python3 nju_electric_query.py --cookie-file /tmp/cookie.json -d ./database 53463 53464 53465
      python3 nju_electric_query.py -q -d ./database 53463 53464  # 安静模式，减少输出
"""

import asyncio
import aiohttp
import aiofiles
import argparse
import json
import os
import signal
import sys
import time
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from typing import Optional
import contextlib

# 默认 Cookie 文件路径
DEFAULT_COOKIE_FILE = "/tmp/cookie.json"

# 北京时间 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回当前北京时间的 naive datetime（用于文件名/日期标注）。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


class TeeLogger:
    """将 stdout/stderr 同时输出到文件和控制台"""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.file = None

    def __enter__(self):
        self.file = open(self.filepath, "w", encoding="utf-8")
        self.stdout_redirect = contextlib.redirect_stdout(self._tee(sys.stdout))
        self.stderr_redirect = contextlib.redirect_stderr(self._tee(sys.stderr))
        self.stdout_redirect.__enter__()
        self.stderr_redirect.__enter__()
        return self

    def __exit__(self, *args):
        self.stderr_redirect.__exit__(*args)
        self.stdout_redirect.__exit__(*args)
        self.file.close()

    def _tee(self, original_stream):
        class TeeStream:
            def __init__(self, file, original):
                self.file = file
                self.original = original
            def write(self, text):
                self.file.write(text)
                self.original.write(text)
            def flush(self):
                self.file.flush()
                self.original.flush()
        return TeeStream(self.file, original_stream)


# 重试配置
MAX_RETRIES = 5
RETRY_DELAY = 2  # 失败后等待秒数
RETRY_BACKOFF = 1.5  # 指数退避倍数
MAX_SCAN_RETRIES = 5  # 扫描模式下单个ID最大重试次数

# 并发配置
DEFAULT_CONCURRENCY = 1  # 默认并发数（单线程，避免触发限流）

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh,en;q=0.9,zh-TW;q=0.8",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Referer": "https://epay.nju.edu.cn/",
}

base_url = "https://epay.nju.edu.cn"


async def load_cookies_from_file(filepath: str) -> dict:
    """从浏览器导出的 JSON 文件加载 cookie"""
    try:
        async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
            content = await f.read()
            cookies_list = json.loads(content)

        cookies = {}
        for cookie in cookies_list:
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value:
                cookies[name] = value

        return cookies
    except FileNotFoundError:
        print(f"错误: Cookie 文件不存在: {filepath}")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"错误: Cookie 文件格式错误: {filepath}")
        sys.exit(1)


def parse_html(html: str) -> dict:
    """解析 HTML 页面，提取电费信息

    苏州校区 vs 非苏州校区的区别：
    - 非苏州校区：仅显示"剩余电量"，数值=余额（元）
    - 苏州校区：显示"剩余电量"（实际度数）和"剩余余额"（元）

    HTML 中有两个"剩余电量"块，通过 v-if 条件渲染：
    - 第一个：v-if="!isSuZhouArea" - 非苏州校区显示
    - 第二个：v-if="isSuZhouArea" - 苏州校区显示

    解决方案：根据校区名称判断，提取正确的字段

    注意：余额或电量可能为负数，负数情况下替换为"0度"或"0元"
    """
    result = {}

    # 从 JS 片段提取 this.check 的 JSON 数据
    match = re.search(r'this\.check\s*=\s*(\{.*\})', html)
    if match:
        try:
            check_data = json.loads(match.group(1))
            result["校区"] = check_data.get("sysName", "")
            result["楼栋"] = check_data.get("buildName", "")
            result["房间"] = check_data.get("roomName", "")
        except json.JSONDecodeError:
            pass

    campus = result.get("校区", "")
    is_suzhou = campus == "苏州校区"

    def normalize_value(value_str: str) -> str:
        """将负数替换为0，正数保持不变"""
        if value_str.startswith("-"):
            return f"0{value_str[-1]}"
        return value_str

    if is_suzhou:
        # 苏州校区：提取"剩余余额"字段（真正的余额，单位：元）
        match = re.search(r'剩余余额.*?<i>(-?[\d.]+元)</i>', html)
        if match:
            result["剩余余额"] = normalize_value(match.group(1))

        # 苏州校区也可以提取真正的电量（度）
        # 注意：苏州校区的"剩余电量"是第二个出现的，需要用 findall 或更精确的正则
        matches = re.findall(r'剩余电量.*?<i>(-?[\d.]+度)</i>', html)
        if len(matches) >= 2:
            # 第二个是苏州校区的真实电量
            result["剩余电量"] = normalize_value(matches[1])
    else:
        # 非苏州校区：提取第一个"剩余电量"，数值即为余额
        match = re.search(r'剩余电量.*?<i>(-?[\d.]+度)</i>', html)
        if match:
            balance = normalize_value(match.group(1))
            result["剩余电量"] = balance  # 非苏州校区，电量=余额

    return result


class QueryError:
    """查询错误类型"""
    NETWORK_ERROR = "网络错误"
    TIMEOUT = "请求超时"
    AUTH_FAILED = "认证失败"
    HTTP_ERROR = "HTTP错误"
    PARSE_ERROR = "解析失败"
    NOT_FOUND = "资源不存在"
    ROOM_NOT_FOUND = "房间不存在"
    RETRY_EXHAUSTED = "重试次数耗尽"
    UNKNOWN = "未知错误"


class RateLimitedError(Exception):
    """服务器返回限流响应时抛出，触发上层批量重试"""
    pass


async def query_single_with_retry(semaphore: asyncio.Semaphore, session: aiohttp.ClientSession, room_id: str, cookies: dict, show_retry: bool = True, request_delay: float = 0) -> dict:
    """带重试的异步查询单个宿舍电费"""
    url = urljoin(base_url, f"/epay/h5/nju/electric/charge?id={room_id}")
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            async with semaphore:
                async with session.get(url, cookies=cookies, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 404:
                        last_error = {"id": room_id, "error": QueryError.NOT_FOUND, "error_type": "not_found", "success": False}
                        break
                    elif response.status == 401 or response.status == 403:
                        last_error = {"id": room_id, "error": QueryError.AUTH_FAILED, "error_type": "auth_failed", "success": False}
                        break
                    elif response.status >= 500:
                        last_error = {"id": room_id, "error": f"{QueryError.HTTP_ERROR}({response.status})", "error_type": "http_error", "success": False}
                    elif response.status != 200:
                        last_error = {"id": room_id, "error": f"{QueryError.HTTP_ERROR}({response.status})", "error_type": "http_error", "success": False}
                    else:
                        html = await response.text()

                        # 检查是否是错误页面（房间ID不存在）
                        if "房间查询失败" in html or "查询房间信息失败" in html:
                            last_error = {"id": room_id, "error": QueryError.ROOM_NOT_FOUND, "error_type": "room_not_found", "success": False}
                            break

                        # 检查是否需要登录
                        if "login" in html.lower() or "登录" in html:
                            last_error = {"id": room_id, "error": QueryError.AUTH_FAILED, "error_type": "auth_failed", "success": False}
                            break

                        # 检查限流响应
                        if "查询已被限制" in html or "请60分钟后再试" in html:
                            raise RateLimitedError("查询已被限制，请60分钟后再试")

                        # 解析 HTML
                        result = parse_html(html)

                        # 检查是否解析成功
                        if not result.get("剩余电量"):
                            last_error = {"id": room_id, "error": QueryError.PARSE_ERROR, "error_type": "parse_error", "success": False}
                            break

                        result["success"] = True
                        result["id"] = room_id  # 用于内部追踪，save_result 会过滤掉
                        await asyncio.sleep(request_delay)  # Enforce delay inside semaphore, before release
                        return result

        except asyncio.TimeoutError:
            last_error = {"id": room_id, "error": QueryError.TIMEOUT, "error_type": "timeout", "success": False}
        except aiohttp.ClientConnectorError:
            last_error = {"id": room_id, "error": QueryError.NETWORK_ERROR, "error_type": "network_error", "success": False}
        except RateLimitedError:
            raise  # 让 RateLimitedError 传播到上层，触发批量级重试
        except Exception as e:
            error_msg = str(e).lower()
            if "timeout" in error_msg:
                last_error = {"id": room_id, "error": QueryError.TIMEOUT, "error_type": "timeout", "success": False}
            elif "connect" in error_msg:
                last_error = {"id": room_id, "error": QueryError.NETWORK_ERROR, "error_type": "network_error", "success": False}
            else:
                last_error = {"id": room_id, "error": f"{QueryError.UNKNOWN}: {str(e)}", "error_type": "unknown", "success": False}

        # 需要重试
        if attempt < MAX_RETRIES - 1:
            delay = RETRY_DELAY * (RETRY_BACKOFF ** attempt)
            if show_retry:
                print(f"\n  宿舍 {room_id} 第 {attempt + 1} 次尝试失败: {last_error.get('error', '未知')}, {delay:.1f}秒后重试...")
            await asyncio.sleep(delay)

    # 重试次数耗尽
    if last_error and last_error.get("error_type") not in ("auth_failed", "not_found", "room_not_found", "parse_error"):
        last_error["error"] = f"{QueryError.RETRY_EXHAUSTED}({last_error.get('error', '')})"
        last_error["error_type"] = "retry_exhausted"

    return last_error or {"id": room_id, "error": QueryError.UNKNOWN, "error_type": "unknown", "success": False}


async def _query_batch_internal(room_ids: list[str], cookies: dict, output_dir: Optional[Path],
                                 show_progress: bool, max_concurrent: int, request_delay: float,
                                 session: aiohttp.ClientSession) -> list[dict]:
    """查询一小批房间，带请求间隔控制。遇限流时取消剩余任务并返回部分结果。"""
    semaphore = asyncio.Semaphore(max_concurrent)

    async def limited_query(room_id):
        return await query_single_with_retry(
            semaphore, session, room_id, cookies, show_progress,
            request_delay=request_delay
        )

    # 创建所有任务
    task_map = {asyncio.create_task(limited_query(rid)): rid for rid in room_ids}
    pending = set(task_map.keys())
    results = {rid: None for rid in room_ids}
    rate_limited = False

    while pending and not rate_limited:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            rid = task_map[task]
            exc = task.exception()
            if exc is not None:
                if isinstance(exc, RateLimitedError):
                    rate_limited = True
                    break
                results[rid] = {"id": rid, "error": str(exc), "error_type": "unknown", "success": False}
            else:
                results[rid] = task.result()

    if rate_limited:
        # 取消剩余待处理任务
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending)
        # 标记未处理的房间为限流失败
        for t, rid in task_map.items():
            if results[rid] is None:
                results[rid] = {"id": rid, "error": "限流", "error_type": "rate_limited", "success": False}

    return [results[rid] for rid in room_ids]


async def query_batch(room_ids: list[str], cookies: dict, output_dir: Optional[Path] = None,
                      show_progress: bool = True, max_concurrent: int = DEFAULT_CONCURRENCY,
                      batch_size: int = 100,
                      request_delay: float = 1.0):
    """异步批量查询 - 分批处理 + 请求间隔 + 限流自动恢复"""
    total = len(room_ids)
    succeeded = 0
    failed = 0

    failed_details = []
    success_details = []

    connector = aiohttp.TCPConnector(limit=max_concurrent)
    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(0, total, batch_size):
            batch = room_ids[i:i + batch_size]
            retries = 0
            max_retries = 2

            while retries <= max_retries:
                results = await _query_batch_internal(
                    batch, cookies, output_dir, show_progress,
                    max_concurrent, request_delay, session
                )

                # 找出被限流的房间
                rate_limited_rooms = [
                    r for r in results
                    if not r["success"] and r.get("error_type") == "rate_limited"
                ]

                # 先处理非限流结果
                for result in results:
                    if result["success"]:
                        succeeded += 1
                        if output_dir:
                            await save_result(result, output_dir, quiet=not show_progress)
                        success_details.append({
                            "id": result["id"],
                            "building": result.get("楼栋", "未知"),
                            "room": result.get("房间", "未知"),
                            "power": result.get("剩余电量", "未知"),
                        })
                    elif result.get("error_type") != "rate_limited":
                        failed += 1
                        failed_details.append({
                            "id": result["id"],
                            "error": result.get("error", "未知错误"),
                            "error_type": result.get("error_type", "unknown"),
                        })

                if not rate_limited_rooms:
                    break  # 无限流，当前子批次完成

                retries += 1
                if retries > max_retries:
                    # 超过重试次数，标记为失败
                    failed += len(rate_limited_rooms)
                    for r in rate_limited_rooms:
                        failed_details.append({
                            "id": r["id"],
                            "error": "重试次数耗尽（限流）",
                            "error_type": "rate_limited",
                        })
                    break

                # 只重试被限流的房间
                batch = [r["id"] for r in rate_limited_rooms]
                if show_progress:
                    print(f"\n[限流] 检测到 {len(rate_limited_rooms)} 个限流，等待 3600s 后重试 (第 {retries} 次)...")
                await asyncio.sleep(3600)

            completed = succeeded + failed
            if show_progress:
                print(f"[{completed}/{total}] 成功: {succeeded}, 失败: {failed}")

    if success_details and show_progress:
        print("\n--- 查询成功 ---")
        for detail in success_details[:10]:
            print(f"  {detail['id']}: {detail['building']} {detail['room']} | 剩余电量: {detail['power']}")

    if failed_details and show_progress:
        print("\n--- 查询失败 (具体原因) ---")
        error_count = {}
        for detail in failed_details:
            error_type = detail.get("error_type", "unknown")
            error_count[error_type] = error_count.get(error_type, 0) + 1
        for error_type, count in error_count.items():
            print(f"  {error_type}: {count}个")

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "success_details": success_details,
        "failed_details": failed_details,
    }


def load_existing_ids(file_path: str) -> dict:
    """从文件加载已有ID及其楼栋信息

    Args:
        file_path: room_ids.txt 文件路径

    Returns:
        字典 {id: (campus, building)}，campus/building 可能为 None
    """
    existing = {}
    output_path = Path(file_path)
    if not output_path.exists():
        return existing

    current_campus = None
    current_building = None

    with open(output_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # 跳过空行
            if not line:
                continue
            # 解析注释行，格式: # 校区/楼栋
            if line.startswith('#'):
                comment = line[1:].strip()
                if '/' in comment:
                    parts = comment.split('/', 1)
                    current_campus = parts[0].strip()
                    current_building = parts[1].strip()
                else:
                    current_campus = comment
                    current_building = None
                continue
            # 验证是有效数字ID
            if line.isdigit():
                existing[line] = (current_campus, current_building)

    return existing


async def scan_room_ids(start_id: int, end_id: int, cookies: dict, output_file: str,
                         max_concurrent: int = DEFAULT_CONCURRENCY, show_progress: bool = True,
                         progress_file: str | None = None, batch_size: int = 3600,
                         request_delay: float = 3.0) -> dict:
    """扫描ID区间，发现存在的房间

    Args:
        start_id: 起始ID (包含)
        end_id: 结束ID (包含)
        cookies: Cookie字典
        output_file: 输出文件路径
        max_concurrent: 最大并发数
        show_progress: 是否显示进度
    """
    # 加载已有ID (使用 config_utils 的 room-name based mapping)
    from scripts.config_utils import load_mapping, extract_ids, is_room_known, update_id, save_mapping

    mapping = load_mapping(output_file)
    existing_id_set = set(extract_ids(mapping))
    if existing_id_set and show_progress:
        print(f"从 {output_file} 加载了 {len(existing_id_set)} 个已有ID")

    # 进度追踪
    if progress_file:
        cursor = 0
        cycle = 1
        range_end = end_id
        batches = {}
        cumulative = {"scanned": 0, "found": 0, "failed": 0}
        batch_seq = 0

        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                prog = json.load(f)
                cursor = prog.get("cursor", 0)
                cycle = prog.get("cycle", 1)
                range_end = prog.get("range_end", end_id)
                batches = prog.get("batches", {})
                cumulative = prog.get("cumulative", {"scanned": 0, "found": 0, "failed": 0})
                if batches:
                    batch_seq = max(int(k) for k in batches.keys())
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            print(f"警告: 无法读取进度文件 {progress_file}，从 cursor=0 开始")

        if cursor > 0 and show_progress:
            print(f"从进度文件恢复: cursor={cursor}, cycle={cycle}")

        prog_data = None  # 用于信号处理器的进度保存

    # 如果使用进度追踪，用 cursor 覆盖扫描区间
    if progress_file:
        scan_start = cursor + 1
        scan_end = min(cursor + batch_size, range_end)
        total = scan_end - scan_start + 1
    else:
        scan_start = start_id
        scan_end = end_id
        total = scan_end - scan_start + 1
    processed = 0
    new_found = 0
    skipped = 0  # 跳过的已有ID计数

    # 错误统计
    error_counts = {
        "room_not_found": 0,  # 房间不存在
        "auth_failed": 0,     # 需要登录
        "http_error": 0,      # HTTP错误
        "timeout": 0,         # 请求超时
        "network_error": 0,   # 网络错误
        "parse_error": 0,     # 解析失败
    }

    # 信号处理器
    def signal_handler():
        """处理终止信号，保存已发现的结果和进度"""
        print(f"\n\n收到终止信号，正在保存已发现的结果...")
        save_mapping(mapping, output_file)
        # 保存进度文件（如果启用且已初始化）
        if progress_file:
            try:
                tmp_file = progress_file + ".tmp"
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(prog_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_file, progress_file)
            except Exception:
                pass
        os._exit(0)  # 立即退出，防止重入

    # 注册 asyncio 信号处理器
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    semaphore = asyncio.Semaphore(max_concurrent)

    async def scan_single(session, room_id):
        nonlocal processed, new_found
        url = urljoin(base_url, f"/epay/h5/nju/electric/charge?id={room_id}")
        attempt = 0  # 重试次数

        while attempt < MAX_SCAN_RETRIES:
            try:
                async with (
                        semaphore,
                        session.get(url, cookies=cookies, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as response
                    ):
                    if response.status != 200:
                        # 可重试错误，继续循环
                        error_counts["http_error"] += 1
                        delay = RETRY_DELAY * (RETRY_BACKOFF ** attempt)
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue

                    html = await response.text()

                    # 检查是否是错误页面（房间不存在）- 永久错误
                    if "房间查询失败" in html or ("查询房间信息失败" in html):
                        error_counts["room_not_found"] += 1
                        break

                    # 检查是否需要登录 - 可重试错误
                    if "login" in html.lower() or "登录" in html:
                        error_counts["auth_failed"] += 1
                        delay = RETRY_DELAY * (RETRY_BACKOFF ** attempt)
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue

                    # 检查限流响应
                    if "查询已被限制" in html or "请60分钟后再试" in html:
                        raise RateLimitedError("查询已被限制，请60分钟后再试")

                    # 解析房间信息
                    result = parse_html(html)
                    if not result.get("校区") or not result.get("楼栋") or not result.get("房间"):
                        # 解析失败 - 永久错误
                        error_counts["parse_error"] += 1
                        print(f"=" * 100)
                        print(f"{html}")
                        print(f"{room_id = }")
                        break

                    # 成功：实时去重并记录（基于房间名去重）
                    campus = result.get("校区", "")
                    building = result.get("楼栋", "")
                    room_name = result.get("房间", "")

                    # Track whether this is a new room before updating
                    if not is_room_known(mapping, campus, building, room_name):
                        new_found += 1

                    # Update ID (add new or replace existing)
                    update_id(mapping, campus, building, room_name, str(room_id))

                    await asyncio.sleep(request_delay)

                    break

            except asyncio.TimeoutError:
                # 可重试错误
                error_counts["timeout"] += 1
                delay = RETRY_DELAY * (RETRY_BACKOFF ** attempt)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            except aiohttp.ClientConnectorError as e:
                # 可重试错误
                error_counts["network_error"] += 1
                # print(f"NetworkError Client Error: {e}")
                delay = RETRY_DELAY * (RETRY_BACKOFF ** attempt)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            except RateLimitedError:
                raise  # 让 RateLimitedError 传播到上层，触发批量级取消
            except Exception:
                # 可重试错误
                error_counts["network_error"] += 1
                delay = RETRY_DELAY * (RETRY_BACKOFF ** attempt)
                await asyncio.sleep(delay)
                attempt += 1
                continue

        # 只在函数退出时更新进度
        processed += 1
        if show_progress:
            print(f"[{processed}/{scan_count}] 新发现: {new_found}")

    # 生成待扫描的ID列表，跳过已有ID
    ids_to_scan = []
    for room_id in range(scan_start, scan_end + 1):
        if str(room_id) in existing_id_set:
            skipped += 1
        else:
            ids_to_scan.append(room_id)

    scan_count = len(ids_to_scan)
    if show_progress:
        print(f"跳过 {skipped} 个已有ID，待扫描 {scan_count} 个ID")

    connector = aiohttp.TCPConnector(limit=max_concurrent)
    async with aiohttp.ClientSession(connector=connector) as session:
        task_map = {}
        pending = set()
        rate_limited = False
        rate_limit_retries = 0
        max_rate_limit_retries = 2
        rate_limited_id = None

        # 逐个创建任务
        for room_id in ids_to_scan:
            if rate_limited:
                break
            task = asyncio.create_task(scan_single(session, room_id))
            task_map[task] = room_id
            pending.add(task)

        # 等待所有任务完成（或遇限流取消）
        while pending and not rate_limited:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                rid = task_map.get(task)
                exc = task.exception()
                if exc is not None:
                    if isinstance(exc, RateLimitedError):
                        rate_limited = True
                        rate_limited_id = rid
                        break
                    # 其他异常已在 scan_single 内部处理

        if rate_limited:
            # 取消剩余的待处理任务
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending)
            error_counts["rate_limited"] = error_counts.get("rate_limited", 0) + 1
            while rate_limit_retries < max_rate_limit_retries:
                rate_limit_retries += 1
                print(f"\n[限流] 等待 3600s 后重试 ID {rate_limited_id} (第 {rate_limit_retries} 次)...")
                await asyncio.sleep(3600)
                # 重试被限流的 ID
                task = asyncio.create_task(scan_single(session, rate_limited_id))
                try:
                    await task
                    # 重试成功
                    break
                except RateLimitedError:
                    print(f"  ID {rate_limited_id} 重试仍被限流，跳过")
                    error_counts["rate_limited"] = error_counts.get("rate_limited", 0) + 1

    total_errors = sum(error_counts.values())

    if show_progress:
        print()

    save_mapping(mapping, output_file)

    # 保存进度
    if progress_file:
        batch_seq += 1
        new_cursor = scan_end if not rate_limited else rate_limited_id - 1
        if new_cursor >= range_end:
            new_cursor = 0
            cycle += 1
            batches = {}
            cumulative = {"scanned": 0, "found": 0, "failed": 0}
        else:
            batches[str(batch_seq)] = {
                "scanned": scan_count,
                "found": new_found,
                "failed": error_counts.get("rate_limited", 0),
                "date": beijing_now().strftime("%Y-%m-%d"),
                "cycle": cycle,
            }
            cumulative["scanned"] += scan_count
            cumulative["found"] += new_found
            cumulative["failed"] += error_counts.get("rate_limited", 0)

        prog_data = {
            "cycle": cycle,
            "date": beijing_now().strftime("%Y-%m-%d"),
            "range_start": start_id,
            "range_end": range_end,
            "cursor": new_cursor,
            "batch_size": batch_size,
            "batches": batches,
            "cumulative": cumulative,
        }

        # 原子写入
        tmp_file = progress_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(prog_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, progress_file)

    if show_progress:
        print(f"扫描完成: 扫描 {scan_count} 个ID, 发现 {new_found} 个新房间, 跳过 {skipped} 个已有ID")
        total_rooms = len(extract_ids(mapping))
        print(f"结果已保存到: {output_file} (共 {total_rooms} 个ID)")

        if total_errors > 0:
            print("\n--- 错误统计 ---")
            error_messages = {
                "room_not_found": "房间不存在",
                "auth_failed": "认证失败",
                "http_error": "HTTP错误",
                "timeout": "请求超时",
                "network_error": "网络错误",
                "parse_error": "解析失败",
                "rate_limited": "限流跳过",
            }
            for error_type, count in error_counts.items():
                if count > 0:
                    print(f"  {error_messages[error_type]}: {count}")

    return {
        "total": total,
        "scanned": scan_count,
        "found": new_found,
        "skipped": skipped,
        "errors": error_counts,
        "total_errors": total_errors,
        "output_file": output_file
    }


async def save_result(result: dict, output_dir: Path, quiet: bool = False):
    """保存结果到文件，格式: {校区}/{楼栋}/{房间}/{日期}.json"""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        if not quiet:
            print(f"\n错误: 没有权限创建目录 {output_dir}")
        return False
    except Exception as e:
        if not quiet:
            print(f"\n错误: 无法创建目录 {output_dir}: {e}")
        return False

    campus = result.get("校区", "未知校区")
    building = result.get("楼栋", "未知楼栋")
    room = result.get("房间", "未知房间")

    campus = re.sub(r'[<>:"/\\|?*]', '_', campus)
    building = re.sub(r'[<>:"/\\|?*]', '_', building)
    room = re.sub(r'[<>:"/\\|?*]', '_', room)

    dir_path = output_dir / campus / building / room
    date_str = beijing_now().strftime("%Y%m%d")
    filename = f"{date_str}.json"
    filepath = dir_path / filename

    try:
        dir_path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        if not quiet:
            print(f"\n错误: 没有权限创建目录 {dir_path}")
        return False
    except Exception as e:
        if not quiet:
            print(f"\n错误: 无法创建目录 {dir_path}: {e}")
        return False

    if filepath.exists():
        if not quiet:
            print(f"\n警告: 文件 {filepath} 已存在，跳过保存")
        return False

    # Remove internal room identifiers and personal identifiers before persistence.
    save_data = {
        k: v for k, v in result.items() if k not in ('id', '宿舍ID', '学号')
    }

    try:
        async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
            await f.write(json.dumps(save_data, ensure_ascii=False, indent=2))
        return True
    except PermissionError:
        if not quiet:
            print(f"\n错误: 没有权限写入文件 {filepath}")
        return False
    except Exception as e:
        if not quiet:
            print(f"\n错误: 无法写入文件 {filepath}: {e}")
        return False


async def async_main():
    parser = argparse.ArgumentParser(description="南京大学电费查询工具")
    parser.add_argument("-d", "--dir", type=str, help="输出目录", default=None)
    parser.add_argument("-c", "--concurrency", type=int, help=f"最大并发数 (默认{DEFAULT_CONCURRENCY})", default=DEFAULT_CONCURRENCY)
    parser.add_argument("--cookie-file", type=str, help="Cookie JSON文件路径", default=DEFAULT_COOKIE_FILE)
    parser.add_argument("-q", "--quiet", action="store_true", help="安静模式，减少输出")
    parser.add_argument("--scan", type=int, nargs=2, metavar=('START', 'END'), help="扫描ID区间模式: 扫描指定范围内的所有ID")
    parser.add_argument("--scan-output", type=str, default="config/room_ids.json", help="扫描结果输出文件 (默认: config/room_ids.json)")
    parser.add_argument("--scan-progress", type=str, help="扫描进度文件路径")
    parser.add_argument("--scan-batch-size", type=int, default=3600, help="每批扫描 ID 数（默认 3600）")
    parser.add_argument("--from-mapping", type=str, help="从JSON映射文件读取房间ID列表")
    parser.add_argument("--batch-size", type=int, default=100, help="小批量大小（默认 100）")
    parser.add_argument("--request-delay", type=float, default=3.0, help="请求间最小间隔秒数（默认 3.0）")
    parser.add_argument("--batch-index", type=int, default=1, help="当前批次序号（从 1 开始，默认 1）")
    parser.add_argument("--total-batches", type=int, default=1, help="总批次数（默认 1）")
    parser.add_argument("--log-file", type=str, help="日志文件路径（同时输出到文件和控制台）")
    parser.add_argument("room_ids", nargs="*", help="宿舍ID列表 (扫描模式下不需要)")
    args = parser.parse_args()

    # 日志文件输出
    tee = None
    if args.log_file:
        try:
            log_dir = os.path.dirname(args.log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            tee = TeeLogger(args.log_file)
            tee.__enter__()
        except (OSError, IOError) as e:
            tee = None
            print(f"警告: 无法创建日志文件 {args.log_file}: {e}，继续运行但不输出到文件")

    start_time = time.time()
    summary_data = None
    scan_result = None
    scan_mode = False

    try:
        # 验证批次参数
        if args.batch_size <= 0:
            print("错误: --batch-size 必须大于 0")
            sys.exit(1)
        if args.total_batches <= 0:
            print("错误: --total-batches 必须大于 0")
            sys.exit(1)
        if args.batch_index < 1 or args.batch_index > args.total_batches:
            print(f"错误: --batch-index 必须在 1 到 {args.total_batches} 之间")
            sys.exit(1)

        room_ids = args.room_ids
        output_dir = Path(args.dir) if args.dir else None
        max_concurrent = args.concurrency
        cookie_file = args.cookie_file
        show_progress = not args.quiet

        if args.from_mapping:
            from pathlib import Path as _Path
            if not _Path(args.from_mapping).exists():
                print(f"错误: 映射文件不存在: {args.from_mapping}")
                sys.exit(1)
            from scripts.config_utils import load_mapping, extract_ids
            mapping = load_mapping(args.from_mapping)
            try:
                room_ids = extract_ids(mapping)
            except (AttributeError, TypeError) as e:
                print(f"错误: 映射文件格式错误: {args.from_mapping} ({e})")
                sys.exit(1)
            if not room_ids:
                print(f"错误: 映射文件 {args.from_mapping} 中没有找到任何房间ID")
                sys.exit(1)
            if show_progress:
                print(f"✓ 从映射文件加载了 {len(room_ids)} 个房间ID: {args.from_mapping}")

            # 按批次切片
            if args.total_batches > 1:
                total_rooms = len(room_ids)
                chunk_size = (total_rooms + args.total_batches - 1) // args.total_batches  # ceil division
                start_idx = chunk_size * (args.batch_index - 1)
                end_idx = min(chunk_size * args.batch_index, total_rooms)
                room_ids = room_ids[start_idx:end_idx]
                if show_progress:
                    print(f"✓ 批次 {args.batch_index}/{args.total_batches}: 查询 {len(room_ids)} 个房间 (切片 [{start_idx}:{end_idx}])")

        if not os.path.exists(cookie_file):
            print(f"错误: Cookie 文件不存在: {cookie_file}")
            print(f"请使用 --cookie-file 参数指定有效的 cookie 文件路径")
            sys.exit(1)
    
        cookies = await load_cookies_from_file(cookie_file)
        if show_progress:
            print(f"✓ 已加载 Cookie 文件: {cookie_file}")

        # 扫描模式
        if args.scan:
            start_id, end_id = args.scan
            if start_id > end_id:
                print(f"错误: 起始ID ({start_id}) 不能大于结束ID ({end_id})")
                sys.exit(1)

            if show_progress:
                print(f"开始扫描ID区间: {start_id} - {end_id} (共 {end_id - start_id + 1} 个ID)")
                print(f"并发数: {max_concurrent}")
                print("-" * 50)

            result = await scan_room_ids(
                start_id, end_id, cookies, args.scan_output,
                max_concurrent, show_progress,
                progress_file=args.scan_progress,
                batch_size=args.scan_batch_size,
                request_delay=args.request_delay,
            )
            scan_result = result; scan_mode = True
            elapsed = time.time() - start_time

            if show_progress:
                print("-" * 50)
                print(f"扫描完成!")
                print(f"  扫描: {result['scanned']}")
                print(f"  发现: {result['found']}")
                print(f"  跳过: {result['skipped']}")
                print(f"  错误: {result['total_errors']}")
                print(f"  耗时: {elapsed:.2f}秒")
                print(f"  输出: {result['output_file']}")
                print("-" * 50)

            return

        # 正常查询模式
        if not room_ids:
            print("错误: 请提供宿舍ID列表或使用 --scan 模式")
            sys.exit(1)

        if output_dir and output_dir.exists():
            if not output_dir.is_dir():
                print(f"错误: {output_dir} 不是一个目录")
                sys.exit(1)
            if not os.access(output_dir, os.W_OK):
                print(f"错误: 没有权限写入目录 {output_dir}")
                sys.exit(1)

        if show_progress:
            print(f"开始查询 {len(room_ids)} 个宿舍 (并发数: {max_concurrent})...")
            print("-" * 50)

        summary_data = await query_batch(
            room_ids, cookies, output_dir,
            show_progress=show_progress,
            max_concurrent=max_concurrent,
            batch_size=args.batch_size,
            request_delay=args.request_delay,
        )
        elapsed = time.time() - start_time

        if show_progress:
            print("=" * 50)
            print(f"查询完成!")
            print(f"  总数: {summary_data['total']}")
            print(f"  成功: {summary_data['succeeded']}")
            print(f"  失败: {summary_data['failed']}")
            print(f"  耗时: {elapsed:.2f}秒")
            if output_dir:
                print(f"  输出目录: {output_dir.absolute()}")
            print("=" * 50)
        else:
            print(f"成功: {summary_data['succeeded']}")
            print(f"失败: {summary_data['failed']}")
            print(f"耗时: {elapsed:.2f}s")
            print(f"完成: {summary_data['succeeded']}/{summary_data['total']} 成功, 失败 {summary_data['failed']}, 耗时 {elapsed:.2f}s")

        if summary_data['failed'] > 0:
            print("\n--- 失败原因统计 ---")
            error_count = {}
            for detail in summary_data.get("failed_details", []):
                error_type = detail.get("error_type", "unknown")
                error_count[error_type] = error_count.get(error_type, 0) + 1

            error_messages = {
                "network_error": "网络错误: 无法连接到服务器",
                "timeout": "请求超时: 服务器响应过慢",
                "auth_failed": "认证失败: Cookie已过期，请更新认证信息",
                "not_found": "资源不存在: 宿舍ID无效或已下架",
                "room_not_found": "房间不存在: 该房间ID在系统中不存在",
                "http_error": "HTTP错误: 服务器内部错误",
                "parse_error": "解析失败: 页面格式已更新",
                "retry_exhausted": "重试次数耗尽",
                "unknown": "未知错误",
            }
            for error_type, count in error_count.items():
                msg = error_messages.get(error_type, error_type)
                print(f"  {msg}: {count}个")

        # 90% success threshold
        success_rate = summary_data['succeeded'] / summary_data['total'] if summary_data['total'] > 0 else 0
        if success_rate < 0.9:
            print(f"错误: 成功率 {success_rate:.1%} ({summary_data['succeeded']}/{summary_data['total']}) 低于 90% 阈值")
            sys.exit(1)
    finally:
        elapsed = time.time() - start_time
        if scan_mode:
            print(f"RESULT: scanned={scan_result['scanned']} found={scan_result['found']} skipped={scan_result['skipped']} errors={scan_result['total_errors']} elapsed={elapsed:.2f}s")
        elif summary_data is not None:
            print(f"RESULT: total={summary_data['total']} success={summary_data['succeeded']} failed={summary_data['failed']} elapsed={elapsed:.2f}s")
        else:
            print(f"RESULT: total=0 success=0 failed=0 elapsed={elapsed:.2f}s")
        if tee is not None:
            tee.__exit__(None, None, None)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
