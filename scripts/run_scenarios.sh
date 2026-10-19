#!/usr/bin/env bash
# Regenerates the replay evidence (success + error paths + human handoff). Requires local ParaBank,
# an artifact from `discover`, and CUA_PARAM_PASSWORD in the environment/.env.
set -u
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
CAP=${CAP:-get_account_details}
R="uv run python -m cua replay $CAP --allow-draft --param username=john"
SUMM='import sys,json; r=json.load(sys.stdin); f=r.get("failure"); print("  status=%s outputs=%s outcome=%s" % (r["status"], r.get("outputs"), r.get("outcome") and r["outcome"]["code"])); f and print("  failure:", {k: f[k] for k in ("step_id","category","expected","observed")}); print("  recoveries=%s escalations=%s" % ([x["condition"] for x in r["recoveries"]], r["escalations"]))'

run() { # name, extra-args...
  local n=$1; shift
  echo "== $n"; rm -rf "evidence/replay-$n"
  $R "$@" --out "evidence/replay-$n" 2>>/tmp/scen_err.txt | python3 -c "$SUMM"
}

run success            --param account_id=12456
run business-not-found --param account_id=99999
run invalid-input      --param account_id=abc
run recoverable-dialog --param account_id=13344 --inject dialog@s5
run session-unattended --param account_id=13344 --inject session@s5
run session-handoff    --param account_id=13344 --inject session@s5 --operator scripted
echo "== invalid-credentials (wrong password)"; rm -rf evidence/replay-invalid-credentials
CUA_PARAM_PASSWORD=wrong $R --param account_id=13344 --out evidence/replay-invalid-credentials 2>>/tmp/scen_err.txt | python3 -c "$SUMM"
