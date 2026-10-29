# MiniMax 提交说明（SDD 上下文管理系统）

## 对应验收项

| 必做项 | 实现位置 |
| --- | --- |
| 能够运行 | `ctxsdd/cli.py`（`python -m ctxsdd ...`），运行步骤见 `README.md` |
| MiniMax 实际参与 | `ctxsdd/minimax_client.py` + `docs/runs/minimax_dev_log.jsonl` + `docs/runs/code_diff.txt` |
| 上下文正确 | `ctxsdd/contextpack.py` 输出含规格/代码/测试来源；`docs/runs/context_pack.json` 为样例；`docs/runs/spec_update/` 为规格更新前后对照（更新后输出使用新内容） |
| 数据库实际参与 | `ctxsdd/obstore.py` 写入并检索工程资料；`docs/runs/ob_query_log.jsonl`、`docs/runs/ob_probe.md` 为查询记录 |
| 状态能够复用 | `ctxsdd/state.py` 通过 PowerContext 保存；`docs/runs/powercontext_state.json` 为写入记录，重启后读取复用 |
| 代码与测试 | `docs/snapshots/` 改前改后快照、`docs/runs/code_diff.txt` 差异、`business/tests/` 新增与回归测试、`docs/runs/test_run_log.jsonl` 运行日志 |

## 业务工程与验证样例

`business/reading_list/` 为业务工程（service / models / data / http_api），
验证样例为「增加筛选条件」的局部修改，明确「要改什么」与「哪些原有行为必须保持」，
对应测试 `test_filter_feature.py`（新增功能）与 `test_regression_baseline.py`（原有行为保持）。

## 本提交

仅补充本说明文件，不改动既有实现与测试，用于明确上述对应关系。
