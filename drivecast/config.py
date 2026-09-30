"""drivecast 配置层(pydantic-settings)。

所有可调项集中在这里,env 或 .env 覆盖,代码里不写死。
契约: docs/02 §5 (LLM), docs/03 §3/§4/§6 (TTS/音频)。
"""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- LLM(OpenAI 兼容,docs/02 §5)----
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 整次调用 wall-clock 上限。gpt-5.5 级模型长文(4000+ 字)实测 100-500s,
    # 上限必须显著高于该分布,否则超时不是兜底而是在击杀本会成功的生成。
    llm_timeout: int = 900
    llm_idle_timeout: int = 120     # 流式空闲看门狗:N 秒收不到 chunk 才视为连接挂死
    llm_max_retries: int = 3
    llm_max_concurrency: int = Field(default=6, ge=1, le=32)
    worker_text_concurrency: int = Field(default=2, ge=1, le=8)
    reviewer_enabled: bool = True   # 脚本事实审稿(docs/07 §3.1);REVIEWER_ENABLED=false 可关省一次 LLM 调用
    reviewer_chunk_chars: int = 6000  # 单次审稿调用的正文字数上限,长集按段切块并行审

    # ---- TTS(docs/03 §3)----
    tts_provider: str = "edge"                    # edge(免费) | doubao(火山引擎豆包,docs/03 §3.5)
    tts_voice_expert: str = "zh-CN-YunxiNeural"   # 主讲(男,沉稳)
    tts_voice_host: str = "zh-CN-XiaoxiaoNeural"  # 主持(女,活泼)—— M2 对话式用
    tts_rate_expert: str = "-5%"                  # 车载基调略减速
    tts_rate_host: str = "+2%"
    tts_pitch: str = "+0Hz"
    tts_max_concurrency: int = Field(default=1, ge=1, le=1)  # 同进程所有通道严格单路
    tts_chunk_max_retries: int = 5
    # 豆包语音合成 seed-tts-2.0(docs/03 §3.5);TTS_PROVIDER=doubao 时生效
    tts_doubao_api_key: str = ""                  # 新版控制台 API Key(X-Api-Key),一个 key 即可
    tts_doubao_app_key: str = ""                  # 旧版控制台退回 App ID + Access Token 一对
    tts_doubao_access_key: str = ""
    tts_doubao_http_url: str = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    tts_doubao_resource_id: str = "seed-tts-2.0"  # 选模型代次+计费;2.0 音色配 seed-tts-2.0
    tts_doubao_uid: str = "drivecast"
    tts_doubao_format: str = "mp3"                # mp3/ogg_opus/pcm;拼接管线按 mp3
    tts_doubao_sample_rate: int = 24000
    tts_doubao_voice_expert: str = "zh_male_m191_uranus_bigtts"    # 云舟(男,2.0 实测效果好)
    tts_doubao_voice_host: str = "zh_female_vv_uranus_bigtts"      # Vivi(女,2.0 实测效果好)
    tts_doubao_rate_expert: int = -5              # speech_rate,[-50,100]
    tts_doubao_rate_host: int = 2
    tts_doubao_recv_timeout: int = 30             # 流式单次读超时,超时按失败退避重试

    # ---- 音频后处理(docs/03 §4)----
    loudness_i: float = -16.0       # LUFS,播客常用
    loudness_tp: float = -1.5
    loudness_lra: float = 11.0
    silence_between_chunks_ms: int = 100   # 同角色段间停顿
    chapter_titles: str = "回顾与开场|核心知识点|高频面试问法|答题框架|易错点与自测"
    chapter_title_refs: str = "参考资料"   # 片尾参考文献口播段(S5)章节名
    audio_artist: str = "DriveCast"
    audio_cover_path: Path = PROJECT_ROOT / "static" / "assets" / "cover.png"

    # ---- M6 质量校验/校准(只给建议,绝不回写 .env) ----
    quality_min_minutes: float = 8.0
    quality_max_minutes: float = 45.0    # 上限覆盖 40 分钟长集(docs/01 §2.1)
    quality_duration_error_pct: float = 10.0
    quality_max_peak_db: float = -0.5
    speech_rate_history_window: int = 20
    speech_rate_min_samples: int = 3

    # ---- 内容(docs/01 §2.1 / 03 §6.1)----
    target_minutes: int = 15
    words_per_minute: int = 300     # rate=-5% 实测 280-320,合成后 ffprobe 回填校准
    # 单段单 agent 字数上限:超出则把该段拆成多个并行子 agent 再拼接(40 分钟长集必备,
    # 单次调用写 4000+ 字又慢又容易缩水);2400 字 ≈ 8 分钟口播,与既有 15/20 分钟集的单段量级一致
    section_max_words_per_agent: int = 2400
    default_episode_count: int = 6
    daily_new_episodes: int = 1     # 每日新学集数:计划排期按 (seq-1)//N 天递进

    # ---- 参考链接猎手(docs/07 §1.4)----
    ref_hunter_enabled: bool = True  # 生成脚本时实时检索参考链接;False 退回题库静态 refs
    ref_search_provider: str = ""    # 实时搜索:tavily / bing;空=不联网搜(仍走 LLM 提名+校验)
    ref_search_api_key: str = ""
    ref_verify_timeout: int = 8      # 链接存活校验/搜索的单次请求超时(秒)
    ref_max_results: int = 8         # 每集参考链接上限

    # ---- Web(docs/04 §5 / docs/05 M3) ----
    web_host: str = "127.0.0.1"          # 仅绑本地,无鉴权单用户
    web_port: int = 8080
    web_graceful_shutdown_sec: int = 10         # 关停时等长连接(SSE)结束的上限,超时强断
    static_dir: Path = PROJECT_ROOT / "static"
    sse_poll_interval_sec: float = 2.0
    worker_poll_interval_sec: float = 1.0
    worker_enabled: bool = True                 # 验收/维护时用 WORKER_ENABLED=false 关闭消费
    worker_stale_running_sec: int = 3600        # running 超过该时长视为孤儿,空轮询时复位 pending

    # ---- 认证(用户名+密码登录,HS256 JWT)----
    auth_secret: str = ""               # JWT 签名密钥;空=进程级随机(重启后所有 token 失效),部署必配
    auth_token_expire_days: int = 7     # token 有效期(天);无刷新/撤销,自然过期

    # ---- 路径 ----
    data_dir: Path = PROJECT_ROOT / "data"

    # 便捷派生
    @property
    def target_words(self) -> int:
        return self.target_minutes * self.words_per_minute

    def plan_path(self, plan_id: str) -> Path:
        return self.data_dir / "plans" / f"{plan_id}.json"

    def script_path(self, episode_id: str) -> Path:
        return self.data_dir / "scripts" / f"{episode_id}.script.json"

    def script_draft_path(self, episode_id: str) -> Path:
        return self.data_dir / "scripts" / f"{episode_id}.draft.json"

    def tts_path(self, episode_id: str) -> Path:
        return self.data_dir / "tts" / f"{episode_id}.tts.json"

    def chunks_dir(self, episode_id: str) -> Path:
        return self.data_dir / "audio" / "chunks" / episode_id

    def episode_audio_path(self, episode_id: str) -> Path:
        return self.data_dir / "audio" / "episodes" / f"{episode_id}.mp3"

    def meta_path(self, episode_id: str) -> Path:
        return self.data_dir / "meta" / f"{episode_id}.json"

    @property
    def chapter_title_list(self) -> list[str]:
        titles = [title.strip() for title in self.chapter_titles.split("|") if title.strip()]
        defaults = ["回顾与开场", "核心知识点", "高频面试问法", "答题框架", "易错点与自测"]
        return (titles + defaults[len(titles):])[:5]


settings = Settings()


def ensure_data_dirs() -> None:
    for sub in ("plans", "scripts", "tts", "meta", "audio/chunks", "audio/episodes"):
        (settings.data_dir / sub).mkdir(parents=True, exist_ok=True)
