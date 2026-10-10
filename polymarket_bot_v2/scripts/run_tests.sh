#!/usr/bin/env bash
# run_tests.sh — 分层测试脚本
# 用法:
#   ./scripts/run_tests.sh smoke          # 只跑关键路径 (< 10秒)
#   ./scripts/run_tests.sh module executor # 只跑 executor 模块测试
#   ./scripts/run_tests.sh module monitor  # 只跑 monitor 相关测试
#   ./scripts/run_tests.sh full            # 全量测试
#   ./scripts/run_tests.sh                 # 默认 = full
set -euo pipefail
cd "$(dirname "$0")/.."

LAYER="${1:-full}"
MODULE="${2:-}"

case "$LAYER" in
  smoke)
    echo "🔥 SMOKE: 关键路径测试 (executor + monitor_positions + db)"
    python3 -m pytest tests/test_order_executor.py tests/test_monitor_positions.py tests/test_data_integrity.py -v --tb=short -q
    ;;
  module)
    if [[ -z "$MODULE" ]]; then
      echo "❌ 用法: ./scripts/run_tests.sh module <名称>"
      echo "  可选: executor, scanner, signal, monitor, tracker, paper_trader, db, weather, positions"
      exit 1
    fi
    case "$MODULE" in
      executor)
        python3 -m pytest tests/test_order_executor.py -v --tb=short
        ;;
      scanner)
        python3 -m pytest tests/test_scanner.py -v --tb=short
        ;;
      signal)
        python3 -m pytest tests/test_signal_engine.py -v --tb=short
        ;;
      monitor)
        python3 -m pytest tests/test_monitor.py tests/test_monitor_positions.py -v --tb=short
        ;;
      tracker)
        python3 -m pytest tests/test_outcome_tracker.py tests/test_profit_taking.py tests/test_stop_loss.py -v --tb=short
        ;;
      paper_trader)
        python3 -m pytest tests/test_paper_trader.py -v --tb=short
        ;;
      db)
        python3 -m pytest tests/test_data_integrity.py tests/test_reconciliation.py -v --tb=short
        ;;
      weather)
        python3 -m pytest tests/test_weather_fetcher.py tests/test_weather_ensemble.py tests/test_weather_dedup.py -v --tb=short
        ;;
      positions)
        python3 -m pytest tests/test_monitor_positions.py tests/test_position_management.py tests/test_position_syncer.py -v --tb=short
        ;;
      *)
        # 尝试匹配 tests/test_<MODULE>.py
        FILE="tests/test_${MODULE}.py"
        if [[ -f "$FILE" ]]; then
          python3 -m pytest "$FILE" -v --tb=short
        else
          echo "❌ 未知模块: $MODULE"
          exit 1
        fi
        ;;
    esac
    ;;
  full)
    echo "🧪 FULL: 全量测试"
    python3 -m pytest tests/ -v --tb=short
    ;;
  *)
    echo "❌ 未知层级: $LAYER"
    echo "  用法: smoke | module <名称> | full"
    exit 1
    ;;
esac
