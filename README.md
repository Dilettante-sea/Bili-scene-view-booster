# Bili scene-view-booster（astrbot_plugin_whale_sense）

> 让 AstrBot 里的助手有“自己的 B 站时间”：到点自己刷一批 B 站视频，先看文案初筛，再让 DSH 只读地读字幕/抽帧，最后用自己的模型判断值不值得聊。
> 
> 这是一个 **薄插件**：插件只负责时钟、候选、配额、must 队列、审计、日记、报告和唤醒；判断、DSH 派单、记忆写入都走助手已有的工具。
> 
> 适合什么场景：将astrbot视为bot唯一大脑，没有让bot在B站@用户等互动需求，仅在自建matrix上进行非群聊的私人对话，且有长期记忆模型LivingMemerry（源astrbot插件市场）配置。发布者自身使用astrbot+tailscale+element x+dsh。

- 插件版本：0.1.0
- 验证环境：AstrBot 4.29.3、DSH Desktop、Matrix、**LivingMemory**、dsh-astrbot-gateway
- 支持平台（metadata）：`matrix`
- 当前本机实际使用：Matrix 两个会话（work / admin）
- 相关仓库：
  - 看视频的 DSH 插件：[dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard)
  - AstrBot 侧连桥：[astrbot_plugin_dsh_gateway](https://github.com/qzy033/astrbot_plugin_dsh_gateway)
  - DSH 侧依赖包：`dsh-astrbot-gateway`

---

## 0. 当前状态（2026-11-12）

- **已跑通**：时钟→feed→work 初筛→`dsh_dispatch`/`whale_track_deep`→DSH 结果→终评→LivingMemory→must→分类/检索→日记/审计/报告。
- **管理员主动视频**：`whale_add_must` → admin must → DSH → 反馈 → 分类的 add/track/judge 单测已过；真实 BVID 跑完整 DSH已通过。
- **标签**：Admin Like / Assistant Taste / Block / Audit 页面可用；小样本 Normal-Thompson + 4+1 搜索词选择；Admin Like 支持删除并静音同名 taste（删除为两击确认）。
- **等待数据**：还没有足够多的真实 `must/archived/discarded` 反馈来评估权重收敛和探索效果；正在 shadow 观察状态。
- **网关补丁**：外部插件 `astrbot_plugin_dsh_gateway` 的 `dsh_status` 长结果截断已在本机修掉（三处改动、备份、升级后怎么恢复，见 4.3）。面板更新会覆盖，属已知项。

---

## 1. 它能做什么

### 1.1 自主刷 B 站

- 按配置的作息窗口随机醒来（默认 20:00–24:00，2 次，带跳过概率和最小间隔）。
- 调 `bili-sense feed` 抓一批候选（默认 5 条），带标题、UP、分区、官方 tag、简介、时长、播放/点赞/投币/收藏等。
- 支持标签偏好和屏蔽标签：
  - `Admin Like`：只在 `tags` 页面管理；首次安装会 seed 一组默认 tag。
  - `Block`：只在 `tags` 页面管理；标题/简介/tag/分区命中就硬屏蔽，不进入候选。
- 支持最短时长过滤（默认 180 秒，滤掉三分钟以下短视频）。
- 候选和批次落 SQLite，供审计和 `whale why` 查询。

### 1.2 初筛、深读、终评

- 在 work 工作会话里让助手只凭文案（标题、简介等简略信息，非通义文案）做初筛：`no / maybe / deep`。
- 对 `deep` 的候选，插件生成严格的 DSH 只读任务模板；助手用现有 `dsh_dispatch` 派单。
- 插件登记 `task_id`，轮询 DSH outbox，拿回：
  - 完整转录（官方字幕/ASR/OCR）；
  - 关键帧绝对路径 + 时间码；
  - `scene_notes` / `has_subtitle` / `errors`。
- 结果回来后，插件再次唤醒 work 会话，让助手看转录和关键帧，做终评：
  - `must / maybe / no`；
  - 先调 `memorize_long_term_memory`，再调 `whale_judge_result`。

### 1.3 must 主/副队列

- `tier=must` 进入主队列（`primary`）。
- 一条 must 的完整状态机：

```text
pending -> pushing -> pushed
                       ├─ archived_done               聊透，归档
                       ├─ discarded_not_interested    不感兴趣，丢弃；记忆保留
                       └─ later_shallow               没聊透
                             ├─ attempts < 3 -> secondary/pending
                             └─ attempts >= 3 -> archived_shallow（出队，不再主动推）
```

- 全局同时只允许一条 `pushing`/`pushed`；`admin_pending` / `admin_reading` / `admin_discussing` 也会阻塞自动推送。
- 推送条件：距助手在 admin 的最后一条消息至少 60 分钟（可配），且不在安静时段。
- 插件会把终评时助手写好的 `push_text` 直接发到 admin；旧数据没有 `push_text` 时才回退到唤醒 admin 会话。
- 用户说“下一条/继续”时，助手调 `whale_push_next`，跳过 1 小时限制。
- `archived_shallow` 不再主动唤醒，但可以用 `whale_recall_undiscussed` 或 `/whale recall` 按关键词检索。

### 1.4 记忆、日记、审计、报告

- 助手调 LivingMemory 的 `memorize_long_term_memory`，插件只记录 `memory_id`。
- 本地 Markdown 日记：`must` 表格 + 详细段落；`maybe` 简略；deep-no 负评。
- SQLite `events` 表 + `whale-audit.jsonl` 双写审计。
- `/whale report [date]` 生成确定性审计摘要；日报默认 23:30，周报默认周日 21:00，直接发到 admin。
- 管理员可以查看状态、队列、must 详情、why 链路、deep 任务，删除单条记忆。

### 1.5 管理命令

```text
/whale status                 运行状态、UMO、CLI、node、gateway cache、时钟、配额
/whale ping work|admin        测试两个会话是否可达
/whale pause | resume         暂停/恢复时钟（已入队 must 保留）
/whale clock                  查看时钟计划
/whale run [dry]              立刻抓一批并唤醒 work；dry 不唤醒
/whale pending                查看 must 主/副队列
/whale tags                   查看 Admin Like / 助手 taste / 屏蔽权重
/whale must done <id>         标记聊透
/whale must drop <id>         标记不感兴趣
/whale must later <id>        标记没聊透，进副队列
/whale must next              立即推下一条
/whale recall <词>            检索已出队但没聊透的内容
/whale report [date]          日报/审计摘要
/whale show <must_id|memory_id> 查看 must 详情
/whale forget <memory_id>     删除一条 LivingMemory 记忆（写审计）
/whale why <bvid>             初筛/深读/终评链路
/whale deep [n]               最近 deep 任务
/whale help                   帮助
```

> 所有命令都是管理员权限；普通用户不能操作队列，也不能删除记忆。

### 1.6 管理员主动发视频

管理员在私聊里直接发 B 站链接/BV 号，助手会调用 `whale_add_must`：

1. 解析 BVID，调 `bili-sense meta` 取标题/tag/统计；
2. 写 `candidates`（source=admin）和 `musts`：
   - `source=admin`
   - `queue=admin`
   - `state=admin_pending`
   - 不增加 `deep_used` / `must_produced` 配额；
3. 返回 DSH 只读任务模板和 `must_id`；
4. 助手在 **admin 会话** 调 `dsh_dispatch`，再调 `whale_track_deep(..., must_id)`；
5. DSH 结果回到 admin，助手终评并给出反馈；
6. must 状态变 `admin_discussing`，按正常逻辑分类：
   - `archived_done`
   - `discarded_not_interested`
   - `later_shallow`

管理员主动 must 会**阻塞自动 must 推送**，避免正聊着突然冒出新话题；但它不占 deep/must 配额，并正常参与 taste 学习与审计。

---

## 2. 它不能做什么

### 2.1 不以独立用户身份进入 B 站客户端

- 不评论、不私信、不点赞、不投币、不收藏、不关注、不分享。
- 不写 B 站任何数据；所有 B 站请求都是只读。
- 不做登录态维护；cookie 失效时回退匿名搜索（排行榜可能被跳过）。

### 2.2 不自己看视频

- 插件不下载视频，不跑 ffmpeg，不做 ASR/OCR/抽帧。
- 这些全部由 DSH 侧的现有视频工具完成；插件只接收 DSH 返回的 JSON 素材。
- DSH 不参与判断、不打标签、不选片、不写记忆。

### 2.3 不自己调用模型/记忆

- 插件不调 `llm_generate`，不调 `persona_manager`，不直接调 LivingMemory 的 `memory_engine`。
- 判断发生在助手自己的回合；记忆写入由助手调 `memorize_long_term_memory` 完成。
- `/whale forget` 是管理员显式操作，才会通过 LivingMemory 删除单条记忆。

### 2.4 前端

- 配置页由 `_conf_schema.json` 自动渲染。
- 标签权重/审计有单独页面：`views/tags/index.html`，从插件详情页打开。
- 页面分 `Admin Like` / `Assistant Taste` / `Block` / `Audit` 四个 tab；管理员可编辑权重、`locked`、`pinned`、屏蔽和查看审计。
- Admin Like 支持删除；删除时会同时把同名 assistant taste 静音，避免它从 taste-only 路径被重新搜出来。
- 删除采用**两击确认**（第一次按钮变“确认删除”，第二次才执行）；AstrBot 的 iframe sandbox 不保证 `window.confirm` 可用。
- 没有独立前端工程；页面是单文件 HTML + 原生 JS。

### 2.5 配额不是硬闸门

- `deep_per_day`、`deep_per_wake`、`must_per_day`、`must_backlog_per_day`、`maybe_per_day` 目前主要是：
  - 写进 prompt 提示助手；
  - 记录到 `quota` 表并在状态/报告里展示。
- 真正硬约束的只有：
  - 同时最多一条 `pushing`/`pushed`，以及 `admin_pending`/`admin_reading`/`admin_discussing` 阻塞自动推送；
  - `max_shallow_attempts` 达到后出队；
  - 最短时长过滤和 Block 硬屏蔽。
- 管理员主动 must 不增加 deep/must 配额。
- 如果某天配额超了，插件不会丢 must；它会继续入队，只是提示/统计会显示超额。

### 2.6 `maybe` 目前不主动推送

- `maybe` 会写日记、进报告，但当前实现没有单独的“maybe 主动分享”循环。
- 只有 `must` 会进入主/副队列和主动推送。

### 2.7 标签是“小样本权重”，不是大模型推荐系统

当前标签行为是：

- **Admin Like**：管理员底座，`base_weight` 0–5，可 `locked`（助手不可改）或 `pinned`（每轮必搜，最多 1 个）。
- **Assistant Taste**：助手自己的增量，来自终评/分类奖励，也可由助手用 `whale_taste_update` 显式调整（单次 ≤0.5）。
- **Block**：硬屏蔽，命中直接不检索；只有管理员能生效，助手不能直接 block。
- 搜索词选择：每个 tag 一个 Normal-Thompson 小模型，选 **4 个 exploit + 1 个 explore**；不是固定 Top-K。
- 有效权重：`effective = clamp(admin_base + assistant_delta, -5, 5)`；`locked` 时只取 base。
- 标签匹配：官方 tag / 分区 / 标题 / 简介，中文连续子串，不做两字 bigram；英文用词边界。
- 学习奖励：`archived_done +1.0`、`must +0.3`、`maybe +0.1`、deep `no -0.5`、`discarded -0.6`；`screen` 阶段不更新。
- 时间衰减：每周 `n_eff` / `sum_reward` × 0.98。
- 当前**不做**：LinUCB、RAG 向量特征、embedding 奖励迁移、LLM 自评布尔奖励、自动 block。

所以它是“小样本、可审计的偏好策略”，不是完整推荐系统。

### 2.8 权重算法与示例

- **Admin base**：`base_weight`，0–5；管理员在 tags 页面修改。
- **Assistant taste**：`assistant_delta`，-5–5；由终评/分类奖励更新，也可由助手 `whale_taste_update` 手调（单次 ≤0.5）。
- **Effective**：

```text
effective = clamp(admin_base + assistant_delta, -5, 5)
```

`locked` 时 `effective = admin_base`；`pinned` 只保证进入搜索词，不改变权重。

#### 后验

每个 tag 一个 Normal 后验：

```text
先验：μ ~ N(0.2, 1)
观测：r ~ N(μ, 0.5²)

precision = 1 + n_eff / 0.25
mean = (1 * 0.2 + sum_reward / 0.25) / precision
confidence = 1 - 1 / precision
sample ~ Normal(mean + manual_delta, 1 / precision)
```

`n_eff` / `sum_reward` 每周乘以 0.98。

#### 奖励归属

一条视频可能命中多个 tag：

- 被选为搜索词的 query tag 拿 50%；
- 其他命中的官方 tag 平分另外 50%；
- 没命中的 tag 不更新。

#### 例子

```text
历史：admin_base = 2.0
知识分享：admin_base = 0（只在 taste 里）
奖励：archived_done = +1.0
source_query = 历史
官方 tags = [历史, 知识分享]

历史分到 +0.5
知识分享分到 +0.5
```

历史第一次奖励后：

```text
n_eff = 0.5
sum_reward = 0.5
precision = 1 + 0.5 / 0.25 = 3
mean = (0.2 + 0.5 / 0.25) / 3 = 0.733
confidence = 1 - 1/3 = 0.667
effective ≈ 2.0 + 0.733 = 2.733
```

第二次 `archived_done` 后：

```text
n_eff = 1.0
sum_reward = 1.0
precision = 5
mean = (0.2 + 1.0 / 0.25) / 5 = 0.84
confidence = 0.8
effective ≈ 2.84
```

一次 `discarded_not_interested`（-0.6，历史分到 -0.3）后：

```text
n_eff = 1.5
sum_reward = 0.7
precision = 7
mean = (0.2 + 0.7 / 0.25) / 7 = 0.4286
confidence = 0.857
effective ≈ 2.43
```

它只会降权，不会自动 block。

#### 搜索词选择

- `pinned` 最多 1 个，先放进搜索词；
- 其余按 Thompson 采样值取 4 个 exploit；
- 再选 1 个 explore：`n_eff` 最小 / 不确定性最大 / 最近最少被选。

### 2.9 不做多用户/多人格隔离

- 当前一套 `work_umo` / `admin_umo` / 一个偏好配置。
- 不是多租户系统；不同管理员/不同人格需要各自部署或后续扩展。

### 2.10 依赖 AstrBot 在线

- AstrBot 停 → 没有时钟、没有唤醒、没有 DSH 任务、没有 must 推送。
- 插件不是独立守护进程；它只是 AstrBot 的一个 Star 插件。

---

## 3. 依赖

### 3.1 AstrBot

- AstrBot `>= 4.24.2`（metadata 声明；本机验证 4.29.3）。
- 需要可用的 `context.cron_manager`（AstrBot 内置 active job），用于唤醒 work/admin。
- 需要平台适配器：
  - 当前用 Matrix adapter；
  - 插件本身不绑定 Matrix，只要 `work_umo`/`admin_umo` 能作为会话发消息即可；
  - 如果 AstrBot 开了 `enable_id_white_list`，两个 UMO 必须加入 `id_whitelist`，否则事件会被直接拦掉。

### 3.2 必需的 AstrBot 插件

| 插件                                                                                   | 作用                                                                | 是否必需                          |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------------- | ----------------------------- |
| [`astrbot_plugin_dsh_gateway`](https://github.com/qzy033/astrbot_plugin_dsh_gateway) | 提供 `dsh_dispatch` / `dsh_status`，写 `cache/inbox`，读 `cache/outbox` | 深读必需                          |
| `astrbot_plugin_livingmemory`                                                        | 提供 `memorize_long_term_memory`，存 must 的 `memory_id`               | 写记忆必需；不装也能跑，但 `memory_id` 会缺失 |
| 平台适配器（如 `astrbot_plugin_matrix_adapter`）                                             | 把 work/admin UMO 变成真实会话                                           | 当前 Matrix 部署必需                |

### 3.3 必需的 DSH 侧组件

| 组件                                                                          | 作用                                        |
| --------------------------------------------------------------------------- | ----------------------------------------- |
| DSH Desktop / DSH 运行时                                                       | 真正执行视频任务的 agent 运行时                       |
| `dsh-astrbot-gateway`                                                       | DSH 侧依赖包，AstrBot 与 DSH 的文件/任务桥；本机验证 0.6.1 |
| [`dsh-video-storyboard`](https://github.com/SAOS-3624/dsh-video-storyboard) | 看视频的 DSH 插件：字幕/ASR/OCR/关键帧；本机验证 0.6.3     |
| ffmpeg / ffprobe / yt-dlp                                                   | 视频探测、下载、抽帧                                |
| ASR 服务（可选）                                                                  | 没有官方字幕时的语音转录；未启动时只能靠硬字幕 OCR/关键帧           |

### 3.4 Node.js 与 bili-sense CLI

- Node.js 18+（需要全局 `fetch`）。
- `bili-sense` CLI：`bili-sense.mjs` + `lib/`，只依赖 Node 标准库，不需要 npm install。
- 配置 `feed_cli` 指向 `bili-sense.mjs`；`node_path` 留空时插件会尝试 DSH 运行时、AstrBot 自带 node、PATH。
- CLI 需要能访问 B 站 API；匿名可用，登录后搜索/排行更稳。

### 3.5 B 站 cookie（可选）

- 插件只使用两个可视化字段：`bili_sessdata` + `bili_jct`。
- 如果这两个字段留空，CLI 独立运行时会回退到 `~/.bili-ai/credentials.json`，
  或 AstrBot `astrbot_plugin_biliread_config.json` 的 `bilibili_cookie`。
- 匿名模式仍可搜索/热门；排行榜需要登录，否则跳过以避免 `-352`。
- `feed.login` 是真实 `nav.data.isLogin`，不是“有 SESSDATA 就算登录”。

---

## 4. 通信关系

```text
┌────────────────────────── AstrBot（唯一主脑） ──────────────────────────┐
│                                                                        │
│  astrbot_plugin_whale_sense                                            │
│   ├─ clock        到点 → 调 bili-sense feed                             │
│   ├─ feed.py      subprocess → bili-sense.mjs → JSON                    │
│   ├─ wake.py      cron_manager.add_active_job → work/admin 会话         │
│   ├─ deep_watch   轮询 gateway cache/outbox → 解析 JSON → 唤醒 work/admin│
│   ├─ must_loop    到点 → context.send_message → admin                   │
│   ├─ diary        data/diary/YYYY-MM.md                                 │
│   ├─ report       确定性文本 → context.send_message → admin             │
│   └─ SQLite       whale.db + whale-audit.jsonl                          │
│                                                                        │
│  助手（work 会话）                                                      │
│   ├─ whale_screen_result        ← 插件返回 deep 派单模板                 │
│   ├─ dsh_dispatch               → gateway → DSH                         │
│   ├─ whale_track_deep           → 插件登记 task_id                       │
│   ├─ memorize_long_term_memory  → LivingMemory                          │
│   ├─ whale_taste_update         → 只改助手 taste / 写审计                 │
│   └─ whale_judge_result         → 插件入 must / 写日记 / 写审计          │
│                                                                        │
│  助手（admin 会话）                                                     │
│   ├─ 直接收到 must 推送 / 报告                                          │
│   ├─ whale_add_must             → 管理员主动视频入队（source=admin）      │
│   ├─ dsh_dispatch / whale_track_deep → admin must 的只读深读              │
│   ├─ whale_judge_result         → 更新 admin must / 写记忆/日记/审计      │
│   ├─ whale_classify_must        → 归档/丢弃/副队列                       │
│   ├─ whale_push_next            → 立即下一条                            │
│   └─ whale_recall_undiscussed   → 检索出队内容                          │
└────────────────────────────────────────────────────────────────────────┘
             │ 只读任务（一次性）
             ▼
   DSH + 视频工具（字幕/ASR/OCR/抽帧）
   只返回素材，不判断、不写记忆、不评论
```

### 4.1 关键边界

- 插件**不直接**调 `dsh_dispatch`；自动模式由助手在 work 会话调，管理员主动视频由助手在 admin 会话调；插件只记 `task_id`、轮询 outbox。
- 插件**不直接**调 `memory_engine`；助手调记忆工具，插件只记 `memory_id`。
- 插件**不直接**调 LLM；判断发生在助手回合。
- DSH 不常驻、不自己醒、不写记忆。
- must 的直接发送用 `context.send_message`，不依赖唤醒 admin 会话；旧路径才用 active job 唤醒。

### 4.2 文件协议

- DSH 任务输入：`<gateway_cache>/inbox/<task_id>.json`（由 gateway 的 `dsh_dispatch` 写）。
- DSH 结果：`<gateway_cache>/outbox/<task_id>.json`，也可能被 `dsh_status` 自动归档到 `<gateway_cache>/outbox/done/<task_id>.json`；插件两处都读。
- 关键帧：DSH 返回绝对路径；插件把最多 6 张放进 cron payload 的 `images`，由 gateway 的图片注入补丁送进助手回合（没有补丁则退化为纯文本）。
- 本插件**直接读 outbox 文件**，不走 `dsh_status` 的文本出口，所以网关那侧的截断改动对本插件无影响（见 4.3）。

### 4.3 网关插件的本机改动（外部插件，升级会被覆盖）

`astrbot_plugin_dsh_gateway` 是外部仓库（[qzy033/astrbot_plugin_dsh_gateway](https://github.com/qzy033/astrbot_plugin_dsh_gateway)，本机装 0.6.1）。
开源工程落到别人的机器上，结果体积、面板渲染、通道截断长度都得按现场调，一装就能用是运气不是常态 ——
所以本机对它动过代码，**改动记在我们这边**。

**改在哪**：live 目录 `…\core\data\plugins\astrbot_plugin_dsh_gateway\dsh_tools.py`，备份 `dsh_tools.py.bak-20261112`
（18929 字节 = 上游原版）。工作区 `out\astrbot-src\dsh_gateway\` 另有一份同一问题的独立实现，只作参考、**没有部署**。

**为什么要改**：`dsh_status` 原来在 `len(body) > 6000` 处直接切一半加「…（内容过长，已截断）」。
实测一份 10544 字符的结果正好断在转录中间，闸门拿到半份 JSON —— 比截断更坏的是它没说「我断了」。

**三处改动**：

| #   | 改动                                                                                                              | 要点                                                                                      |
| --- | --------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| 1   | 抽出常量 `INLINE_BODY_LIMIT = 6000`                                                                                 | 阈值收在一处；注释写明「数字不是重点，重点是任何长度都不再假装完整」                                                      |
| 2   | 不超限原样返回（与上游一致）；超限**不回正文**，改回「摘要 + 字段清单 + 落盘绝对路径 + chars/bytes」，末尾打 `[FULL-FILE]`                                | 旧分支替换掉了 `body[:6000] + "…已截断"`                                                          |
| 3   | 新增 `_result_path()`（依次查 `outbox/` 与 `outbox/done/`）、`_field_stats()`（`raw_decode` 只吃第一个完整 JSON 对象，吃不到退 200 字预览） | 正文是「JSON 块 + 后面一段纯文本路径清单」，整段 `json.loads` 必失败；归档用 `os.replace` 搬进 `done/`，只查根目录会报不存在的位置 |

自测：`py_compile` 通过；真实结果离线跑 10544 → **569 字符**；纯文本回退分支单独测过。

**live 与工作区参考实现的差异**（排查时对不上先看这张表）：

| 维度   | live（在跑）              | 工作区参考实现（未部署）                 |
| ---- | --------------------- | ---------------------------- |
| 阈值   | 6000 字符，只看字符          | 8000 字符 **且** 15000 字节       |
| 超限标记 | `[FULL-FILE]`（指向落盘文件） | `——END——`（证明内联正文没被路上切断）      |
| 字段清单 | 不列图片                  | 额外逐行列出全部图片绝对路径               |
| 唤醒来话 | 上游原样                  | 多一行「结果文件：N 字符 / M 字节 + 绝对路径」 |
| 验证   | py_compile + 真实数据     | 26 项断言                       |

**两个盲区**（不影响「超限必给路径」这条主承诺）：6000 只按字符算，6000 汉字 = 18000 字节，
真撞字节上限时 `[FULL-FILE]` 还在、容易被当成「读完了」；清单里不带图片路径，
图仍由 gateway 的 `_collect_wake_images` 扫 outbox 自动发，但少了「闸门自己拿路径调 `dsh_send_image`」的兜底。

**升级后怎么恢复**：

1. 面板「更新」会用商店 0.6.1 覆盖 live，三处一起丢（`.bak` 也可能被清）—— 先把改过的 `dsh_tools.py` 另存一份到工作区；
2. 恢复＝把改过的 `dsh_tools.py` 拷回覆盖，重载插件；没副本就按上表重打三处；
3. 确认生效：`GET /api/plug/astrbot_plugin_dsh_gateway/ping` 返回 200；再让闸门对历史 task_id 调一次 `dsh_status`，
   看到 `[FULL-FILE]` 和 `outbox/done/…json` 即新逻辑，还看到「…（内容过长，已截断）」就是被覆盖回去了。

**两条配套事实**：`outbox/done/` 不设 TTL（全插件只有 `os.replace` 搬家、没有删除路径），
所以「拿到路径时文件还在」成立，现场最老的一件已躺 9 天；`dsh_archive` 只搬 `cache/dsh_relay/`，不碰 `outbox/`。

---

## 5. 安装与配置

### 5.1 安装插件

1. 把 `astrbot_plugin_whale_sense/` 放到 AstrBot 的 `data/plugins/` 下，或通过 WebUI 上传 zip。
2. 确认 `metadata.yaml`、`_conf_schema.json`、`main.py`、`core/` 都在。
3. 在 AstrBot 中启用插件。
4. 打开插件配置页，先填两个 UMO。

### 5.2 两个 UMO

- `work_umo`：自动模式的内部工作会话。初筛、派 DSH、终评、写记忆都在这里，不打扰管理员。
- `admin_umo`：管理员私聊。must 推送、日报、报告走这里；管理员主动发视频时也在这里读和反馈。

注意：

- `work_umo` 的实际类型可能是 `OtherMessage`，按原样填，不要强行改成 `FriendMessage`。
- 如果 AstrBot 开了 `enable_id_white_list`，两个 UMO 都要加入 `id_whitelist`，否则事件会被拦掉。
- 如果 AstrBot 的 Matrix E2EE 是关闭的，work room 也必须是未加密房间；否则 bot 收不到/发不出。

### 5.3 bili-sense

```bash
node /path/to/bili-sense/bili-sense.mjs doctor --json
node /path/to/bili-sense/bili-sense.mjs feed --json --n 5 --min-duration 180
```

- `doctor` 返回 `login` / `wbi` / `search` / `api.banned`。
- 插件配置 `feed_cli` 指向 `bili-sense.mjs`。
- `node_path` 留空会自动找；找不到时填 `node.exe` 绝对路径。

### 5.4 其他插件

- 装好并启用 `astrbot_plugin_dsh_gateway`。
- 装好并启用 `astrbot_plugin_livingmemory`，确认 `memorize_long_term_memory` 可用。
- DSH 侧装好视频工具，确认 `dsh_dispatch` 能跑通一条视频。
- 配置 `gateway_cache` 指向 gateway 的 cache 目录。

### 5.5 关键配置

| 字段                                      | 默认                | 说明                    |
| --------------------------------------- | ----------------- | --------------------- |
| `enabled`                               | true              | 总开关                   |
| `work_umo` / `admin_umo`                | 空                 | 两个会话                  |
| `feed_cli`                              | 空                 | bili-sense.mjs 路径     |
| `node_path`                             | 空                 | node.exe；留空自动找        |
| `feed_n`                                | 5                 | 每批候选数                 |
| `min_duration_sec`                      | 180               | 最短时长，滤掉三分钟以下          |
| `max_duration_sec`                      | 1800              | 最长时长（30 分钟），超过不进候选；0 = 不限 |
| `bili_sessdata` / `bili_jct`            | 空                 | 分开填写的 B 站 cookie      |
| `gateway_cache`                         | 空                 | gateway cache 目录      |
| `dsh_timeout_sec`                       | 600               | DSH 深读超时              |
| `clock_windows`                         | 20:00-24:00       | 作息窗口                  |
| `clock_wakes`                           | 2                 | 每窗口最多醒几次              |
| `skip_chance`                           | 0.3               | 本次不想看的概率              |
| `min_gap_min`                           | 10                | 两次唤醒最小间隔              |
| `quiet_hours`                           | 00:00-08:00       | 不推 must               |
| `deep_per_day` / `deep_per_wake`        | 5 / 3             | deep 配额（提示+统计）        |
| `must_per_day` / `must_backlog_per_day` | 2 / 1             | must 产出配额（提示+统计）      |
| `idle_minutes`                          | 60                | 距助手最后一条消息多久才推 must    |
| `max_shallow_attempts`                  | 3                 | 没聊透最多推几次              |
| `diary_dir`                             | 空                 | 日记目录；空则 `data/diary/` |
| `report_daily_at` / `report_weekly_at`  | 23:30 / Sun 21:00 | 报告时间                  |
| `dry_run`                               | false             | 只审计/日记，不唤醒/不派单/不发送    |

> 标签不再作为插件配置字段。Admin Like / Assistant Taste / Block 只在 `tags` 页面和 SQLite 中管理；页面保存后下一轮 feed 生效。

### 5.6 启动自检

1. `/whale status`：两个 UMO、CLI、node、gateway cache、时钟计划都对。
2. `/whale ping work`、`/whale ping admin`：两条会话都能发消息。
3. `/whale run dry`：只抓候选、不唤醒；检查候选数量和时长。
4. `/whale run`：真跑一轮；在 work 会话看初筛/DSH/终评。
5. 在 admin 私聊发一个 BVID，确认助手调用 `whale_add_must`，并能在 `/whale pending` 看到 `source=admin`。
6. `/whale pending`、`/whale report`：检查队列和审计。

---

## 6. 数据与审计

插件数据目录（AstrBot `data/plugin_data/astrbot_plugin_whale_sense/`）：

```text
whale.db                 SQLite：kv / candidates / deep_tasks / musts / quota / events /
                         admin_tags / taste_tags / block_tags / tag_events / tag_audit
whale-audit.jsonl        追加式审计：wake / batch / screen / deep_track / judge /
                         must_produced / must_pushed / must_classified / clock_fire /
                         admin_must_added / admin_must_judged / tag_select / tag_reward ...
diary/YYYY-MM.md         本地日记
```

- SQLite 是主数据源；JSONL 用于外部审计/排查。
- 标签学习事件在 `tag_events`；人工修改/删除在 `tag_audit`；页面 Audit tab 展示 `tag_audit`。
- 审计文件目前只追加，不自动轮转；长期运行需要自己归档。
- 报告以 admin 消息发送；当前不额外写 `reports/` 文件。

---

## 7. 故障排查

### 7.1 feed 没候选

- `feed_cli` 路径不对，或 `node_path` 不对。
- `node bili-sense.mjs doctor --json` 看 `wbi` / `search` / `banned`。
- B 站风控 `-412`：等熔断结束，不要连续重试。
- 所有候选都被 `min_duration_sec` 或 Block 过滤；`/whale run dry` 看日志里的过滤前后数量。

### 7.2 cookie 显示未登录

- `doctor` 的 `login` 是真实 `isLogin`，不是“有 SESSDATA 就算登录”。
- 用 `bili_sessdata` / `bili_jct` 分开填在两个独立字段里。
- `-101 账号未登录` = SESSDATA 失效或复制不完整；重新从浏览器/插件复制。
- 匿名模式仍可搜索；排行榜会被跳过。

### 7.3 DSH 没结果

- 看 `<gateway_cache>/outbox/<task_id>.json` 和 `outbox/done/`。
- 用 `dsh_status(task_id=...)` 看 gateway 队列。
- `dsh_timeout_sec` 默认 600；视频下载/OCR/抽帧可能超过 5 分钟。
- 没有字幕且 ASR 服务没启动时，DSH 只能靠硬字幕 OCR/关键帧，可能返回空转录。

### 7.4 must 不推

- `/whale pending` 看是否已有 `pushing`/`pushed`，或 `admin_pending`/`admin_reading`/`admin_discussing`；它们都会阻塞自动推送。
- 管理员主动 must 未分类时，自动 must 不会推新话题。
- 距助手在 admin 的最后一条消息是否满 `idle_minutes`。
- 是否在 `quiet_hours`。
- `push_text` 为空时会回退唤醒 admin；检查 cron/会话是否被其他任务占用。
- 10 分钟没发出会 `pushing -> pending`，下一次重试。

### 7.5 配置页不显示新字段

- 改 `_conf_schema.json` 后要同步到线上插件目录并重载插件。
- 标签字段已经不在配置页；Admin Like / Assistant Taste / Block 只在 `tags` 页面。
- 浏览器硬刷新（Ctrl+F5）或重新打开插件详情页。
- 如果仍不显示，检查 JSON 是否合法；schema 解析失败时插件可能仍能启动，但配置页不会更新。

### 7.6 tags 页面空白/不显示 Admin Like

- 先硬刷新（Ctrl+F5），再重新打开插件详情页。
- 页面使用 AstrBot 的 `AstrBotPluginPage` bridge，API 前缀是 `page/`；如果 bridge 未就绪，页面会显示错误。
- 检查浏览器控制台；如果提示 bridge 不可用，确认 AstrBot 版本支持插件 View（本机 4.29.3）。
- 后端接口：

```text
/api/v1/plugins/extensions/astrbot_plugin_whale_sense/page/tags
```

- 如果接口正常但页面空，查看页面控制台里的 `page/tags` 请求错误。
- Admin Like 是 `admin_tags` 表；如果表为空，`ensure_seed` 会重新 seed 默认 tag。

---

## 8. 设计原则

- **AstrBot 是唯一主脑**：判断、DSH 派单、记忆写入都在助手回合里。
- **插件是薄层**：只做时钟、候选、队列、审计、日记、报告、唤醒。
- **DSH 只读**：不判断、不打标签、不写记忆。
- **不碰 B 站写操作**：评论/私信/点赞/收藏/关注/分享全部禁止。
- **失败可见**：不静默改状态；状态和审计可查，管理员可手动处理。
- **不追求自动兜底一切**：不为极小概率写多套回退；宁可留痕、可手动修。

---

## 9. 相关文件

- 插件入口：`main.py`
- 配置 schema：`_conf_schema.json`
- 核心模块：`core/clock.py`、`core/feed.py`、`core/taste.py`、`core/wake.py`、`core/deep_watch.py`、`core/must.py`、`core/must_loop.py`、`core/diary.py`、`core/report.py`、`core/tools.py`
- 标签页面：`views/tags/index.html`
- CLI：独立的 `bili-sense/` 目录（`bili-sense.mjs` + `lib/`）
- 需求/方案文档不随插件包发布，维护在项目仓库或维护者本地。

---

## 10. 已知限制（摘要）

- 标签是“小样本 Normal-Thompson + Administration base + Assistant taste”，不做 LinUCB/RAG/embedding 迁移。
- `maybe` 不主动推送，只写日记/报告。
- 配额是提示/统计，不是硬闸门；只有 must 单条推送、admin must 阻塞和 3 次浅聊是硬约束。
- 有标签管理页（`views/tags`），但没有独立前端工程。
- 没有多用户/多人格隔离。
- 依赖 AstrBot、DSH、gateway、LivingMemory 同时在线；任一环缺失都会影响对应能力。
- 审计 JSONL 不自动轮转。

---

## 11. 下一步：shadow 验收

当前不缺功能，缺的是真实反馈数据。建议下一阶段只观察，不急着改算法：

1. 在 `tags` 页面改一个 Admin Like 的 base 权重，确认 Audit 出现记录；下一次 feed 的搜索词随之变化。
2. 删除一个非关键 Admin Like，确认同名 Assistant Taste 变为 muted，且不会再被搜到。
3. 让助手正常跑几轮终评，观察 Assistant Taste 是否出现 weight / confidence / n。
4. 记录 `must`、`archived_done`、`discarded` 的变化；至少积累 1–2 周再判断权重是否收敛。
5. 检查同一 tag 是否长期霸占搜索词；如果出现，先调探索位/`pinned`/`locked`，不要直接上 LinUCB。

shadow 期间不需要重载插件；标签页保存后下一轮 feed 生效。
