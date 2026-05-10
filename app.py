"""
AbrigoQR v4 — Backend FastAPI (cloud-ready)

Arquitetura híbrida:
  • Backend roda em nuvem (Render) — só recebe notas, salva no Postgres.
  • RPA Playwright roda LOCAL (sua máquina) — faz polling em /api/notas/pendentes
    e lança no portal NFP via local_rpa_worker.py.
  • Frontend é o app mobile Capacitor (Android/iOS) — fala via REST.

Endpoints:
  GET  /api/config                — config pública (entidade, versão)
  GET  /api/status                — health check
  POST /lancar-nota               — registra nota (status=pendente)
  POST /analisar-imagem           — Claude Vision extrai dados de cupom
  GET  /api/notas                 — histórico
  GET  /api/nota/{id}/status      — status de uma nota (polling do app)
  GET  /api/notas/pendentes       — [RPA] pega fila pendente (auth)
  PATCH /api/nota/{id}/status     — [RPA] atualiza status após processar (auth)
  CRUD /api/estabelecimentos      — cadastro de estabelecimentos

Variáveis de ambiente:
  DATABASE_URL       — sqlite:///./abrigoqr.db (default) ou postgres://...
  ANTHROPIC_API_KEY  — chave do Claude Vision
  RPA_TOKEN          — secret compartilhado com o worker RPA local
  CORS_ORIGINS       — lista separada por vírgula (* em dev)
  PORT/PORTA         — porta do servidor (Render injeta PORT)
  ENTIDADE_NOME, ENTIDADE_CNPJ, ENTIDADE_LABEL — dados da entidade
"""

import logging, os, re, json, asyncio
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, validator
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from dotenv import load_dotenv
import anthropic

# RPA é opcional — só carrega se Playwright + módulo estiverem disponíveis
# (no cloud Render não instalamos Playwright, então cai aqui)
try:
    from rpa_nfp import lancar_nota_nfp
    RPA_DISPONIVEL = True
except Exception as _rpa_err:
    RPA_DISPONIVEL = False
    lancar_nota_nfp = None

# ─── CONFIG ───────────────────────────────────────────────
load_dotenv()

ENTIDADE_NOME  = os.getenv("ENTIDADE_NOME",  "Lar dos Idosos São Francisco")
ENTIDADE_CNPJ  = os.getenv("ENTIDADE_CNPJ",  "12345678000199")
ENTIDADE_LABEL = os.getenv("ENTIDADE_LABEL", "NOME DA SUA ENTIDADE AQUI")
PORTA          = int(os.getenv("PORT", os.getenv("PORTA", 8000)))
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
DATABASE_URL   = os.getenv("DATABASE_URL", "sqlite:///./abrigoqr.db")
RPA_TOKEN      = os.getenv("RPA_TOKEN", "")
CORS_ORIGINS   = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

# Render entrega "postgres://" mas SQLAlchemy 2.x exige "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("abrigoqr")

# ─── BANCO ────────────────────────────────────────────────
engine_kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class NotaORM(Base):
    __tablename__ = "notas"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    chave       = Column(String, unique=True, nullable=False, index=True)
    cnpj        = Column(String)
    valor       = Column(Float)
    data_nota   = Column(String)
    coo         = Column(String)
    nome_emit   = Column(String)
    origem      = Column(String, default="qr")
    colaborador = Column(String, default="")
    status      = Column(String, default="pendente", index=True)
    tentativas  = Column(Integer, default=0)
    criado_em   = Column(DateTime, default=datetime.utcnow)
    atualizado  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EstabORM(Base):
    __tablename__ = "estabelecimentos"
    id        = Column(Integer, primary_key=True, autoincrement=True)
    nome      = Column(String, nullable=False)
    cnpj      = Column(String, unique=True, nullable=False)
    categoria = Column(String, default="Outro")
    criado_em = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(engine)
    db_label = DATABASE_URL.split("@")[-1].split("?")[0]
    log.info(f"✅ Banco inicializado ({db_label})")


