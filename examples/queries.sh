#!/usr/bin/env bash
# 常见查询示例。设 A_GATEWAY_CONTACT 后 SEC 系源才可用。
set -euo pipefail
BASE="${A_GATEWAY_BASE:-http://127.0.0.1:8848}"

q() {  # q <source> <endpoint> <params-json>
  curl -s -X POST "$BASE/v1/query" -H 'Content-Type: application/json' \
    -d "{\"source\":\"$1\",\"endpoint\":\"$2\",\"params\":$3,\"limit\":3}" \
    | python3 -m json.tool
}

echo "== 1. 查一个公司在 SEC 的申报记录 =="
q sec_edgar submissions '{"cik":"0000320193"}'

echo "== 2. 查 Crossref 最近一天新索引的 DOI =="
q crossref works '{"filter":"from-index-date:2026-10-21"}'

echo "== 3. 查临床试验近一周更新 =="
q clinicaltrials_gov_v2 studies '{"term":"AREA[LastUpdatePostDate]RANGE[2026-10-15,MAX]"}'

echo "== 4. 查一家公司的 LEI(全球法人 ID) =="
q gleif_lei lei_records '{"size":3}'

echo "== 5. 查某天新增的欧盟统计表 =="
q eurostat toc '{}'

echo "== 6. 先看端点怎么调(不需要试错) =="
curl -s "$BASE/v1/sources/crossref/endpoints/works" | python3 -m json.tool
