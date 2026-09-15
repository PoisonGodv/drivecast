# DriveCast 音频管线设计(03)

> 面向车载通勤收听的技术面试学习播客。输入是主题,产出是可入库、可下载、可拖进度条的 mp3。
> 目标听众:资深后端工程师,内容深度匹配,车载环境(噪声大、不能看屏幕、注意力碎片化)。

## 1. 端到端管线总览

```
主题 ──► [S1 学习计划] ──► [S2 逐集脚本生成] ──► [S3 脚本预处理 TTS友好化]
      ──► [S4 TTS 分角色分段合成(edge-tts/豆包)] ──► [S5 音频后处理(响度归一/拼接/元数据)]
      ──► [S6 入库 + 章节标记 + 播放页]
```

每一步都有落盘中间产物,保证**断点续做**:

```
drivecast/
  data/
    plans/{plan_id}.json          # S1 产出:学习计划(集列表)
    scripts/{ep_id}.script.json   # S2 产出:带说话人标记的原始脚本
    tts/{ep_id}.tts.json          # S3 产出:TTS 友好化后的分段文本(每段含 voice/rate/pitch)
    audio/chunks/{ep_id}/{seq}.mp3 # S4 产出:逐段音频(最小合成单元)
    audio/episodes/{ep_id}.mp3    # S5 产出:最终整集(已归一+拼接+ID3)
    meta/{ep_id}.json             # S6 产出:章节标记/时长/字数统计
```

状态机:`pending → scripted → tts_ready → synth_partial → synth_done → post_done → ready`
断点续合成 = 遍历 chunks 目录,缺的 seq 才重新调 edge-tts;post_done 之前任何一步失败都不影响已落盘 chunk。

---

## 2. S2/S3:脚本与 TTS 友好化(听感质量的关键)

### 2.1 脚本结构(S2 产出契约)

LLM 输出严格 JSON,按"轮(turn)"组织,每轮一个说话人:

```json
{
  "ep_id": "ep_0007",
  "title": "分布式锁:Redis SETNX 到 RedLock 的坑",
  "turns": [
    {"speaker": "host",  "text": "开场……"},
    {"speaker": "expert", "text": "我们先看 SETNX……"}
  ]
}
```

`host`=引导/提问/总结(节奏快、活泼),`expert`=讲原理/拆八股(节奏稳、权威)。双人对话感来自角色交替,不是真两个人。

**片尾参考文献段(S5)**:脚本落盘前,从本集已校验的参考链接确定性拼接一段口播(不过 LLM):只念资料标题(最多 6 条、单条截 40 字),不念 URL,收尾提示"链接已附在本集文稿末尾"。独白式由 expert 念、对话式由 host 收尾念;落为独立 S5 chunk 与「参考资料」章节,同时拼进文稿末尾保持音文一致。无参考链接则不出此段。

### 2.2 TTS 友好化(S3)— 听感翻车的 90% 在这步

纯文本直接喂 TTS 会把 `QPS`、`p99`、`O(log n)`、`etcd` 念成乱码。必须做"念法改写",规则如下(在脚本预处理阶段用确定性的代码+一张映射表完成,**不要靠 LLM 自由发挥**,否则同术语前后念法不一致):

| 类型 | 原文 | 念法(SSML/改写后) | 处理方式 |
|---|---|---|---|
| 数字/大数 | `10000 QPS` | `一万 QPS` | 中文数字朗读,>4位转 万/亿 |
| 百分位 | `p99 / p99.9` | `P 九十九 / P 九十九点九` | 映射表 |
| 时间 | `200ms` | `两百毫秒` | 单位展开 |
| 版本号 | `Go 1.21` | `Go 一点二一` | 点号读"点" |
| 英文缩写 | `QPS/TTL/RPC/CPU` | 保留字母但前后加空格停顿 | 映射表 + 空格 |
| 英文术语 | `etcd / nginx / Kafka` | 按社区通行中文读法:`etcd`→"E T C D"或保留原音 | 映射表逐条人工校对 |
| 代码标识符 | `sync.Mutex` / `http.HandleFunc` | 不逐字符念,念语义:"sync 包里的互斥锁" | **脚本层就要求 LLM 别写裸代码** |
| 公式/复杂度 | `O(log n)` | `O log n` | 映射表 |
| 标点停顿 | 长句 | 在子句间补 `、` 或 `……` 引导停顿 | 规则:超 40 字无标点则按语义切 |

