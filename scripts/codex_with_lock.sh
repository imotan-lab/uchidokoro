#!/usr/bin/env bash
# codex_with_lock.sh — 自動タスクから Codex を呼ぶときの入口。
#
# ★なぜ要るか（2026-07-30・Codex指摘3）★
#   ロックは最終heartbeatから30分で「異常終了の残骸」とみなされ、他のタスクに奪われる。
#   一方 Codex への1回の相談は最大30分、3往復なら最大90分かかる。
#   そのまま呼ぶと、
#     ①相談中に heartbeat が途切れる
#     ②別タスクがロックを奪う
#     ③戻ってきたタスクの check が失敗する
#     ④「エラー時はSTEPをスキップ」という規則に従って**そのまま書き込みを続ける**
#   という経路が通ってしまう。
#
# ★このスクリプトがやること★
#   1. Codex を裏で走らせる
#   2. その間 5分ごとに heartbeat を打つ（ロックを保ち続ける）
#   3. heartbeat が失敗したら **Codex を止めて即座に異常終了**（ロックを失ったまま進まない）
#
# 使い方:
#   bash scripts/codex_with_lock.sh <CTX> <prompt_file> <out_file> [timeout_sec] [attempts] [effort]
#
# 終了コード: 0=成功 / 1=Codex失敗 / 2=★ロックを失った（呼び出し側は即終了すること）★
set -u
CTX="${1:?CTX path required}"
PROMPT="${2:?prompt_file required}"
OUT="${3:?out_file required}"
TMO="${4:-900}"
ATT="${5:-2}"
EFF="${6:-high}"

REPO="C:/Users/imao_/Desktop/個人用/うちどころ"
REVIEW="C:/Users/imao_/Documents/uchidokoro/tools/codex_review.sh"
HEARTBEAT_EVERY=300      # 5分（ロックのstale判定30分に対して十分短く）

if [ ! -f "$CTX" ]; then
  echo "[codex_with_lock] ★CTXがありません: $CTX★ 書き込まずに終了してください" >&2
  exit 2
fi

bash "$REVIEW" "$PROMPT" "$OUT" "$TMO" "$ATT" "$EFF" &
CODEX_PID=$!

while kill -0 "$CODEX_PID" 2>/dev/null; do
  sleep "$HEARTBEAT_EVERY" &
  wait $! 2>/dev/null || true
  kill -0 "$CODEX_PID" 2>/dev/null || break
  if ! python "$REPO/scripts/task_lock.py" heartbeat --ctx "$CTX" >/dev/null 2>&1; then
    echo "[codex_with_lock] ★ロックを失いました → Codexを止めます★" >&2
    kill "$CODEX_PID" 2>/dev/null || true
    wait "$CODEX_PID" 2>/dev/null || true
    exit 2
  fi
done

wait "$CODEX_PID"
RC=$?

# 戻ってきた時点でもロックを持っているか確かめる（持っていなければ書き込ませない）
if ! python "$REPO/scripts/task_lock.py" check --ctx "$CTX" >/dev/null 2>&1; then
  echo "[codex_with_lock] ★戻った時点でロックを失っています★ 書き込まずに終了してください" >&2
  exit 2
fi
exit "$RC"
