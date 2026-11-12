#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# AIDBM 质量门禁（文档 §11.2 / 差异 G7）
#
#   ./scripts/test_gate.sh              # 快速门禁：确定性用例 + 覆盖率门槛（提交前/CI 阻断）
#   ./scripts/test_gate.sh --full       # 全量回归：与基线比对，禁止新增失败（CI 阻断）
#   ./scripts/test_gate.sh --ui         # 前端冒烟（HTTP 模式，无需浏览器；CI 另有浏览器任务）
#   ./scripts/test_gate.sh --all        # fast + full + ui 全跑（发布前）
#   ./scripts/test_gate.sh --update-baseline   # 重写 scripts/test_baseline.json
#
# 说明：
# - 用例集与基线是**显式**的（不静默跳过）：快速集只含当前 100% 通过的确定性模块；
#   全量模式允许存量失败（环境依赖），但失败/错误数不得高于基线、通过数不得低于基线。
# - 覆盖率门槛按 GitHub CI 干净 checkout 实测基线设为 15%（core+api，合计约 5.9 万行），只用于拦下降；
#   本地工作区包含被 .gitignore 排除的 AI 收费模块，覆盖率口径与 CI 不同，不能据此上调。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
COV_THRESHOLD="${COV_THRESHOLD:-15}"
ART_DIR="${ART_DIR:-$ROOT/artifacts}"
BASELINE="${BASELINE:-$ROOT/scripts/test_baseline.json}"
export CODEBUDDY_SAFE_DELETE_ENABLED=0

# 快速门禁用例集：全部为确定性、无外部数据库/SSH 依赖的模块（基线 128 用例）
GATE_MODULES=(
  tests/test_api_contract.py          # 接口契约（错误码/分页/版本/OpenAPI/命名）
  tests/test_agentless_audit.py       # 目标端免装取证判定 + capabilities 接口
  tests/test_ai_model.py
  tests/test_ai_alert.py
  tests/test_ai_alert_taskdetail.py
  tests/test_custom_backup.py
  tests/test_link_sources_contract.py
  tests/test_neo4j_registration.py
  tests/test_rt_scheduler.py
)

mkdir -p "$ART_DIR"
MODE="fast"
case "${1:-}" in
  --full) MODE="full" ;;
  --ui) MODE="ui" ;;
  --all) MODE="all" ;;
  --update-baseline) MODE="baseline" ;;
  "" ) ;;
  *) echo "未知参数: $1（见文件头用法）"; exit 2 ;;
esac

hr() { printf '%s\n' "------------------------------------------------------------"; }
fail() { echo "[门禁失败] $*"; exit 1; }

run_fast() {
  hr; echo "[门禁] 快速集：${#GATE_MODULES[@]} 个模块 + 覆盖率门槛 ${COV_THRESHOLD}%"
  "$PY" -m pytest "${GATE_MODULES[@]}" -q \
        --cov=core --cov=api \
        --cov-report=term-missing:skip-covered \
        --cov-report=xml:"$ART_DIR/coverage.xml" \
        --cov-fail-under="$COV_THRESHOLD" \
        --junitxml="$ART_DIR/junit-fast.xml"
  local rc=$?
  [ $rc -eq 0 ] || fail "快速集未通过（rc=$rc，报告 $ART_DIR/junit-fast.xml）"
  echo "[门禁] 快速集通过（覆盖率 ≥ ${COV_THRESHOLD}%）"
}

run_full() {
  hr; echo "[门禁] 全量回归：tests/（与 $BASELINE 比对）"
  "$PY" -m pytest tests -q --junitxml="$ART_DIR/junit-full.xml"
  local rc=$?
  "$PY" scripts/gate_baseline.py --junit "$ART_DIR/junit-full.xml" \
        --baseline "$BASELINE" --mode check \
    || fail "全量回归出现新增失败或通过数下降（详见上方差异行）"
  echo "[门禁] 全量回归通过（存量失败未增加；pytest rc=$rc）"
}

run_ui() {
  hr; echo "[门禁] 前端冒烟（HTTP 模式：登录 + 全部页面 + API 契约）"
  "$PY" scripts/frontend_smoke.py --mode http \
    || fail "前端 HTTP 冒烟未通过"
  echo "[门禁] 前端 HTTP 冒烟通过"
}

case "$MODE" in
  fast) run_fast ;;
  full) run_full ;;
  ui) run_ui ;;
  all) run_fast; run_full; run_ui ;;
  baseline)
    hr; echo "[门禁] 重写基线"
    "$PY" -m pytest tests -q --junitxml="$ART_DIR/junit-full.xml" >/dev/null 2>&1
    "$PY" scripts/gate_baseline.py --junit "$ART_DIR/junit-full.xml" \
          --baseline "$BASELINE" --mode update
    ;;
esac

hr; echo "[门禁] 全部通过（mode=$MODE）"
