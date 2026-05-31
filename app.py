"""
AbrigoQR v4 — Backend FastAPI (cloud-ready)

Arquitetura híbrida:
  • Backend roda em nuvem (Render) — só recebe notas, salva no Postgres.
  • RPA Playwright roda LOCAL (sua máquina) — faz polling em /api/notas/pendentes
    e lança no portal NFP via local_rpa_worker.py.
  • Frontend é o app mobile Capacitor (Android/iOS) — fala via REST.

Endpoints:
  GET  /                          — UI legada (Jinja, modo desktop) ou redirect
  GET  /install                   — pagina amigavel com QR pra instalar o APK
  GET  /install/apk               — redirect pro APK do GitHub Releases
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

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
INSTALL_REPO   = os.getenv("INSTALL_REPO", "suelikeiko69-afk/abrigoqr")
INSTALL_TAG    = os.getenv("INSTALL_TAG",  "latest")

# Identificador do build esperado do APK. Quando o JS no APK detectar que
# seu SCANNER_BUILD nao bate com este valor, ele mostra um banner amarelo
# "Nova versao disponivel - Atualizar" -> direciona pro /install/apk.
# IMPORTANTE: atualizar este valor sempre que SCANNER_BUILD mudar em
# mobile/www/index.html. Os dois precisam ficar sincronizados.
LATEST_APP_BUILD = os.getenv("LATEST_APP_BUILD", "v6-resolution-fallback-2026.05.31")

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
        "entidade_nome":    ENTIDADE_NOME,
        "entidade_cnpj":    fmt_cnpj(ENTIDADE_CNPJ),
        "entidade_label":   ENTIDADE_LABEL,
        "versao":           "4.0.0",
        "rpa_disponivel":   RPA_DISPONIVEL,
        "latest_app_build": LATEST_APP_BUILD,
        "install_url":      f"/install",
    }


# ── Pagina de instalacao do APK (com QR) ─────────────────
@app.get("/install/apk")
async def install_apk():
    """Redireciona para o APK mais recente publicado como GitHub Release."""
    return RedirectResponse(
        f"https://github.com/{INSTALL_REPO}/releases/download/{INSTALL_TAG}/app-debug.apk",
        status_code=302,
    )


@app.get("/install", response_class=HTMLResponse)
async def install_page(request: Request):
    """Pagina amigavel pra colaborador instalar o app no celular."""
    install_url = str(request.url).split("?")[0].rstrip("/")
    apk_url     = f"{install_url}/apk"
    html = _INSTALL_HTML
    html = html.replace("__ENTIDADE__",   _esc(ENTIDADE_NOME))
    html = html.replace("__APK_URL__",    apk_url)
    html = html.replace("__INSTALL_URL__", install_url)
    html = html.replace("__VERSAO__",     "4.0.0")
    return HTMLResponse(html)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


_INSTALL_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instalar AbrigoQR</title>
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js"></script>
<style>
  :root { --v:#0A5C45; --vm:#0F7A5C; --vl:#D6F0E8; --vxl:#EDFAF5; --vd:#063D2E;
          --bg:#F5F1E8; --txt:#2A2A2A; --gray:#6B6B6B; }
  * { box-sizing: border-box; }
  html, body { margin:0; padding:0; }
  body { font-family:-apple-system,"Segoe UI",system-ui,sans-serif; background:var(--bg);
         color:var(--txt); line-height:1.5; min-height:100vh; }
  .wrap { max-width:560px; margin:0 auto; padding:24px 16px 80px; }
  header { text-align:center; padding:24px 16px 16px; }
  .logo { font-size:56px; line-height:1; }
  h1 { font-size:30px; margin:8px 0 4px; color:var(--vd); font-weight:700; letter-spacing:-.01em; }
  .ent { color:var(--vm); font-weight:500; font-size:15px; }
  .card { background:white; border-radius:18px; padding:24px; margin-bottom:14px;
          box-shadow:0 4px 20px rgba(10,92,69,.08); }
  .download { display:block; text-align:center; padding:18px 20px; border-radius:14px;
              text-decoration:none; font-size:18px; font-weight:600;
              background:linear-gradient(135deg,var(--v),var(--vm)); color:white;
              margin:14px 0; box-shadow:0 4px 14px rgba(10,92,69,.25);
              transition:transform .1s; }
  .download:active { transform:scale(.98); }
  .download.alt { background:#888; box-shadow:none; font-size:14px; padding:10px 14px; }
  .qr-wrap { display:flex; justify-content:center; padding:16px 0 8px; }
  .qr { padding:14px; background:white; border:3px solid var(--vl); border-radius:14px; }
  .qr img { display:block; width:240px; height:240px; }
  .info { font-size:13px; color:var(--gray); text-align:center; margin:6px 0 0; word-break:break-all; }
  h2 { color:var(--vd); font-size:18px; margin:0 0 14px; }
  .step { display:flex; gap:14px; margin:14px 0; align-items:flex-start; }
  .step-num { flex-shrink:0; width:32px; height:32px; background:var(--v); color:white;
              border-radius:50%; display:flex; align-items:center; justify-content:center;
              font-weight:700; font-size:14px; }
  .step-text { padding-top:5px; font-size:15px; }
  .step-text strong { color:var(--vd); }
  code { font-family:"SF Mono",Consolas,"DM Mono",monospace; background:#eee;
         padding:2px 6px; border-radius:4px; font-size:13px; }
  .badge { display:inline-block; background:var(--vl); color:var(--vd);
           padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600;
           margin-left:8px; }
  .desktop-only { display:block; }
  .mobile-only  { display:none;  }
  @media (max-width:600px) {
    .desktop-only { display:none;  }
    .mobile-only  { display:block; }
    h1 { font-size:26px; }
    .qr img { width:200px; height:200px; }
  }
  footer { text-align:center; font-size:12px; color:var(--gray); margin-top:24px; }
  footer a { color:var(--gray); }
</style>
</head>
<body>

<div class="wrap">

<header>
  <div class="logo">🌿</div>
  <h1>AbrigoQR</h1>
  <div class="ent">__ENTIDADE__</div>
</header>

<div class="card">
  <div class="mobile-only">
    <h2>Instalar no celular <span class="badge">Android</span></h2>
    <a class="download" href="__APK_URL__">📥 Baixar AbrigoQR</a>
    <p class="info">Após baixar, toque no arquivo para instalar.</p>
  </div>

  <div class="desktop-only">
    <h2>Aponte a câmera do celular para o QR Code</h2>
    <div class="qr-wrap">
      <div class="qr" id="qr"></div>
    </div>
    <p class="info">Ou compartilhe este link:<br><strong>__INSTALL_URL__</strong></p>
    <a class="download alt" href="__APK_URL__">Baixar APK direto (avançado)</a>
  </div>
</div>

<div class="card">
  <h2>Como instalar — passo a passo</h2>
  <div class="step"><div class="step-num">1</div><div class="step-text">Toque em <strong>Baixar AbrigoQR</strong> acima.</div></div>
  <div class="step"><div class="step-num">2</div><div class="step-text">Quando o download terminar, abra o arquivo <code>app-debug.apk</code> na notificação ou no app de Arquivos.</div></div>
  <div class="step"><div class="step-num">3</div><div class="step-text">O Android vai pedir <strong>"Permitir desta fonte"</strong> ou <strong>"Fontes desconhecidas"</strong> — autorize só pra este aplicativo (Chrome, Drive, etc).</div></div>
  <div class="step"><div class="step-num">4</div><div class="step-text">Toque em <strong>Instalar</strong> e aguarde.</div></div>
  <div class="step"><div class="step-num">5</div><div class="step-text">Abra o AbrigoQR e <strong>conceda permissão de câmera</strong> quando pedir — ela é usada pra ler os QR codes dos cupons.</div></div>
</div>

<div class="card">
  <h2>Dúvidas frequentes</h2>
  <p style="margin:6px 0;font-size:14px"><strong>iPhone funciona?</strong> Ainda não — só Android por enquanto. O app pra iOS está em desenvolvimento.</p>
  <p style="margin:6px 0;font-size:14px"><strong>É seguro?</strong> Sim, o aplicativo é gratuito, de código aberto e não coleta dados pessoais.</p>
  <p style="margin:6px 0;font-size:14px"><strong>Tela mostra "Sem conexão"?</strong> O servidor pode estar dormindo (acorda em ~30s). Toque longo no nome da entidade pra reconfigurar a URL se precisar.</p>
</div>

<footer>
  AbrigoQR v__VERSAO__ · <a href="/api/status">status do servidor</a>
</footer>

</div>

<script>
  // Gera QR apontando pra esta mesma pagina (pra desktop -> mobile)
  try {
    var qr = qrcode(0, 'M');
    qr.addData("__INSTALL_URL__");
    qr.make();
    document.getElementById('qr').innerHTML = qr.createImgTag(6, 8);
  } catch(e) { console.warn('QR fail:', e); }
</script>
</body>
</html>
"""


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