**根本原则:脚本生成 prompt 里就约束 LLM "说人话、别贴代码、术语用通行中文念法"**,S3 只做兜底改写和一致性校验(同一术语全篇念法统一)。代码示例一律转成口语描述("一个带过期时间的 set if not exist")。

### 2.3 分段(chunking)

edge-tts 单次请求有长度与稳定性约束,且长文本容易被限流/截断。按 turn 天然分段,再对超长 turn 二次切:

- 单 chunk 目标 **200~400 字**,硬上限 500 字
- 切分点:句号/问号/感叹号;优先在语义边界,**不在术语或数字中间切**(正则保护 `[\w.]+%?` 连续段)
- 每个 chunk 记录 `{seq, speaker, voice, rate, pitch, text}`,按 seq 命名落盘 → 断点续合成直接按缺失 seq 补

---

## 3. S4:TTS 合成细节(edge-tts 默认 / 豆包可选)

### 3.1 音色选择(中文 Neural)

推荐双音色搭配(都属 `zh-CN`,自然度和稳定性最好的一档):

| 角色 | 音色 | 理由 |
|---|---|---|
| expert(主讲) | `zh-CN-YunxiNeural`(男) | 沉稳、清晰,技术讲解有权威感,车载低频噪声下穿透力好 |
| host(引导) | `zh-CN-XiaoxiaoNeural`(女) | 活泼、亲和,提问/过渡/总结时提神,男女声切换本身强化"对话感" |

备选:`zh-CN-YunjianNeural`(更磁性的男声,可替换 expert)、`zh-CN-XiaoyiNeural`(更年轻女声)。**避免**用过快的音色,车载需要更慢更稳。

先 `edge-tts --list-voices | grep zh-CN` 实测确认当前可用清单(Microsoft 偶发下线个别音色),音色做成配置项不写死。

### 3.2 rate / pitch

- 整体基调:`rate="-5%"`(车载略减速,补偿噪声与注意力分散)
- expert 讲原理:`rate="-8%", pitch="+0Hz"`(稳)
- host 提问/串场:`rate="+2%", pitch="+2Hz"`(轻快)
- 开场摘要 / 结尾自测:`rate="-10%"`(强调,让用户听清记点)
- pitch 微调即可,>±5Hz 会失真。参数全部进 chunk 元数据,不靠全局。

### 3.3 限流与并发

edge-tts 走免费公共服务,**并发是头号限流诱因**:

- 单进程 TTS 请求并发严格 **1**，Edge 与豆包共用请求锁；`TTS_MAX_CONCURRENCY` 只能为 1。
- 每个 chunk 之间 sleep 0.3~0.8s(随机抖动,模拟自然请求)
- 失败指数退避:1s→2s→4s→8s,单 chunk 最多重试 5 次,仍失败则标记该集 `synth_partial` 并记录失败 seq,**绝不整集重头来**
- 文本通道默认 2 个任务、LLM 总预算 6 路；音频通道 1 个任务。文本与 TTS 可重叠，同计划脚本保持顺序。S3 在 S2 完成后启动，S4 在 S1/S2 完成后启动，参考猎取提前与正文并行。

### 3.4 调用方式

优先 `edge-tts` Python 库(非 CLI),便于流式写盘和控制:

```python
import edge_tts, asyncio
async def synth(text, voice, rate, pitch, out):
    c = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
    await c.save(out)   # 每 chunk 独立 mp3
```

### 3.5 豆包语音合成 seed-tts-2.0(付费可选通道)

edge-tts 是免费公共服务,音色与稳定性受微软侧约束;接入火山引擎豆包 TTS(seed-tts-2.0)作为可切换的第二通道。协议用同族 v3 接口中的 **HTTP 单向流式**(`POST https://openspeech.bytedance.com/api/v3/tts/unidirectional`,NDJSON 流式返回 base64 音频)——本场景是批量出文件、chunk 文本本来就整段就绪,用不到双向 WS 的"边发文本边收音频",HTTP 通道更简单且是 API Key 鉴权的官方文档化组合。双向流式 WS(`wss://.../api/v3/tts/bidirection`)为同能力实时版,后续要做边生成边播再迁。

