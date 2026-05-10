"""
AbrigoQR v4 — Worker RPA Local

Roda na sua maquina (com Playwright instalado) e processa as notas que o
backend cloud salvou como 'pendente'. Polling: GET /api/notas/pendentes,
para cada uma executa lancar_nota_nfp() e faz PATCH /api/nota/{id}/status.

Uso:
    python local_rpa_worker.py

Config (no mesmo .env do app.py):
    CLOUD_API_URL    — URL do backend Render (ex: https://abrigoqr-backend.onrender.com)
    RPA_TOKEN        — secret compartilhado com o backend
    ENTIDADE_LABEL   — label da entidade no portal NFP
    POLL_INTERVAL    — segundos entre cada polling (default 60)

Para parar: Ctrl+C
"""

import asyncio, logging, os, signal, sys
from types import SimpleNamespace
from datetime import datetime

import httpx
from dotenv import load_dotenv

from rpa_nfp import lancar_nota_nfp

# ─── CONFIG ───────────────────────────────────────────────
load_dotenv()

CLOUD_API_URL  = os.getenv("CLOUD_API_URL", "http://localhost:8000").rstrip("/")
RPA_TOKEN      = os.getenv("RPA_TOKEN", "")
ENTIDADE_LABEL = os.getenv("ENTIDADE_LABEL", "")
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", 60))
LIMITE_LOTE    = int(os.getenv("LIMITE_LOTE", 5))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("abrigoqr.worker")

if not RPA_TOKEN:
    log.error("RPA_TOKEN nao configurado no .env. Abortando.")
    sys.exit(1)
if not ENTIDADE_LABEL:
    log.error("ENTIDADE_LABEL nao configurado no .env. Abortando.")
    sys.exit(1)


# ─── CLIENTE HTTP ─────────────────────────────────────────
HEADERS = {"x-rpa-token": RPA_TOKEN, "User-Agent": "abrigoqr-worker/1.0"}


async def buscar_pendentes(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"{CLOUD_API_URL}/api/notas/pendentes",
        params={"limite": LIMITE_LOTE},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


async def marcar_status(client: httpx.AsyncClient, nota_id: int, status: str):
    r = await client.patch(
        f"{CLOUD_API_URL}/api/nota/{nota_id}/status",
        json={"status": status},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()


# ─── LOOP PRINCIPAL ───────────────────────────────────────
def _to_nota_obj(d: dict) -> SimpleNamespace:
    """Converte o dict da API no objeto que rpa_nfp.lancar_nota_nfp espera."""
    return SimpleNamespace(
        chave         = d.get("chave", ""),
        valor         = d.get("valor", 0.0) or 0.0,
        data          = d.get("data_nota", "") or "",
        cnpj          = d.get("cnpj", "") or "",
        nome_emitente = d.get("nome_emit", "") or "",
        coo           = d.get("coo", "") or "",
        origem        = d.get("origem", "qr") or "qr",
        colaborador   = d.get("colaborador", "") or "",
    )


async def processar_uma(client: httpx.AsyncClient, nota_dict: dict):
    nota_id = nota_dict["id"]
    chave_curta = str(nota_dict.get("chave", ""))[:22]
    log.info(f"➜ Processando nota id={nota_id} chave={chave_curta}...")

    try:
        nota = _to_nota_obj(nota_dict)
        await lancar_nota_nfp(nota, ENTIDADE_LABEL)
        await marcar_status(client, nota_id, "ok")
        log.info(f"  ✅ id={nota_id} concluida")
    except Exception as e:
        log.error(f"  ❌ id={nota_id} erro: {e}")
        try:
            await marcar_status(client, nota_id, "erro")
        except Exception as e2:
            log.error(f"  ⚠️  falha ao reportar status: {e2}")


_parando = False

async def loop():
    global _parando
    log.info(f"🤖 Worker iniciado")
    log.info(f"   Backend:        {CLOUD_API_URL}")
    log.info(f"   Entidade:       {ENTIDADE_LABEL}")
    log.info(f"   Poll interval:  {POLL_INTERVAL}s")
    log.info(f"   Lote por ciclo: {LIMITE_LOTE}")

    async with httpx.AsyncClient() as client:
        # Health check inicial
        try:
            r = await client.get(f"{CLOUD_API_URL}/api/status", timeout=10)
            log.info(f"   Backend status: {r.json().get('status')} v{r.json().get('versao')}")
        except Exception as e:
            log.warning(f"   ⚠️  Backend nao respondeu no health check: {e}")

        while not _parando:
            try:
                pendentes = await buscar_pendentes(client)
            except httpx.HTTPStatusError as e:
                log.error(f"HTTP {e.response.status_code} ao buscar pendentes: {e.response.text[:200]}")
                pendentes = []
            except Exception as e:
                log.error(f"Erro ao buscar pendentes: {e}")
                pendentes = []

            if pendentes:
                log.info(f"📥 {len(pendentes)} nota(s) pendente(s)")
                for nota in pendentes:
                    if _parando:
                        break
                    await processar_uma(client, nota)
            else:
                log.debug("Nada pendente.")

            # Sleep com checagem de cancelamento
            for _ in range(POLL_INTERVAL):
                if _parando:
                    break
                await asyncio.sleep(1)

    log.info("👋 Worker encerrado")


def _handle_sigint(signum, frame):
    global _parando
    log.info("Sinal de interrupcao recebido, encerrando apos lote atual...")
    _parando = True


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigint)
    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        pass
