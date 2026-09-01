# 08 · 发布前真听 QC Checklist

> 本文档对应内容可靠性契约 `docs/07-content-reliability.md` §4。  
> 目的：在 TTS/音频生成之后、正式发布之前，通过“真听”+结构化核对，拦截术语误读、数字/API 名错误、权衡不自洽、S2 问法失真、三问可区分度不足以及 senior 深度不够等问题。

## 1. 检查项

### 1.1 术语念法抽查
- [ ] 抽查 3 处以上技术术语/缩写/英文品牌在音频中的读法（如 Redis、MySQL、MVCC、RTO、CUBIC、Redlock、Kafka、Go GC Pacer）。
- [ ] 确认连读不会导致歧义（如 "maxmemory-samples"、"read_committed"、"GOMAXPROCS"）。
- [ ] 若出现数字+单位的读法（"1 秒" vs "1s"、"256 个槽位"），确认 TTS 没有吞掉数量级。

### 1.2 数字与 API/函数名核对
- [ ] 关键数字与事实卡一致（RDB save 900/300/60、AOF appendfsync、maxmemory-samples=5、DB_TRX_ID 6 字节、初始 RTO=1s、α=1/8 β=1/4、Kafka max.in.flight=5、Redlock 5 节点 3 多数派等）。
- [ ] API/命令/配置名拼写正确，大小写无误（`SET NX PX`、`appendfsync everysec`、`enable.idempotence`、`isolation.level`、`GOMAXPROCS`、`SELECT ... FOR UPDATE`）。
- [ ] 版本号/默认值声明有来源（Redis 4.0 混合持久化、Kafka 3.0 默认幂等、Go 1.14 异步抢占、Linux CUBIC 2.6.19）。

### 1.3 权衡自洽性检查
- [ ] 每个知识点都给出“为什么选 A 而不是 B”的权衡，而非仅罗列定义。
- [ ] 性能/一致性/可用性三角描述没有自相矛盾（例如既说 Redlock 安全又说它万无一失）。
- [ ] 场景化建议明确：什么情况下用 cache-aside、write-through、write-behind；什么时候不能用 Redlock。

### 1.4 S2 高频问法来源核对
- [ ] S2 段落的问法能在题库或面经中找到对应考点，不是凭空编造的“伪面试题”。
- [ ] 题干改写后考点不变，没有因为口语化而丢失边界条件。
- [ ] 每集 S2 至少包含 2 道来自题库的问题（见 `drivecast/content/question_bank/backend_arch.json`）。

### 1.5 三问可区分度检查
- [ ] 每集 S4 的 3 个自测问题覆盖不同层次（概念/实现/权衡/陷阱），不是同一问题的三种问法。
- [ ] 答案不互相提示，避免“听了第一问就知道第三问答案”。
- [ ] 难度按 senior 听众校准，不出现初中级水准的送分题。

### 1.6 Senior 深度把关
- [ ] 内容默认听众已会用该技术，重点讲原理、源码行为、边界条件、面试追问。
- [ ] 包含“如果面试被追问”的深度扩展点（如 Redlock 争议、GC 停顿影响锁、MVCC purge 滞后、TCP 时钟粒度 G）。
- [ ] 不出现“XX 很好用”“XX 是分布式缓存”这类水稿表述。

### 1.7 与事实卡一致性核对
- [ ] 脚本中所有数字、API 语义、版本行为与 `drivecast/content/fact_cards/*.json` 无冲突。
- [ ] 对事实卡中 confidence < 1.0（标“待人工复核”）的条目，QC 时应重点复核。
- [ ] 若脚本引用了 confidence < 1.0 的事实，优先要求作者提供官方来源或降级表述。

## 2. 记录模板

| 集号 / 主题 | 检查项 | 发现 / 问题描述 | 复核人 | 状态 | 备注 |
|---|---|---|---|---|---|
| 例：E01 Redis 持久化 | 1.2 数字核对 | RDB 默认触发条件误读为 "save 60 1000" | 张三 | 已修复 | 已改为 10000 |
| | 1.4 S2 来源 | S2 第 2 题未在题库找到对应 | 李四 | 待处理 | 需补充到 question_bank |
| | 1.6 Senior 深度 | 缺少 AOF 重写失败场景的边界讨论 | 王五 | 已通过 | 已在 S3 补充 |
| | | | | | |

## 3. 验收判定

- **通过**：所有检查项勾选完成，记录模板中无未关闭的 `待处理`/`待复核` 项。
- **阻塞**：发现任何与事实卡矛盾的数字/API 名，或 S2 问法为编造，必须打回重新生成/审稿。
- **备注**：真听 QC 本身也可能遗漏；发现的错误应追加到对应 episode meta 的 `errata` 字段，并触发 `POST /api/episodes/{id}/regenerate`。
