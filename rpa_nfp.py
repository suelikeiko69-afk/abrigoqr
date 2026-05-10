"""
AbrigoQR v4 — RPA Playwright
Suporta lançamento via:
  - Chave NFC-e de 44 dígitos (QR Code ou imagem)
  - COO + CNPJ + valor + data (lançamento manual/sem chave)

Melhorias v4:
  - Retry automático (até 3 tentativas)
  - Timeouts ajustados
  - Detecção de sucesso mais robusta
  - Screenshot tanto em erro quanto em sucesso
"""

import asyncio, logging, os, re
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

log = logging.getLogger("abrigoqr.rpa")

SESSAO_DIR   = Path("./sessao_nfp")
URL_PORTAL   = "https://www.nfp.fazenda.sp.gov.br"
URL_DOACAO   = URL_PORTAL + "/Entidades/DoacaoCupomFiscalSemCPF.aspx"
TIMEOUT_MS   = 30_000   # aumentado para 30s
MAX_TENTATIVAS = 3

# ─── Seletores (verifique com F12 se o portal atualizar) ──
SEL = {
    "chave_input":     'input[name="ctl00$ContentPlaceHolder1$txtChaveAcesso"]',
    "valor_input":     'input[name="ctl00$ContentPlaceHolder1$txtValor"]',
    "cnpj_input":      'input[name="ctl00$ContentPlaceHolder1$txtCNPJ"]',
    "data_input":      'input[name="ctl00$ContentPlaceHolder1$txtDataEmissao"]',
    "coo_input":       'input[name="ctl00$ContentPlaceHolder1$txtCOO"]',
    "entidade_select": 'select[name="ctl00$ContentPlaceHolder1$ddlEntidade"]',
    "btn_pesquisar":   'input[id*="btnPesquisar"], input[value*="Pesquisar"]',
    "btn_doar":        'input[type="submit"][value*="Doar"], input[id*="btnDoacao"]',
    # Mensagens de retorno do portal — testa múltiplas variações
    "msg_sucesso":     "#ctl00_ContentPlaceHolder1_lblMensagem, #lblMensagem, .msg-sucesso",
    "ja_logado":       "#lnkNomeUsuario, .nome-usuario, text=Sair",
}

PALAVRAS_SUCESSO = ("sucesso", "realizada", "registrada", "efetuada", "confirmada")
PALAVRAS_ERRO    = ("erro", "inválid", "não encontrada", "falha", "problema")


async def lancar_nota_nfp(nota, entidade_label: str):
    """
    Lança nota no portal NFP com retry automático.
    Detecta automaticamente se é por chave NFC-e ou por CNPJ+COO+data.
    """
    SESSAO_DIR.mkdir(exist_ok=True)
    tem_sessao = any(SESSAO_DIR.iterdir()) if SESSAO_DIR.exists() else False
    tem_chave  = _tem_chave_valida(nota.chave)

    ultimo_erro = None
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            await _tentar_lancamento(nota, entidade_label, tem_sessao, tem_chave)
            return  # sucesso
        except Exception as e:
            ultimo_erro = e
            log.warning(f"⚠ Tentativa {tentativa}/{MAX_TENTATIVAS} falhou: {e}")
            if tentativa < MAX_TENTATIVAS:
                espera = 5 * tentativa  # backoff: 5s, 10s
                log.info(f"⏳ Aguardando {espera}s antes de nova tentativa...")
                await asyncio.sleep(espera)

    raise Exception(f"Falhou após {MAX_TENTATIVAS} tentativas. Último erro: {ultimo_erro}")


