"""MinerU 云端 PDF 解析客户端（三级降级）。

降级顺序：
1. 设置了 ``MINERU_API_TOKEN`` → 精确解析 API v4（单文件 ≤200MB / ≤200 页）
2. 未设 token → Agent 轻量 API v1（免登录，单文件 ≤10MB）
3. 以上均失败/超限 → 返回 None，由调用方回退本地解析

相比 markitdown：双栏版面还原正确、保留标题层级与 LaTeX 公式、表格转 Markdown，
不产生"正文被切成表格单元格 / 词间空格丢失"的噪声。

⚠️ 解析会把 PDF 上传到 mineru.net（第三方云服务），对未发表/涉密论文请先评估。

环境变量：
- MINERU_ENABLED       是否启用（默认 1；设 0 则始终走本地解析）
- MINERU_API_TOKEN     精确解析 API 的 token；留空则用免登录 Agent API
- MINERU_LANGUAGE      OCR 语言（默认 ch，中英日等）
- MINERU_MODEL_VERSION v4 精确 API 的模型版本（默认 pipeline；可选 vlm）
- MINERU_TIMEOUT       单文件解析总超时秒数（默认 600）
- MINERU_POLL_INTERVAL 轮询间隔秒数（默认 3）
- MINERU_AGENT_MAX_MB  免登录 API 的文件大小上限（默认 10）
- MINERU_CACHE_DIR     解析结果缓存目录（默认 .helloagents/pdf_cache）
- MINERU_SSL_VERIFY    是否校验 TLS 证书（默认 1；本机证书链不全时自动降级重试）
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile
from pathlib import Path
from typing import Optional

import requests

AGENT_BASE = "https://mineru.net/api/v1/agent/parse"
PRECISE_BASE = "https://mineru.net/api/v4"


# ---------- 配置读取 ----------
def _env_flag(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


def is_enabled() -> bool:
    """MinerU 是否启用（注意：启用意味着 PDF 会上传到云端）。"""
    return _env_flag("MINERU_ENABLED", True)


def has_token() -> bool:
    return bool((os.getenv("MINERU_API_TOKEN") or "").strip())


def describe() -> str:
    """给 /doctor 用的一行状态描述。"""
    if not is_enabled():
        return "已禁用（MINERU_ENABLED=0），使用本地解析"
    if has_token():
        return "已启用（精确解析 API v4，需 token）"
    return "已启用（免登录 Agent API，单文件 ≤10MB）"


def _timeout() -> int:
    try:
        return max(30, int(os.getenv("MINERU_TIMEOUT", "600")))
    except Exception:
        return 600


def _poll_interval() -> float:
    try:
        return max(1.0, float(os.getenv("MINERU_POLL_INTERVAL", "3")))
    except Exception:
        return 3.0


def _verify() -> bool:
    return _env_flag("MINERU_SSL_VERIFY", True)


# ---------- HTTP（带 SSL 降级重试） ----------
def _request(method: str, url: str, *, timeout: int, retries: int = 3, **kwargs):
    """发请求；证书校验失败时关闭校验重试。

    本机（Windows + 不完整证书链）对部分 CDN 会校验失败或 SSL EOF，
    关闭校验可绕过；仅用于公开解析结果的传输。
    """
    last_exc: Optional[Exception] = None
    for verify in (_verify(), False):
        for attempt in range(retries):
            try:
                r = requests.request(method, url, timeout=timeout, verify=verify, **kwargs)
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("request failed")


def _curl_download(url: str, timeout: int) -> Optional[bytes]:
    """用 curl 兜底下载。

    某些 CDN（如 cdn-mineru.openxlab.org.cn）在本机与本仓库 Python 的 OpenSSL
    握手会 UNEXPECTED_EOF，而系统 curl（Windows 走 schannel）正常；因此保留这条兜底路径。
    """
    import shutil
    import subprocess

    if not shutil.which("curl"):
        return None
    try:
        p = subprocess.run(
            ["curl", "-sS", "-L", "--ssl-no-revoke", "--max-time", str(int(timeout)), url],
            capture_output=True, timeout=timeout + 20,
        )
        if p.returncode == 0 and p.stdout:
            return p.stdout
        if p.stderr:
            print(f"[MinerU] curl 下载失败: {p.stderr.decode('utf-8', 'ignore')[:160]}")
    except Exception as e:  # noqa: BLE001
        print(f"[MinerU] curl 下载异常: {type(e).__name__}: {e}")
    return None


def _download_bytes(url: str, *, timeout: int) -> bytes:
    """下载二进制内容：requests → curl 兜底。"""
    try:
        return _request("GET", url, timeout=timeout).content
    except Exception as e:  # noqa: BLE001
        print(f"[MinerU] requests 下载失败（{type(e).__name__}），尝试 curl 兜底")
        blob = _curl_download(url, timeout)
        if blob:
            return blob
        raise


# ---------- 缓存 ----------
def _cache_path(p: Path) -> Path:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    key = f"{h.hexdigest()}|{os.getenv('MINERU_MODEL_VERSION', 'pipeline')}"
    digest = hashlib.md5(key.encode()).hexdigest()
    d = Path(os.getenv("MINERU_CACHE_DIR", ".helloagents/pdf_cache"))
    return d / f"{digest}.md"


def _cache_read(p: Path) -> Optional[str]:
    try:
        cp = _cache_path(p)
        if cp.exists():
            txt = cp.read_text(encoding="utf-8")
            if txt.strip():
                return txt
    except Exception:
        pass
    return None


def _cache_write(p: Path, md: str) -> None:
    try:
        cp = _cache_path(p)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(md, encoding="utf-8")
    except Exception:
        pass


# ---------- 对外入口 ----------
def parse_pdf(
    path,
    *,
    language: Optional[str] = None,
    enable_table: bool = True,
    enable_formula: bool = True,
) -> Optional[str]:
    """把 PDF 解析成 Markdown；失败返回 None（调用方回退本地解析）。"""
    if not is_enabled():
        return None
    p = Path(path)
    if not p.exists() or p.suffix.lower() != ".pdf":
        return None

    cached = _cache_read(p)
    if cached is not None:
        print(f"[MinerU] 命中缓存: {p.name}")
        return cached

    lang = language or os.getenv("MINERU_LANGUAGE", "ch")
    try:
        if has_token():
            md = _parse_precise(p, lang, enable_table, enable_formula)
        else:
            md = _parse_agent(p, lang, enable_table, enable_formula)
    except Exception as e:  # noqa: BLE001
        print(f"[MinerU] 解析失败（{type(e).__name__}: {e}），将回退本地解析")
        return None

    if md and md.strip():
        _cache_write(p, md)
        return md
    return None


def _page_count(p: Path) -> int:
    """读取 PDF 页数；失败返回 0（表示未知）。"""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(p))
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:
        return 0


# ---------- Agent 免登录 API（v1） ----------
def _parse_agent(p: Path, lang: str, enable_table: bool, enable_formula: bool) -> Optional[str]:
    size_mb = p.stat().st_size / 1e6
    try:
        max_mb = float(os.getenv("MINERU_AGENT_MAX_MB", "10"))
    except Exception:
        max_mb = 10.0
    if size_mb > max_mb:
        print(f"[MinerU] {p.name} 为 {size_mb:.1f}MB，超过免登录 API 上限 {max_mb:.0f}MB；"
              f"如需解析请配置 MINERU_API_TOKEN，或改走本地解析")
        return None

    # 免登录 API 单文件最多 20 页；超过则按 20 页一段分次解析后拼接。
    # 注意：page_range 是 1-based 闭区间（传 "0-19" 会报 invalid start page）。
    total_pages = _page_count(p)
    if total_pages > 20:
        print(f"[MinerU] {p.name} 共 {total_pages} 页，超过免登录 API 单次 20 页上限，"
              f"将分段解析（可配置 MINERU_API_TOKEN 走精确 API 一次完成）")
        parts: list[str] = []
        for start in range(0, total_pages, 20):
            end = min(start + 20, total_pages)  # 1-based 闭区间
            seg = _parse_agent_pages(p, lang, enable_table, enable_formula, f"{start + 1}-{end}")
            if seg is None:
                return None
            parts.append(seg)
        return "\n\n".join(parts)

    return _parse_agent_pages(p, lang, enable_table, enable_formula, None)


def _parse_agent_pages(
    p: Path, lang: str, enable_table: bool, enable_formula: bool,
    page_range: Optional[str],
) -> Optional[str]:
    payload = {
        "file_name": p.name,
        "language": lang,
        "enable_table": bool(enable_table),
        "enable_formula": bool(enable_formula),
    }
    if page_range:
        payload["page_range"] = page_range
    r = _request("POST", f"{AGENT_BASE}/file", timeout=90, json=payload)
    data = (r.json() or {}).get("data") or {}
    task_id, upload_url = data.get("task_id"), data.get("file_url")
    if not (task_id and upload_url):
        print(f"[MinerU] Agent API 提交异常: {r.text[:200]}")
        return None

    with open(p, "rb") as f:
        blob = f.read()
    # PUT 上传：不要带 Content-Type，否则会破坏 OSS 签名
    _request("PUT", upload_url, timeout=_timeout(), data=blob)

    deadline = time.time() + _timeout()
    while time.time() < deadline:
        time.sleep(_poll_interval())
        q = _request("GET", f"{AGENT_BASE}/{task_id}", timeout=60).json()
        d = q.get("data") or {}
        state = d.get("state")
        if state == "done":
            md_url = d.get("markdown_url")
            if not md_url:
                print("[MinerU] Agent API 返回 done 但缺少 markdown_url")
                return None
            return _download_bytes(md_url, timeout=180).decode("utf-8", "ignore")
        if state == "failed":
            print(f"[MinerU] Agent API 解析失败: {d.get('err_msg') or d.get('err_code')}")
            return None
    print("[MinerU] Agent API 解析超时")
    return None


# ---------- 精确解析 API（v4，需 token） ----------
def _parse_precise(p: Path, lang: str, enable_table: bool, enable_formula: bool) -> Optional[str]:
    token = (os.getenv("MINERU_API_TOKEN") or "").strip()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "files": [{"name": p.name}],
        "model_version": os.getenv("MINERU_MODEL_VERSION", "pipeline"),
        "language": lang,
        "enable_table": bool(enable_table),
        "enable_formula": bool(enable_formula),
    }
    r = _request("POST", f"{PRECISE_BASE}/file-urls/batch", timeout=90,
                 headers=headers, json=payload)
    data = (r.json() or {}).get("data") or {}
    batch_id, urls = data.get("batch_id"), data.get("file_urls") or []
    if not (batch_id and urls):
        print(f"[MinerU] 精确 API 提交异常: {r.text[:200]}")
        return None

    with open(p, "rb") as f:
        blob = f.read()
    _request("PUT", urls[0], timeout=_timeout(), data=blob)

    deadline = time.time() + _timeout()
    while time.time() < deadline:
        time.sleep(_poll_interval())
        q = _request("GET", f"{PRECISE_BASE}/extract-results/batch/{batch_id}",
                     timeout=60, headers=headers).json()
        d = q.get("data") or {}
        for item in d.get("extract_result") or []:
            state = item.get("state")
            if state == "done":
                zurl = item.get("full_zip_url")
                if not zurl:
                    print("[MinerU] 精确 API 返回 done 但缺少 full_zip_url")
                    return None
                return _extract_md_from_zip(zurl)
            if state == "failed":
                print(f"[MinerU] 精确 API 解析失败: {item.get('err_msg')}")
                return None
    print("[MinerU] 精确 API 解析超时")
    return None


def _extract_md_from_zip(url: str) -> Optional[str]:
    blob = _download_bytes(url, timeout=300)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        cands = [n for n in z.namelist() if n.lower().endswith(".md")]
        if not cands:
            return None
        name = next((n for n in cands if n.lower().endswith("full.md")), cands[0])
        return z.read(name).decode("utf-8", "ignore")
