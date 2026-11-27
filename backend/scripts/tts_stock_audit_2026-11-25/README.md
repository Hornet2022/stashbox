# 存量音色影响面核验证据（2026-11-25）

配套文档：`docs/音色与蒸馏质量问题_根因与修复_2026-11-25.md` §1.16、
`docs/TTS音色一致性契约_v1.md` §2.4.1。

## 结论（权威干净重算）

- 95 篇存量中 **92 篇可复核旧音频**，按 ECAPA 相邻滑窗余弦（6s 窗 / 2s 步）：
  **29 篇确证真换人（min ≤ 0.70）** / 29 不确定（0.70~0.82）/ 34 同一人（≥ 0.82）。
  另 3 篇 V0 旧文件已随首批重生成删除、孤儿目录无副本，不可独立复核。
- 确证换人按版本：V0:16 / V2:1 / V3:12 ⇒ 影响面**跨 V0 与 V3**，不只 V0。
- 首批 18 篇重生成（按版本选 15V0+3V1）只覆盖 29 篇确证换人中的 **7 篇**，还剩 **22 篇**。
- 首批验收：7 篇确证换人全 → 同一人；内容 0 缺陷。
  **但 2 篇原「同一人」回归换人**（`art_1b7999b8` 0.8767→0.2427、`art_1a259c1a` 0.8851→0.6662）。
- 机制：零样本克隆**逐请求采样**，18 篇全 2 块 1 接缝，~12% 接缝抽到不同人；
  块长 250→1500 把接缝 ~9→1，大幅降低但**非归零**。根治需固定说话人方案。

## 判据口径（同基准）

- 换人边界 `0.70` = 仓内 `app/services/tts/consistency.py` 的 `ECAPA_DIFF_MAX`（两测量单位通用）。
- 同一人边界 `0.82` = **滑窗单位**单独标定（同一人单条自比基准下界 0.8227）；
  **不是** `ECAPA_SAME_MIN=0.85`（那是「整块 vs 参考音」单位，对滑窗偏严会假阳性）。
- 标定素材与过程见 `/tmp/tts_verify/calib_sliding.py`（已知真值：同一人 / 真换人）。

## 文件

| 文件 | 含义 |
|---|---|
| `clean_impact.py` | 权威重算脚本（按文件 mtime 身份归位新旧，避开 DB 当前指向污染） |
| `clean_impact.json` | 重算结果：`old`/`new` 逐篇 min+判定+版本，`remaining_bad`=22 篇 id |
| `all_scan.jsonl` | 18:25 全量旧音频扫描（每文件一行：min/med/n_below_diff/凹陷位置） |
| `new_scan.jsonl` | 首批 18 篇新音频中 13 篇的补扫（另 5 篇早重生成已在 all_scan） |
| `articles.json` | 95 篇按流水线版本分类（V0~V3，依据 created_at 落入版本生效区间） |
| `new_urls.json` | 18 篇当前生产 DB `audio_url` 文件名（权威新文件身份） |
| `content_after.json` | 重生成后内容质量快照（仓内 `recreation_quality.check_group` 同判据） |
| `result.json` | 最早 2 篇前后对照原始 ECAPA 轨迹（art_1ed13969 / art_4197eec） |

## 复算

`clean_impact.py` 依赖 `/tmp/tts_verify/all/` 里的音频文件 mtime 来归位新旧。
音频可用 `rsync root@<prod>:/var/lib/stashbox/data/audio/ /tmp/tts_verify/all/` 重新拉取，
扫描用 `batch_ecapa.py`（ECAPA 离线，`HF_HUB_OFFLINE=1`）。
若只核对数字而不重跑：`clean_impact.json` 的 `old`/`new` 行可与 `all_scan.jsonl`/`new_scan.jsonl`
逐文件对账（`file` 字段一致）。

⚠️ 旧音频不可恢复的 3 篇（`art_6bf654c0` / `art_470217e3` / `art_fc555b29`）：
重生成会删除旧文件，孤儿目录 `/root/retired-certs/orphan-audio` 仅存其中 2 篇的 Oct-22 旧版
（`art_1ed13969.d77c3893` / `art_4197eec.70c5cad7`，已用于前后对照）。
**教训：重生成前应先备份旧音频 / 抓内容快照，否则前后对照与回滚都不可得。**