- **鉴权(新版控制台,推荐)**:一个 API Key 即可——`X-Api-Key` + `X-Api-Resource-Id`;旧版控制台退回 `X-Api-App-Id` + `X-Api-Access-Key` 一对,代码按配置自动切换
- **Resource-Id 即模型代次**:默认 `seed-tts-2.0`(仅 2.0 音色,`*_uranus_bigtts` / `saturn_*` 命名);用 1.0 音色(`*_moon_bigtts`/`*_wvae_bigtts` 等)需改 `TTS_DOUBAO_RESOURCE_ID=seed-tts-1.0`,代次与音色不匹配报 `55000000 resource ID is mismatched`
- **切换**:`.env` 设 `TTS_PROVIDER=doubao` + `TTS_DOUBAO_API_KEY`;edge 通道配置不动,随时切回
- **默认音色**:expert=`zh_male_m191_uranus_bigtts`(云舟,男)、host=`zh_female_vv_uranus_bigtts`(Vivi,女)——均为社区实测效果好的 2.0 音色;`TTS_DOUBAO_RATE_EXPERT/HOST` 复用 edge 的 ±N% 语义(speech_rate [-50,100])
- **实现**(`tts/doubao.py`):单 chunk 单请求,流式逐行解析 NDJSON,`code=0` 的 data(base64)拼音频,`code=20000000` 结束,非 0 抛错;坏行抛错不吞(吞会静默丢音频)
- **失败语义与 edge 通道完全一致**:全局信号量 + 指数退避 + `.part` 原子写 + 单 chunk 重试,断点续合成/章节/响度归一全链路不受通道影响
- **试音**:`python scripts/doubao_voice_probe.py` 按当前配置音色各合成一句到 `data/audio/_probe/`
- 付费按 `text` 字符计费;2.0 支持 `additions.context_texts` 自然语言情感/语速指令(逐句调用才稳定,暂未接,需要时按官方 additions 文档加)

---

## 4. S5:双人对话拼接与后处理

### 4.1 依赖决策:pydub 不够,需要 ffmpeg

- `pydub` 只是 ffmpeg 的 Python 封装,**底层仍要装 ffmpeg 才能解/编码 mp3**
- 纯 Python 无依赖方案(如直接二进制拼 mp3 帧)对 CBR mp3 勉强可行但脆(VBR/帧头/响度归一都搞不定),不值得
- **结论:装 ffmpeg。** 本机当前未装(`ffmpeg not found`):

```bash
brew install ffmpeg
pip install edge-tts pydub
```

ffmpeg 用途:①mp3 解码拼接 ②**响度归一(loudnorm)** ③写 ID3 元数据/封面 ④(可选)生成章节。

### 4.2 拼接流程(每集)

1. **逐 chunk 归一**:每个 chunk 先过 `loudnorm` 到统一响度(目标 **I=-16 LUFS**,播客常用;车载可再压到 -14 提升可懂度),`TP=-1.5, LRA=11`
   - 双人音量天然不均,先归一每个 chunk 再拼,比整集一次性归一更稳
2. **句间停顿**:host↔expert 切换处插 250ms 静音,同角色连续 chunk 间 100ms,让"换人说"有呼吸感
3. **拼接**:`ffmpeg -f concat` 按 seq 顺序合并(同码率可直接 stream copy,跨参数先统一转码到 `libmp3lame -q:a 4` ≈ 165kbps VBR)
4. **整集二次 loudnorm 兜底**(两趟 loudnorm 模式取 measured 值再线性应用,音质损失最小)
5. **首尾垫乐**(可选):0.5s 淡入 + 结尾 1s 淡出,提升"成品感",不需要真音乐,纯淡入淡出即可

### 4.3 元数据与章节

- **ID3**:标题(集名)、artist("DriveCast")、album(学习计划名/主题方向)、track(集序号)、封面(统一生成一张封面图,内嵌)
- **章节标记**:开场摘要 / 各知识点 / 结尾自测,各记一个时间戳;有参考链接的集在末尾追加 **S5「参考资料」章节**(片尾口播段,见 §2 末尾)。两种落法:
  - 写进 `meta/{ep_id}.json`,自有播放页用它做进度条分段跳转(**主方案,可控**)
  - (可选)用 ffmpeg 的 `ffmetadata` 写 CHAPTER 到 mp3,兼容 Apple Podcasts 类客户端
