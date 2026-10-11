#!/usr/bin/env bash
# 逐个测试文件单独起 pytest —— 绕开宿主机的临时目录文件锁。
#
# 背景：这台 Windows 宿主机上，系统临时目录里的文件删除会**无限期阻塞**
# （os.unlink / os.rmdir / SQLite close 都中招，没有异常、直接卡住）。
# 一次性跑全量套件时，先跑完的文件留下的临时库会越积越多，
# 到某个点整轮就会卡死在 dispose() 上，跑不完也报不出结果。
#
# 每个文件单独起一个进程，进程退出即释放句柄，绕开这个限制。
# 代价是慢一些（每文件一次解释器启动），换来的是**能拿到完整结果**。
#
# 用法：bash scripts/run_tests_isolated.sh [每个文件的超时秒数]
set -u

TIMEOUT="${1:-240}"
PY=".venv/Scripts/python.exe"
OUT="test_results.txt"
: > "$OUT"

pass=0; fail=0; hang=0; total_p=0; total_f=0

for f in app/tests/test_*.py; do
  name="$(basename "$f")"
  line="$(timeout "$TIMEOUT" "$PY" -m pytest "$f" -q --no-header -p no:cacheprovider 2>&1 \
          | grep -E "passed|failed|error" | tail -1)"
  if [ -z "$line" ]; then
    echo "$name :: HANG/TIMEOUT" | tee -a "$OUT"
    hang=$((hang+1))
  else
    echo "$name :: $line" | tee -a "$OUT"
    p=$(echo "$line" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || echo 0)
    fl=$(echo "$line" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' || echo 0)
    total_p=$((total_p + ${p:-0}))
    total_f=$((total_f + ${fl:-0}))
    if [ "${fl:-0}" -gt 0 ]; then fail=$((fail+1)); else pass=$((pass+1)); fi
  fi
done

echo "=== ALL DONE ===" | tee -a "$OUT"
echo "" | tee -a "$OUT"
echo "文件: 全过 $pass / 有失败 $fail / 卡死 $hang" | tee -a "$OUT"
echo "用例: 通过 $total_p / 失败 $total_f" | tee -a "$OUT"