# ─── APP ──────────────────────────────────────────────────
app = FastAPI(title="AbrigoQR v4", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

Path("static").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Mantém o Jinja só pra modo desktop legado (acesso via navegador no servidor local).
# No cloud isso não é usado — o app mobile consome direto a REST API.
from jinja2 import Environment, FileSystemLoader as _FSL
_jinja_env = None
if Path("templates/index.html").exists():
    _jinja_env = Environment(loader=_FSL("templates"), auto_reload=True, cache_size=0)


# ─── MODELOS Pydantic ─────────────────────────────────────
class Nota(BaseModel):
    chave:         str
    valor:         float = 0.0
    data:          str   = ""
    cnpj:          str   = "00000000000000"
    nome_emitente: str   = ""
    coo:           str   = ""
    origem:        str   = "qr"
    colaborador:   str   = ""

    @validator("chave")
    def val_chave(cls, v):
        limpa = re.sub(r"\D", "", v)
        if limpa and len(limpa) == 44:
            return limpa
        return v or "SEM-CHAVE"

    @validator("data", pre=True, always=True)
    def val_data(cls, v):
        return v or datetime.now().strftime("%Y-%m-%d")


class ImagemPayload(BaseModel):
    imagem_base64: str
    mime_type:     str = "image/jpeg"


class Estabelecimento(BaseModel):
    nome:      str
    cnpj:      str
    categoria: str = "Outro"

    @validator("cnpj")
    def val_cnpj(cls, v):
        limpa = re.sub(r"\D", "", v)
        if len(limpa) != 14:
            raise ValueError("CNPJ deve ter 14 dígitos")
        return limpa


class StatusUpdate(BaseModel):
    status: str  # "ok" | "erro" | "pendente"


# ─── AUTH (rotas do worker RPA) ───────────────────────────
def require_rpa_token(x_rpa_token: str = Header(default="")):
    if not RPA_TOKEN:
        raise HTTPException(503, "RPA_TOKEN não configurado no servidor")
    if x_rpa_token != RPA_TOKEN:
        raise HTTPException(401, "Token RPA inválido")


# ─── ROTAS ────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    init_db()
    log.info(f"🌿 AbrigoQR v4 — {ENTIDADE_NOME}")
    log.info(f"🤖 RPA local disponível: {RPA_DISPONIVEL}")


@app.get("/", response_class=HTMLResponse)
async def home():
    if _jinja_env is None:
        return HTMLResponse(
            "<h1>AbrigoQR API</h1>"
            "<p>Backend em modo cloud. Use o app mobile.</p>"
            f"<p>Versão 4.0.0 — entidade: {ENTIDADE_NOME}</p>"
        )
    return HTMLResponse(_jinja_env.get_template("index.html").render({
        "entidade_nome": ENTIDADE_NOME,
        "entidade_cnpj": fmt_cnpj(ENTIDADE_CNPJ),
    }))


@app.get("/api/config")
async def api_config():
    """Configuração pública usada pelo app mobile no startup."""
    return {
        "entidade_nome":  ENTIDADE_NOME,
        "entidade_cnpj":  fmt_cnpj(ENTIDADE_CNPJ),
        "entidade_label": ENTIDADE_LABEL,
        "versao":         "4.0.0",
        "rpa_disponivel": RPA_DISPONIVEL,
    }


# ── Lançar nota ──────────────────────────────────────────
@app.post("/lancar-nota")
async def lancar(nota: Nota, db: Session = Depends(get_db)):
    log.info(f"📥 Nota [{nota.origem}]: {str(nota.chave)[:22]}... CNPJ={nota.cnpj}")

    if db.query(NotaORM).filter_by(chave=nota.chave).first():
        return JSONResponse(status_code=409, content={
            "status":   "duplicata",
            "mensagem": "Esta nota já foi registrada.",
        })

    row = NotaORM(
        chave=nota.chave, cnpj=nota.cnpj, valor=nota.valor,
        data_nota=nota.data, coo=nota.coo, nome_emit=nota.nome_emitente,
        origem=nota.origem, colaborador=nota.colaborador, status="pendente",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Modo local (RPA carregado): roda inline em background.
    # Modo cloud: o local_rpa_worker.py vai puxar via /api/notas/pendentes.
    if RPA_DISPONIVEL:
        asyncio.create_task(_executar_rpa_local(nota))

    return {
        "status":   "sucesso",
        "mensagem": "Nota recebida! Será processada em breve.",
        "chave":    nota.chave,
        "id":       row.id,
    }


async def _executar_rpa_local(nota: Nota):
    try:
        await lancar_nota_nfp(nota, ENTIDADE_LABEL)
        _set_status(nota.chave, "ok")
        log.info(f"✅ RPA concluído: {str(nota.chave)[:22]}")
    except Exception as e:
        _set_status(nota.chave, "erro")
        log.error(f"❌ RPA erro ({str(nota.chave)[:22]}): {e}")


def _set_status(chave: str, status: str):
    db = SessionLocal()
    try:
        row = db.query(NotaORM).filter_by(chave=chave).first()
        if row:
            row.status = status
            row.tentativas = (row.tentativas or 0) + 1
            row.atualizado = datetime.utcnow()
            db.commit()
    finally:
        db.close()


# ── Endpoints do RPA worker remoto ────────────────────────
@app.get("/api/notas/pendentes", dependencies=[Depends(require_rpa_token)])
async def notas_pendentes(limite: int = 20, db: Session = Depends(get_db)):
    rows = (
        db.query(NotaORM).filter_by(status="pendente")
        .order_by(NotaORM.criado_em.asc()).limit(limite).all()
    )
    return [_nota_dict(r) for r in rows]


@app.patch("/api/nota/{nota_id}/status", dependencies=[Depends(require_rpa_token)])
async def atualizar_nota_status(
    nota_id: int,
    payload: StatusUpdate,
    db: Session = Depends(get_db),
):
    row = db.query(NotaORM).filter_by(id=nota_id).first()
    if not row:
        raise HTTPException(404, "Nota não encontrada")
    row.status = payload.status
    row.tentativas = (row.tentativas or 0) + 1
    row.atualizado = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


# ── Analisar imagem com IA ────────────────────────────────
@app.post("/analisar-imagem")
async def analisar_imagem(payload: ImagemPayload):
    if not ANTHROPIC_KEY:
        raise HTTPException(503, "ANTHROPIC_API_KEY não configurada no servidor")
    if len(payload.imagem_base64) > 10_000_000:
        raise HTTPException(400, "Imagem muito grande. Máximo 7.5 MB.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = """Você é um sistema especializado em leitura de cupons fiscais brasileiros.
Analise esta imagem de cupom fiscal (NFC-e, SAT-CF-e ou cupom ECF) e extraia os dados.

Retorne APENAS um objeto JSON válido com estes campos (sem markdown, sem ```json):
{
  "chave": "44 dígitos da chave de acesso NFC-e, ou vazio se não visível",
  "cnpj": "14 dígitos do CNPJ do emitente, sem pontuação",
  "valor": "valor total do cupom como número decimal, ex: 42.90",
  "data": "data de emissão no formato YYYY-MM-DD",
  "coo": "número COO/CCF/ECF do cupom, apenas dígitos",
  "nome_emitente": "nome/razão social do estabelecimento",
  "confianca": "alta, media ou baixa (sua confiança na extração)"
}

Se algum campo não for legível, retorne string vazia para esse campo.
Nunca invente dados. Retorne apenas o JSON."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": payload.mime_type,
                        "data": payload.imagem_base64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```json\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$",      "", raw, flags=re.MULTILINE)
        dados = json.loads(raw.strip())
        log.info(
            f"🤖 IA extraiu: CNPJ={dados.get('cnpj')} "
            f"valor={dados.get('valor')} confiança={dados.get('confianca')}"
        )
        return dados
    except json.JSONDecodeError as e:
        log.error(f"❌ IA retornou JSON inválido: {raw[:200] if 'raw' in dir() else '?'}")
        raise HTTPException(422, f"IA não conseguiu extrair dados estruturados: {e}")
    except anthropic.APIError as e:
        log.error(f"❌ Erro API Anthropic: {e}")
        raise HTTPException(502, f"Erro na API de IA: {e}")


# ── CRUD Estabelecimentos ─────────────────────────────────
@app.get("/api/estabelecimentos")
async def listar_estabs(q: str = "", db: Session = Depends(get_db)):
    query = db.query(EstabORM)
    if q:
        query = query.filter(EstabORM.nome.ilike(f"%{q}%"))
    return [_estab_dict(r) for r in query.order_by(EstabORM.nome).all()]


@app.post("/api/estabelecimentos")
async def criar_estab(e: Estabelecimento, db: Session = Depends(get_db)):
    if db.query(EstabORM).filter_by(cnpj=e.cnpj).first():
        raise HTTPException(409, "CNPJ já cadastrado")
    db.add(EstabORM(nome=e.nome, cnpj=e.cnpj, categoria=e.categoria))
    db.commit()
    return {"status": "ok", "mensagem": f"'{e.nome}' cadastrado com sucesso"}


@app.delete("/api/estabelecimentos/{cnpj}")
async def deletar_estab(cnpj: str, db: Session = Depends(get_db)):
    limpo = re.sub(r"\D", "", cnpj)
    db.query(EstabORM).filter_by(cnpj=limpo).delete()
    db.commit()
    return {"status": "ok"}


@app.get("/api/estabelecimentos/buscar/{nome}")
async def buscar_estab(nome: str, db: Session = Depends(get_db)):
    rows = (
        db.query(EstabORM).filter(EstabORM.nome.ilike(f"%{nome}%")).limit(10).all()
    )
    return [_estab_dict(r) for r in rows]


# ── Status e histórico ────────────────────────────────────
@app.get("/api/notas")
async def api_notas(limite: int = 100, db: Session = Depends(get_db)):
    rows = db.query(NotaORM).order_by(NotaORM.criado_em.desc()).limit(limite).all()
    total = db.query(NotaORM).count()
    ok    = db.query(NotaORM).filter_by(status="ok").count()
    pend  = db.query(NotaORM).filter_by(status="pendente").count()
    erro  = db.query(NotaORM).filter_by(status="erro").count()
    return {
        "notas":       [_nota_dict(r) for r in rows],
        "contadores":  {"t": total, "ok": ok, "pend": pend, "erro": erro},
        "entidade":    {"nome": ENTIDADE_NOME, "cnpj": fmt_cnpj(ENTIDADE_CNPJ)},
    }


@app.get("/api/nota/{nota_id}/status")
async def nota_status(nota_id: int, db: Session = Depends(get_db)):
    row = db.query(NotaORM).filter_by(id=nota_id).first()
    if not row:
        raise HTTPException(404, "Nota não encontrada")
    return {"status": row.status, "tentativas": row.tentativas}


@app.get("/api/status")
async def api_status(db: Session = Depends(get_db)):
    total = db.query(NotaORM).count()
    ok    = db.query(NotaORM).filter_by(status="ok").count()
    return {
        "status":         "ok",
        "versao":         "4.0.0",
        "entidade":       ENTIDADE_NOME,
        "total_notas":    total,
        "notas_ok":       ok,
        "rpa_disponivel": RPA_DISPONIVEL,
        "hora":           datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


# ─── HELPERS ──────────────────────────────────────────────
def fmt_cnpj(c: str) -> str:
    d = re.sub(r"\D", "", c)
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return c


def _nota_dict(r: NotaORM) -> dict:
    return {
        "id":          r.id,
        "chave":       r.chave,
        "cnpj":        r.cnpj,
        "valor":       r.valor,
        "data_nota":   r.data_nota,
        "coo":         r.coo,
        "nome_emit":   r.nome_emit,
        "origem":      r.origem,
        "colaborador": r.colaborador,
        "status":      r.status,
        "tentativas":  r.tentativas,
        "criado_em":   r.criado_em.isoformat() if r.criado_em else None,
        "atualizado":  r.atualizado.isoformat() if r.atualizado else None,
    }


def _estab_dict(r: EstabORM) -> dict:
    return {
        "id":        r.id,
        "nome":      r.nome,
        "cnpj":      r.cnpj,
        "categoria": r.categoria,
        "criado_em": r.criado_em.isoformat() if r.criado_em else None,
    }


# ─── ENTRY POINT ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORTA, reload=True, log_level="info")
