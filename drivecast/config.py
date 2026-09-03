"""drivecast 配置层(pydantic-settings)。

所有可调项集中在这里,env 或 .env 覆盖,代码里不写死。
契约: docs/02 §5 (LLM), docs/03 §3/§4/§6 (TTS/音频)。
"""
from pathlib import Path

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
    llm_timeout: int = 180          # 长脚本输出给足时间
    llm_max_retries: int = 3
    reviewer_enabled: bool = True   # 脚本事实审稿(docs/07 §3.1);REVIEWER_ENABLED=false 可关省一次 LLM 调用

    # ---- TTS(docs/03 §3)----
    tts_voice_expert: str = "zh-CN-YunxiNeural"   # 主讲(男,沉稳)
    tts_voice_host: str = "zh-CN-XiaoxiaoNeural"  # 主持(女,活泼)—— M2 对话式用
    tts_rate_expert: str = "-5%"                  # 车载基调略减速
    tts_rate_host: str = "+2%"
    tts_pitch: str = "+0Hz"
    tts_max_concurrency: int = 1                  # M1 串行;上限 3
    tts_chunk_max_retries: int = 5

    # ---- 音频后处理(docs/03 §4)----
    loudness_i: float = -16.0       # LUFS,播客常用
    loudness_tp: float = -1.5
    loudness_lra: float = 11.0
    silence_between_chunks_ms: int = 100   # 同角色段间停顿
    chapter_titles: str = "回顾与开场|核心知识点|高频面试问法|答题框架|易错点与自测"
    audio_artist: str = "DriveCast"
    audio_cover_path: Path = PROJECT_ROOT / "static" / "assets" / "cover.png"

    # ---- M6 质量校验/校准(只给建议,绝不回写 .env) ----
    quality_min_minutes: float = 8.0
    quality_max_minutes: float = 22.0
    quality_duration_error_pct: float = 10.0
    quality_max_peak_db: float = -0.5
    speech_rate_history_window: int = 20
    speech_rate_min_samples: int = 3

    # ---- 内容(docs/01 §2.1 / 03 §6.1)----
    target_minutes: int = 15
    words_per_minute: int = 300     # rate=-5% 实测 280-320,合成后 ffprobe 回填校准
    default_episode_count: int = 6

    # ---- Web(docs/04 §5 / docs/05 M3) ----
    web_host: str = "127.0.0.1"          # 仅绑本地,无鉴权单用户
    web_port: int = 8080
    static_dir: Path = PROJECT_ROOT / "static"
    sse_poll_interval_sec: float = 2.0
    worker_poll_interval_sec: float = 1.0
    worker_enabled: bool = True                 # 验收/维护时用 WORKER_ENABLED=false 关闭消费
    worker_stale_running_sec: int = 3600        # running 超过该时长视为孤儿,空轮询时复位 pending

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
