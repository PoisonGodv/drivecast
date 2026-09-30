# DriveCast 前端设计文档

> 定位:本地 Web 应用(Python 后端 + 浏览器前端),核心场景是**开车通勤听技术播客**。
> 设计原则:轻量优先、开车友好、面试学习导向。用户是资深后端工程师,内容呈现不水、交互不啰嗦。

---

## 1. 页面清单与用户动线

### 1.1 页面清单

| 路由 | 页面 | 职责 |
|---|---|---|
| `#/login` | 登录 | 用户名+密码表单,签发 JWT 后回跳原视图(§5.4) |
| `/` | 首页 | 今日推荐 + 进行中的计划 + 快速入口 |
| `/create` | 创建计划 | 输入主题 → 选方向 → 定集数 → 确认大纲 |
| `/plan/:id` | 计划详情 | 剧集列表 + 每集生成状态/进度 + 播放入口 |
| `#/play/:episodeId` | 播放页 | 开车场景核心页(详见 §2) |
| `/review` | 题库复习页 | 卡片式自测,按掌握度标记(详见 §4) |

不提供注册页(账号由 CLI `python -m drivecast create-user` 管理),不做导航栏之外的冗余信息架构。顶部 4 个 tab:首页 / 新建 / 复习 / 播放(有在播集时显示),右侧挂当前用户名与"退出"入口。未登录时任何视图先落 `#/login`,登录成功后回跳原视图。

### 1.2 用户动线

**主动线(创造内容):**
```
首页 → [新建] → 输入主题(自由文本) → 选内容方向(四选一/自定义)
     → 定集数(默认 6 集,可调 4-12) → AI 生成大纲(逐集标题+知识点,可删改)
     → 确认 → 跳计划详情页,后台逐集生成(脚本→TTS)
     → 每集就绪即可点播放,不必等全部生成完
```

**主动线(消费内容,开车场景):**
```
首页 → 今日推荐/进行中计划 → 点"继续播放" → 播放页
     → 锁屏听 → 一集播完自动连播下一集 → 到家后打开题库复习页自测
```

关键设计决策:**"边生成边听"**。逐集生成耗时长(单集脚本+TTS 可能 1-3 分钟),绝不让用户等整个计划生成完。第一集 ready 就可以在通勤路上开始听,后续集在后台继续生成。

---

## 2. 播放页重点设计(开车场景)

这是唯一一个"使用中不看屏幕"的页面,一切为盲操作和锁屏服务。

### 2.1 布局(竖屏移动端优先,桌面浏览器同构)

```
┌─────────────────────────┐
│ 计划名 · 第 3/8 集        │  ← 小字,扫一眼即可
│                         │
│      本集标题(大字)      │
│   知识点标签 #Redis #缓存  │
│                         │
│  ┌───────────────────┐  │
│  │   进度条(超大热区)  │  │  ← 整行可拖,高度 ≥ 48px
│  └───────────────────┘  │
│  -0:15        12:34     │
│                         │
│   [−15s]  [▶/⏸]  [+15s] │  ← 三个巨型按钮,播放键 ≥ 96px
│                         │
│  [1.0x▾]      [本集文稿] │  ← 倍速 + 文稿抽屉
└─────────────────────────┘
```

硬性规则:
- **播放/暂停按钮 ≥ 96×96px**,快进快退 ≥ 72×72px,间距 ≥ 24px —— 等红灯时余光盲按不miss。
- 进度条整行可点可拖,热区上下各扩 24px(`padding` 透明区),不要求精确点中细线。
- 全程单手可达:核心控件集中在屏幕下 2/3。
- 深色主题为默认(夜间通勤),对比度满足 WCAG AA。

### 2.2 锁屏控制:Media Session API

开车时屏幕是锁的,控制依赖系统锁屏/蓝牙车机/耳机按键。用 **Media Session API** 把网页播放注册成系统级媒体会话:

