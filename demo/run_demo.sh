#!/usr/bin/env bash
# 离线演示脚本：启动本地静态报价站 / 用电明细站（仅需 Python 标准库），
# 然后调用套餐比较 API 与峰谷标签核查 API。
# 前置：API 已在 8000 端口运行
#   uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或直接使用 docker compose up --build（站点在 quote-site:80）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${QUOTE_SITE_PORT:-8080}"
API_BASE="${API_BASE:-http://127.0.0.1:8000}"
COMPARE_URL="${COMPARE_URL:-$API_BASE/api/v1/compare-plans}"
VERIFY_URL="${VERIFY_URL:-$API_BASE/api/v1/verify-labels}"

python3 -m http.server "$PORT" --directory "$ROOT/quote-site" >/tmp/quote-site.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
sleep 1

PORT="$PORT" COMPARE_URL="$COMPARE_URL" VERIFY_URL="$VERIFY_URL" DEMO_ROOT="$ROOT" python3 - <<'PY'
import json, os, sys, urllib.request

ROOT = os.environ["DEMO_ROOT"]


def post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as resp:
        return json.loads(resp.read())


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


site = f"http://127.0.0.1:{os.environ['PORT']}"

compare_payload = load(os.path.join(ROOT, "demo/request.json"))
compare_payload["list_url"] = f"{site}/index.html"
print("== 套餐比较 ==")
print(json.dumps(post(os.environ["COMPARE_URL"], compare_payload), ensure_ascii=False, indent=2))

verify_payload = load(os.path.join(ROOT, "demo/verify_request.json"))
verify_payload["list_url"] = f"{site}/usage/index.html"
print("== 峰谷标签核查 ==")
print(json.dumps(post(os.environ["VERIFY_URL"], verify_payload), ensure_ascii=False, indent=2))
PY