CLAUDE_MODELS_TENTATIVA = [
    os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5"),
    "claude-sonnet-4-5-20250929",
    "claude-3-5-sonnet-latest",
    "claude-3-5-sonnet-20241022",
]


# ── Diagnostico rapido da IA ────────────────────────────
@app.get("/api/test-ia")
async def test_ia():
    """Endpoint de diagnostico: confere se ANTHROPIC_API_KEY funciona
    e quais modelos respondem. Use no browser pra debugar."""
    result = {
        "anthropic_key_set":    bool(ANTHROPIC_KEY),
        "anthropic_key_format": (ANTHROPIC_KEY[:7] + "..." + ANTHROPIC_KEY[-4:]) if ANTHROPIC_KEY else None,
        "anthropic_key_len":    len(ANTHROPIC_KEY) if ANTHROPIC_KEY else 0,
        "tentativas":           [],
    }
    if not ANTHROPIC_KEY:
        result["erro"] = "ANTHROPIC_API_KEY nao configurada"
        return result
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    for modelo in CLAUDE_MODELS_TENTATIVA:
        tentativa = {"modelo": modelo}
        try:
            r = client.messages.create(
                model=modelo, max_tokens=10,
                messages=[{"role": "user", "content": "diga oi"}],
            )
            tentativa["ok"] = True
            tentativa["resposta"] = r.content[0].text[:50] if r.content else ""
            result["tentativas"].append(tentativa)
            result["modelo_funcionando"] = modelo
            break
        except Exception as e:
            tentativa["ok"]    = False
            tentativa["tipo"]  = type(e).__name__
            tentativa["erro"]  = str(e)[:300]
            result["tentativas"].append(tentativa)
    return result


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

    raw = ""
    ultimo_erro = None
    for modelo in CLAUDE_MODELS_TENTATIVA:
        try:
            response = client.messages.create(
                model=modelo,
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
            try:
                dados = json.loads(raw.strip())
            except json.JSONDecodeError as e:
                log.error(f"❌ IA retornou JSON invalido (modelo {modelo}): {raw[:200]}")
                raise HTTPException(422, f"IA nao estruturou os dados: {e}")
            log.info(
                f"🤖 IA ({modelo}) extraiu: CNPJ={dados.get('cnpj')} "
                f"valor={dados.get('valor')} confianca={dados.get('confianca')}"
            )
            return dados
        except anthropic.NotFoundError as e:
            log.warning(f"⚠ Modelo {modelo} nao encontrado: {e}")
            ultimo_erro = ("NotFoundError", modelo, str(e))
            continue
        except anthropic.AuthenticationError as e:
            log.error(f"❌ ANTHROPIC_API_KEY invalida: {e}")
            raise HTTPException(401, f"Chave Anthropic invalida: {str(e)[:200]}")
        except anthropic.PermissionDeniedError as e:
            log.error(f"❌ Permissao negada: {e}")
            raise HTTPException(403, f"IA sem permissao: {str(e)[:200]}")
        except anthropic.RateLimitError as e:
            log.warning(f"⏱ Rate limit em {modelo}: {e}")
            raise HTTPException(429, f"IA com rate limit: {str(e)[:200]}")
        except anthropic.APIConnectionError as e:
            log.error(f"❌ APIConnectionError em {modelo}: {e}")
            ultimo_erro = ("APIConnectionError", modelo, str(e))
            continue
        except anthropic.APIError as e:
            log.error(f"❌ APIError em {modelo}: type={type(e).__name__} msg={e}")
            ultimo_erro = (type(e).__name__, modelo, str(e))
            continue
        except Exception as e:
            log.error(f"❌ Erro inesperado em {modelo}: type={type(e).__name__} msg={e}")
            ultimo_erro = (type(e).__name__, modelo, str(e))
            continue

    # Se chegou aqui, todos os modelos falharam
    tipo, modelo, msg = ultimo_erro or ("Desconhecido", "?", "?")
    raise HTTPException(
        502,
        f"IA falhou em todos os modelos. Ultimo erro: {tipo} no modelo '{modelo}'. "
        f"Mensagem: {msg[:200]}. "
        f"Verifique /api/test-ia para diagnostico."
    )


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