```js
const audio = new Audio(`/api/episodes/${id}/audio`);

navigator.mediaSession.metadata = new MediaMetadata({
  title: episode.title,
  artist: plan.name,           // 锁屏显示"计划名"
  album: 'DriveCast',
  artwork: [{ src: '/cover.png', sizes: '512x512' }],
});

navigator.mediaSession.setActionHandler('play',  () => audio.play());
navigator.mediaSession.setActionHandler('pause', () => audio.pause());
navigator.mediaSession.setActionHandler('seekbackward', () => skip(-15));
navigator.mediaSession.setActionHandler('seekforward',  () => skip(+15));
navigator.mediaSession.setActionHandler('previoustrack', prevEpisode);
navigator.mediaSession.setActionHandler('nexttrack',     nextEpisode);
```

效果:iOS/Android 锁屏界面、车机中控、蓝牙耳机按键都能控制播放/暂停/±15s/切集,**用户全程不用碰手机屏幕**。
注意:Safari 对 Media Session 支持完整(macOS 14+/iOS 15+ 基本可用);锁屏进度同步用 `setPositionState()` 在 `timeupdate` 里节流上报。

### 2.3 后台连续播放(自动连播)

- `audio.onended` → 拉取本计划下一集(取 `status=ready` 且序号最近的)→ 切换 `audio.src` → 更新 Media Session metadata → `play()`。
- 浏览器后台/锁屏下 `<audio>` 可持续播放(只要 Media Session 注册且用户有过一次手动 play 手势)。这是"音频文件"方案的天然优势,不需要 Service Worker。
- 连播队列在客户端内存维护,刷新后从"最近播放进度"(localStorage + 服务端各存一份)恢复。
- **播放进度落盘**:每 10s 和 `pause`/`ended` 时 POST 一次当前集 position,下次进入任何页面都能"继续上次"。

### 2.4 倍速与文稿

- 倍速档位:0.75 / 1.0 / 1.25 / 1.5 / 2.0,循环切换或下拉,记忆用户上次选择。`audio.playbackRate` 原生支持。
- "本集文稿"底部抽屉:展示结构化脚本(知识点 / 常见面试问法 / 答题框架),适合到家后对照复盘,**开车时默认收起**。

---

## 3. 生成进度的实时反馈

### 3.1 选型:SSE(Server-Sent Events),不用 WebSocket,也不用轮询

| 方案 | 结论 | 理由 |
|---|---|---|
| **SSE** | **采用** | 服务端→客户端单向推送,正好匹配"生成进度"场景;基于 HTTP,FastAPI `StreamingResponse` 几行搞定;浏览器 `EventSource` 原生支持,**断线自动重连**;无额外依赖 |
| 轮询 | 备选降级 | 实现最简但浪费请求、进度更新有延迟感;仅当 SSE 出问题时的兜底 |
| WebSocket | 不采用 | 双向通信在这里用不上,引入连接管理/心跳复杂度,杀鸡用牛刀 |

### 3.2 事件协议

计划详情页打开时建立一条连接:

```
GET /api/plans/{id}/events    →  text/event-stream
```

后端在生成管线的每个里程碑推送事件:

```
event: episode_status
data: {"episode_id": "e3", "seq": 3, "stage": "tts", "status": "generating", "percent": 60}

event: episode_status
data: {"episode_id": "e3", "seq": 3, "stage": "done", "status": "ready", "audio_url": "/api/episodes/e3/audio", "duration_sec": 754}

event: plan_status
data: {"plan_id": "p1", "ready": 3, "total": 8, "all_done": false}
```

`stage` 枚举:`outline → script → tts → done | failed`。前端据此渲染每集的状态徽章。

### 3.3 进度展示设计

- **每集一行**,状态用颜色+图标徽章:`排队中(灰) → 写脚本中(蓝,呼吸动画) → 合成语音中(蓝,显示 percent) → 可播放(绿,出现▶按钮) → 失败(红,可重试)`。
- 顶部一条总进度条:`已就绪 3/8 集`,给"边生成边听"一个直观预期。
- 某集变 `ready` 时,若用户正停留在详情页,该行**轻微高亮 + 提示音**(可关),并把它顶部的"继续播放"按钮点亮。
- 连接断开:`EventSource` 自动重连;重连后主动拉一次全量计划状态对齐,避免漏事件。
- 失败集:行尾"重试"按钮,POST `/api/episodes/{id}/generate`,不阻塞其他集。

