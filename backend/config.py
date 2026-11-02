"""Runtime settings, read from environment variables (or a local .env file)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# AI model provider: "anthropic", "openai_compat" (Gemini / Groq / Ollama / OpenRouter ...) or "none".
# "auto" picks anthropic when ANTHROPIC_API_KEY is set, openai_compat when LLM_API_KEY/LLM_BASE_URL is set.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "low")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

if LLM_PROVIDER == "auto":
    if ANTHROPIC_API_KEY:
        LLM_PROVIDER = "anthropic"
    elif LLM_BASE_URL and LLM_MODEL:
        LLM_PROVIDER = "openai_compat"
    else:
        LLM_PROVIDER = "none"

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))
MAX_EVIDENCE = int(os.getenv("MAX_EVIDENCE", "24"))
FETCH_ARTICLE_BODIES = int(os.getenv("FETCH_ARTICLE_BODIES", "6"))
DB_PATH = Path(os.getenv("TRUTHTRACE_DB", str(DATA_DIR / "truthtrace.db")))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 TruthTrace/1.0"
)
