"""
AbrigoQR — Diagnostico de setup.
Roda antes de tentar deploy / rodar o worker pra detectar configuracao quebrada.

Uso:
    python validate_setup.py
"""

from __future__ import annotations
import importlib, os, re, sys
from pathlib import Path

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"

def ok(msg):    print(f"{GREEN}[OK]{RESET}    {msg}")
def fail(msg):  print(f"{RED}[FAIL]{RESET}  {msg}")
def warn(msg):  print(f"{YELLOW}[WARN]{RESET}  {msg}")
def info(msg):  print(f"{BLUE}[INFO]{RESET}  {msg}")

problems: list[str] = []
warnings: list[str] = []


# ─── 1) Estrutura de arquivos ─────────────────────────────
ARQUIVOS = [
    "app.py",
    "rpa_nfp.py",
    "local_rpa_worker.py",
    "requirements.txt",
    "requirements-cloud.txt",
    "render.yaml",
    "Dockerfile",
    "mobile/www/index.html",
    "mobile/www/config.js",
    "mobile/capacitor.config.json",
    "mobile/package.json",
]

print("\n=== 1) Estrutura de arquivos ===")
for f in ARQUIVOS:
    if Path(f).exists():
        ok(f"Arquivo: {f}")
    else:
        fail(f"Arquivo: {f}")
        problems.append(f"Faltando: {f}")


# ─── 2) Variaveis de ambiente ─────────────────────────────
print("\n=== 2) .env ===")
env_path = Path(".env")
if not env_path.exists():
    fail(".env nao existe. Copie .env.exemplo como .env.")
    problems.append("Crie .env (copie de .env.exemplo)")
else:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        warn("python-dotenv nao instalado, lendo .env manualmente")

    OBRIGATORIAS = {
        "ENTIDADE_NOME":  None,
        "ENTIDADE_CNPJ":  re.compile(r"^\d{14}$"),
        "ENTIDADE_LABEL": None,
    }
    OPCIONAIS = {
        "ANTHROPIC_API_KEY": re.compile(r"^sk-ant-"),
        "RPA_TOKEN":         None,
        "DATABASE_URL":      None,
        "CLOUD_API_URL":     re.compile(r"^https?://"),
    }

    for var, pat in OBRIGATORIAS.items():
        v = os.getenv(var, "")
        if not v:
            fail(f"{var} nao configurado")
            problems.append(f"{var} ausente em .env")
        elif pat and not pat.match(v):
            fail(f"{var} invalido: {v!r}")
            problems.append(f"{var} formato invalido")
        else:
            display = v if len(v) < 50 else v[:47] + "..."
            ok(f"{var}: {display}")

    for var, pat in OPCIONAIS.items():
        v = os.getenv(var, "")
        if not v:
            warn(f"{var} nao configurado (opcional)")
            warnings.append(f"{var} ausente")
        elif pat and not pat.match(v):
            fail(f"{var} formato invalido")
            problems.append(f"{var} formato invalido")
        else:
            mask = (v[:8] + "..." if "KEY" in var or "TOKEN" in var else v)
            ok(f"{var}: {mask}")


# ─── 3) Dependencias Python ───────────────────────────────
print("\n=== 3) Dependencias Python ===")
PACOTES = {
    "fastapi":       "FastAPI (backend)",
    "uvicorn":       "Uvicorn (servidor)",
    "sqlalchemy":    "SQLAlchemy (ORM)",
    "anthropic":     "Anthropic SDK (IA Claude)",
    "httpx":         "httpx (worker HTTP)",
    "dotenv":        "python-dotenv",
}
PACOTES_RPA = {
    "playwright":    "Playwright (RPA local)",
}

for mod, desc in PACOTES.items():
    try:
        importlib.import_module(mod if mod != "dotenv" else "dotenv")
        ok(f"{desc}")
    except ImportError:
        fail(f"{desc}  → pip install {mod if mod != 'dotenv' else 'python-dotenv'}")
        problems.append(f"Pacote faltando: {mod}")

print()
for mod, desc in PACOTES_RPA.items():
    try:
        importlib.import_module(mod)
        ok(f"{desc}")
    except ImportError:
        warn(f"{desc} — necessario apenas pra rodar o RPA local")
        warnings.append(f"{mod} ausente (precisa pro RPA)")


# ─── 4) Modo de execucao ──────────────────────────────────
print("\n=== 4) Modo de execucao ===")
cloud_url = os.getenv("CLOUD_API_URL", "")
db_url    = os.getenv("DATABASE_URL", "sqlite:///./abrigoqr.db")

if cloud_url and "onrender.com" in cloud_url:
    info("Modo: WORKER LOCAL contra backend Render")
    info(f"  Backend: {cloud_url}")
    info("  Para rodar:  python local_rpa_worker.py")
elif db_url.startswith("postgres") or db_url.startswith("postgresql"):
    info("Modo: BACKEND CLOUD (Postgres)")
    info("  Para rodar:  uvicorn app:app --host 0.0.0.0 --port $PORT")
else:
    info("Modo: DESENVOLVIMENTO LOCAL (SQLite)")
    info(f"  DB: {db_url}")
    info("  Para rodar:  python -m uvicorn app:app --reload")


# ─── 5) Resumo ────────────────────────────────────────────
print("\n" + "=" * 50)
if not problems:
    print(f"{GREEN}TUDO OK!{RESET}  ", end="")
    if warnings:
        print(f"({len(warnings)} aviso(s))")
        for w in warnings:
            print(f"  - {w}")
    else:
        print()
    sys.exit(0)
else:
    print(f"{RED}{len(problems)} problema(s) encontrado(s):{RESET}")
    for p in problems:
        print(f"  - {p}")
    if warnings:
        print(f"\n{YELLOW}Avisos:{RESET}")
        for w in warnings:
            print(f"  - {w}")
    sys.exit(1)