---

## 4. 题库复习页

面试导向的核心沉淀页:每集脚本生成时,AI 同步抽出若干张**自测卡**(问题 + 答题框架),汇入题库。

### 4.1 卡片式自测交互

```
┌───────────────────────────┐
│ #Redis #缓存穿透            │
│                           │
│  Q: 缓存穿透/击穿/雪崩的区别 │
│     和各自解法?            │
│                           │
│      [ 点击翻面 ]          │  ← 整卡可点
└───────────────────────────┘
        翻面后 ↓
┌───────────────────────────┐
│  A(答题框架):              │
│  1. 定义三连:穿透=查无此键…  │
│  2. 解法:布隆过滤器/空值缓存 │
│  3. 击穿→互斥锁+逻辑过期…    │
│  ─────────────           │
│  [ 陌生 ] [ 模糊 ] [ 掌握 ]  │  ← 掌握度三档标记
└───────────────────────────┘
```

- 整卡点击翻面(CSS 3D transform,无需库),翻面看"答题框架"而非长篇答案 —— 训练面试答题结构,不是背答案。
- 掌握度三档:`陌生 / 模糊 / 掌握`,标记后写回服务端,作为下次排序权重。

### 4.2 排序与筛选

- 默认排序:**简化间隔重复**（与 01 §5.3 对齐的固定间隔:unknown=+1 天、fuzzy=+2 天、mastered=+7 天）—— 陌生卡高频出现,掌握卡拉长间隔;存 `mastery`(三态) + `last_reviewed_at` + `next_review_at` 即可,不引入复杂算法库。SM-2 完整算法留作 V2 增强。
- 顶部筛选:按计划 / 按标签(#Redis #分布式) / 按掌握度。
- 一个"今日待复习 N 张"计数,和首页联动,形成"路上听 → 到家练"闭环。

---

## 5. 技术选型与组件结构

### 5.1 选型(轻量优先)

| 层 | 选择 | 理由 |
|---|---|---|
| 框架 | **原生 ES Module + 少量 vanilla JS**,或 **Alpine.js / petite-vue**(约 6KB) | 单页不多、交互不重,上 React/Vue 全家桶是过度工程;若要声明式渲染用 petite-vue 一档轻量方案即可 |
| 构建 | **无构建**:后端直接托管静态文件 | 本地应用,FastAPI `StaticFiles` 挂 `/static`,省掉 node_modules/打包链;改动即刷新 |
| 样式 | **手写 CSS + CSS 变量**做主题;可选 **Pico.css**(class-less,约 10KB)打底 | 不引入 Tailwind/AntD/MUI 等重型方案;深色主题用 CSS 变量一套切换 |
| 音频 | 原生 `<audio>` + Media Session API | 见 §2,零依赖 |
| 实时 | 原生 `EventSource`(SSE) | 见 §3 |
| 状态 | 页面级模块内状态 + localStorage 持久化(进度/倍速) | 无全局 store 需求 |
| 路由 | hash 路由(几十行)或服务端模板多页 | 页面仅 5 个,不值得引 router 库 |

> 若后续交互复杂度上升(如文稿高亮跟读、复杂筛选),再平滑迁移到 Vue3 + Vite 也不迟;MVP 阶段守住"无构建、零重型依赖"。

### 5.2 目录与组件结构

```
static/
├── index.html            # 壳,含顶部 tab 导航
├── css/
│   ├── base.css          # 变量/主题/重置
│   └── components.css    # 卡片/按钮/进度条/徽章
├── js/
│   ├── main.js           # 入口 + hash 路由
│   ├── api.js            # fetch 封装(计划/剧集/卡片)
│   ├── player.js         # 播放页:audio + MediaSession + 连播队列
│   ├── progress.js       # SSE EventSource 封装 + 状态渲染
│   ├── review.js         # 题库翻面卡 + 掌握度标记
│   └── store.js          # localStorage 读写(进度/倍速/主题)
└── assets/
    └── cover.png         # Media Session 锁屏封面
```

职责边界:
- `player.js` 是唯一碰 `<audio>` 和 Media Session 的地方,全局单例,跨页面保持播放(路由切换不销毁 audio)。
- `progress.js` 只在计划详情页激活,离开页面 `eventSource.close()`。
- `api.js` 统一错误处理(本地服务挂了给明确提示)。

### 5.3 需要后端配合的接口约定(前端视角提需求)

| 接口 | 说明 |
|---|---|
| `GET /api/plans` / `POST /api/plans` | 列表 / 创建(主题+方向+集数) |
| `POST /api/plans/{id}/outline` | 生成大纲(可编辑后确认) |
| `GET /api/plans/{id}` | 计划详情含每集 status/seq/duration |
| `GET /api/plans/{id}/events` | **SSE** 进度推送 |
| `GET /api/episodes/{id}/audio` | 音频流(支持 Range,拖动进度条需要) |
| `GET /api/episodes/{id}/script` | 结构化文稿(知识点/问法/框架) |
| `POST /api/episodes/{id}/generate` | 失败重试 |
| `GET/POST /api/cards` · `POST /api/cards/{id}/mastery` | 题库与掌握度（命名以 02 为准:`GET /api/review/today` · `POST /api/questions/{id}/grade`,body `{mastery:"unknown|fuzzy|mastered"}`) |
| `GET/PUT /api/playback/position` | 播放进度存取 |