- 章节时间戳在拼接时就能算出来(累加每个 chunk 时长+静音),不需要二次分析

---

## 5. 车载收听优化(集结构)

每集固定四段式(写进 S2 脚本生成 prompt,作为硬结构):

```
[1] 开场摘要(约 30s / 90~120 字):今天讲什么、为什么面试爱考、3 个要点预告
[2] 正文(知识点 × 2~4 个):每个知识点 = 原理 → 常见面试问法 → 答题框架 → 一个坑
[3] 快问快答(双人对话高光):host 抛高频面试题,expert 给一句话标准答法
[4] 结尾自测 3 问(rate 放慢,每问之间停 1.5s 留白,让用户在车里口头作答)
```

- **结构化优先**:正文每个知识点单独成章(见 4.3 章节),用户等红灯/到公司能精准重听某段
- **出口**:①本地 Web 播放页(进度条 + 章节跳转 + 倍速,手机连同一局域网也能开) ②**下载 mp3 到手机**(微信/AirDrop/文件共享),配合支持章节的播客 App 离线听
- 车载核心诉求:不看屏也能听完、能断点续播、噪音下听得清 → 靠慢速+响度归一+章节保证

---

## 6. 时长与质量控制

### 6.1 字数→时长换算

中文 TTS 实测绘速约 **4.5~5.5 字/秒**(rate=-5% 时取 ~5 字/秒,即每分钟 **280~320 字**)。

| 目标时长 | 脚本目标字数(正文) |
|---|---|
| 10 分钟 | 约 3000 字 |
| 15 分钟(主力目标) | 约 4500 字 |
| 20 分钟 | 约 6000 字 |

**S2 生成脚本时就按目标字数下 prompt**(如"本集正文约 4500 字"),而不是生成后再裁。每集合成后用 `ffprobe` 读真实时长回填 meta,与预估对比校准换算系数(系数存配置,越用越准)。

### 6.2 质量校验(入库前自动跑)

- 实际时长默认在 8~45 分钟区间内，否则告警；范围可配置，文案使用实际阈值。另分别计算用户目标偏差与文本预测偏差，默认阈值 10%。告警不自动重新生成音频。
- 响度归一后峰值无削波(ffmpeg `volumedetect` 检查 max_volume ≤ -0.5dB)
-  chunk 数与 tts.json 段数一致(防漏拼)
- 抽样人工/二次听检开场 10s + 任一知识点段,确认术语念法正确

### 6.3 失败重试与断点续合成(落地规则)

- 最小重试单元 = **单个 chunk**,已落盘的 chunk 永不重合成
- 集级状态 `synth_partial` 记录缺失/失败 seq 列表,下次启动只补这些
- edge-tts 连续失败超过阈值(如 3 个 chunk × 5 次重试)→ 判定服务端限流,整批暂停 5 分钟再续,不死循环
- 后处理(ffmpeg)失败多为环境问题,单独报错不污染已合成的 chunk
- 所有中间产物按 ep_id 隔离,可整集删除重生成而不影响其他集

---

## 7. 本机环境落地清单

```bash
# 一次性
brew install ffmpeg
pip install edge-tts pydub

# 验证
edge-tts --list-voices | grep zh-CN     # 确认音色可用
ffmpeg -version                          # 确认 ffmpeg 装好
```

- Python 3.14.7(pyenv):edge-tts / pydub 均为纯 Python 轮子,兼容;ffmpeg 走 subprocess 调用,无绑定兼容问题
- 配置项(不写死):音色对、rate/pitch、并发数、响度目标、字数→时长系数、每集目标时长

### 质量规则升级与恢复

质量独立版本化，不通过增加音频流水线版本强制重编码。`quality_recheck` 仅读取成品/已有材料并更新 meta 与 DB；历史材料缺失为 unknown，不伪造通过。保留章节和人工 QC。旧 `target_duration_error` 表示预测误差，不等同用户所选分钟数。

缓存块需可探测到正时长才复用；仅缺失或损坏块重新请求 TTS。本地 `audio_upgrade` 不允许调用远程 TTS。正常停机恢复任务为 pending，删除取消不会恢复。