async def _tentar_lancamento(nota, entidade_label: str, tem_sessao: bool, tem_chave: bool):
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(SESSAO_DIR),
            headless=tem_sessao,
            slow_mo=600 if not tem_sessao else 300,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        try:
            log.info(f"🤖 Portal NFP — {'chave NFC-e' if tem_chave else 'CNPJ+COO+data'}")
            await page.goto(URL_PORTAL, timeout=TIMEOUT_MS, wait_until="domcontentloaded")

            # Login
            if not await _logado(page):
                if tem_sessao:
                    log.warning("⚠ Sessão expirada — abrindo navegador para relogin")
                    await browser.close()
                    await _forcar_relogin(p, nota, entidade_label)
                    return
                else:
                    await _aguardar_login(page)

            # Navega para doação
            await page.goto(URL_DOACAO, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

            if tem_chave:
                await _preencher_por_chave(page, nota)
            else:
                await _preencher_por_campos(page, nota)

            # Seleciona entidade
            await _selecionar_entidade(page, entidade_label)

            # Clica Doar
            await page.click(SEL["btn_doar"], timeout=TIMEOUT_MS)

            # Aguarda confirmação
            msg = await _aguardar_sucesso(page)
            log.info(f"🎉 Nota lançada com sucesso! Retorno: {msg[:80] if msg else 'OK'}")

            # Screenshot de confirmação
            try:
                await page.screenshot(path=f"ok_{str(nota.chave)[:10]}.png")
            except Exception:
                pass

        except Exception as e:
            try:
                await page.screenshot(path=f"erro_{str(nota.chave)[:10]}.png")
            except Exception:
                pass
            raise
        finally:
            await browser.close()


def _tem_chave_valida(chave: str) -> bool:
    if not chave:
        return False
    limpa = re.sub(r'\D', '', chave)
    return len(limpa) == 44


async def _preencher_por_chave(page, nota):
    """Fluxo com chave NFC-e de 44 dígitos."""
    chave = re.sub(r'\D', '', nota.chave)
    log.info(f"⌨ Preenchendo chave: {chave[:22]}...")
    await page.fill(SEL["chave_input"], chave)
    if nota.valor and nota.valor > 0:
        try:
            await page.fill(SEL["valor_input"], f"{nota.valor:.2f}".replace(".", ","))
        except Exception:
            pass
    try:
        await page.click(SEL["btn_pesquisar"], timeout=8000)
        await page.wait_for_timeout(2000)
    except PWTimeout:
        pass


async def _preencher_por_campos(page, nota):
    """Fluxo alternativo: CNPJ + data + COO + valor (sem chave NFC-e)."""
    log.info(f"⌨ Preenchendo por campos: CNPJ={nota.cnpj} COO={nota.coo}")
    cnpj = re.sub(r'\D', '', nota.cnpj)

    try:
        await page.fill(SEL["cnpj_input"], cnpj, timeout=8000)
        try:
            await page.click(SEL["btn_pesquisar"], timeout=6000)
            await page.wait_for_timeout(1500)
        except PWTimeout:
            pass
    except Exception:
        log.warning("⚠ Campo CNPJ não encontrado — tentando continuar")

    if nota.data:
        try:
            data_br = "/".join(reversed(nota.data.split("-")))  # YYYY-MM-DD → DD/MM/YYYY
            await page.fill(SEL["data_input"], data_br, timeout=6000)
        except Exception:
            pass

    if nota.coo:
        try:
            await page.fill(SEL["coo_input"], str(nota.coo), timeout=6000)
        except Exception:
            pass

    if nota.valor and nota.valor > 0:
        try:
            await page.fill(SEL["valor_input"], f"{nota.valor:.2f}".replace(".", ","))
        except Exception:
            pass

    try:
        await page.click(SEL["btn_pesquisar"], timeout=8000)
        await page.wait_for_timeout(2000)
    except PWTimeout:
        pass


async def _logado(page) -> bool:
    try:
        el = await page.wait_for_selector(SEL["ja_logado"], timeout=5000)
        return el is not None
    except PWTimeout:
        return False


async def _aguardar_login(page):
    log.info("👤 Aguardando login manual gov.br (até 3 min)...")
    await page.wait_for_selector(SEL["ja_logado"], timeout=180_000)
    log.info("✅ Login detectado!")


async def _forcar_relogin(p, nota, entidade_label):
    b = await p.chromium.launch_persistent_context(
        str(SESSAO_DIR), headless=False, slow_mo=600,
        viewport={"width": 1280, "height": 900}
    )
    pg = b.pages[0] if b.pages else await b.new_page()
    await pg.goto(URL_PORTAL, timeout=TIMEOUT_MS)
    await _aguardar_login(pg)
    await b.close()
    await lancar_nota_nfp(nota, entidade_label)


async def _selecionar_entidade(page, label: str):
    """Seleciona entidade no dropdown — tenta match exato, depois parcial."""
    try:
        await page.select_option(SEL["entidade_select"], label=label, timeout=10_000)
        log.info(f"✅ Entidade selecionada (exato): {label}")
        return
    except Exception:
        pass

    try:
        opts = await page.eval_on_selector_all(
            f'{SEL["entidade_select"]} option',
            "os => os.map(o => ({v: o.value, t: o.textContent.trim()}))"
        )
        # Match parcial — ignora maiúsculas/minúsculas e acentos aproximados
        label_lower = label.lower()
        m = next(
            (o for o in opts if label_lower in o["t"].lower() or o["t"].lower() in label_lower),
            None
        )
        if m:
            await page.select_option(SEL["entidade_select"], value=m["v"])
            log.info(f"✅ Entidade selecionada (parcial): {m['t']}")
        else:
            disponiveis = [o["t"] for o in opts if o["v"]]
            log.warning(f"⚠ Entidade '{label}' não encontrada. Disponíveis: {disponiveis}")
            raise Exception(
                f"Entidade '{label}' não encontrada no portal. "
                f"Verifique ENTIDADE_LABEL no .env. Disponíveis: {disponiveis}"
            )
    except Exception as e:
        raise Exception(f"Não foi possível selecionar a entidade: {e}")


async def _aguardar_sucesso(page) -> str:
    """Aguarda mensagem de retorno do portal e valida se é sucesso."""
    await page.wait_for_timeout(2000)

    # Tenta ler mensagem do elemento de retorno
    try:
        await page.wait_for_selector(SEL["msg_sucesso"], timeout=20_000)
        msg = (await page.inner_text(SEL["msg_sucesso"])).strip()

        if any(p in msg.lower() for p in PALAVRAS_SUCESSO):
            return msg
        if any(p in msg.lower() for p in PALAVRAS_ERRO):
            raise Exception(f"Portal retornou erro: {msg}")
        # Mensagem ambígua — registra e segue
        log.warning(f"⚠ Resposta do portal não reconhecida: {msg}")
        return msg

    except PWTimeout:
        # Fallback: verifica se ainda está na página de doação (indica falha)
        url_atual = page.url
        if "DoacaoCupomFiscal" in url_atual:
            raise Exception("Timeout aguardando confirmação — possível falha no lançamento")
        # Redirecionou para outra página — considera sucesso
        log.info(f"ℹ Redirecionado para: {url_atual} — assumindo sucesso")
        return "ok (redirecionamento)"