**性能注意点**:音频接口必须支持 HTTP `Range` 请求(否则进度条拖动和断点续播失效);音频文件按集落盘、用文件流返回,不要整文件读进内存。

### 5.4 登录与凭证(2026-09 多用户改造)

用户名+密码登录,后端签发 HS256 JWT(7 天过期),多用户数据隔离以服务端为准;前端只负责凭证的存取、注入与过期后的跳转。

- **凭证存取**:`store.js` 统一管理,token 存 `localStorage['drivecast:token']`(沿用 `drivecast:` 前缀),同时缓存用户名供展示;`getToken/setToken/clearToken` 是唯一入口。
- **请求注入**:`api.js` 的 `request()` 统一加 `Authorization: Bearer <token>` 头。
- **401 处理**:任何非登录请求收到 401 → 清 token → 广播 `drivecast:unauthorized` 事件;`main.js` 监听后停播、断开 SSE、切到 `#/login` 并记住来路。登录请求(`POST /api/auth/login`)自身豁免,防止 401 死循环。
- **SSE 与音频例外**:`EventSource` 和 `<audio>/<a download>` 无法自定义请求头,这三类 GET(`/api/plans/{id}/events`、`/api/episodes/{id}/audio(/download)`)用 `withToken()` 拼 `?token=` 查询参数(后端仅对这 3 个 GET 端点开放 query token,其余端点带 `?token=` 也当无凭证)。`withToken` 只对本站相对路径生效,不把 token 外带到外域。
- **路由守卫与启动验活**:启动时若有 token 先 `GET /api/auth/me` 验活(校验期间显示 loading,防登录页闪烁);未登录访问任何视图先落 `#/login`,`pendingHash` 记住原视图,登录成功后回跳;已登录访问 `#/login` 自动回首页。`EventSource` 401 会静默循环重连且无法被 fetch 拦截,SSE `onerror` 时探测一次 `/api/auth/me`,失效即走统一 401 路径踢回登录。
- **退出登录**:头部"退出"入口 → 停播、断开 SSE、清 token、回 `#/login`。
- **修改密码**:头部"修改密码"入口(`account.js`)打开弹层(原密码/新密码/确认),前端先校验长度≥8 与两次一致;`POST /api/auth/password` 失败(原密码错为 400、频繁为 429)只在表单内提示。成功后服务端吊销该用户全部旧 token 并回发新 token:`store.setToken` 换凭证(会重置会话,旧 SSE/音频 URL 带旧 token 一并卸载),再恢复登录态并重新应用当前路由。
