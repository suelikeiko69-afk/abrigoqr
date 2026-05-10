# AbrigoQR v4 — Guia Completo de Deploy

Tempo total: ~30 minutos. Você sai daqui com:
- Backend rodando em nuvem (Render + Postgres)
- App Android instalado nos celulares dos colaboradores
- RPA local processando notas no portal NFP

Estrutura híbrida:
```
COLABORADOR (celular) → BACKEND CLOUD (Render) → RPA LOCAL (seu PC) → PORTAL NFP
```

---

## Passo 0 — Pré-requisitos

| Necessário | Onde obter |
|---|---|
| Conta GitHub | https://github.com (já feito) |
| Conta Render (free) | https://render.com |
| Chave Anthropic API | https://console.anthropic.com → API Keys |
| Python 3.12+ | https://python.org |
| Node 20+ (só se for buildar APK local) | https://nodejs.org |

---

## Passo 1 — Configurar localmente (5 min)

```bash
git clone https://github.com/suelikeiko69-afk/abrigoqr.git
cd abrigoqr
cp .env.exemplo .env
# Edite .env com sua ANTHROPIC_API_KEY
pip install -r requirements.txt
playwright install chromium
python validate_setup.py
```

`validate_setup.py` deve mostrar `TUDO OK!`. Se reclamar, vá em [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Teste o backend localmente (opcional):
```bash
python -m uvicorn app:app --reload
# Abra http://localhost:8000/api/status
```

---

## Passo 2 — Deploy do backend no Render (5 min)

1. **Login** em https://render.com (use sua conta GitHub).
2. Clique em **New → Blueprint**.
3. Cole a URL do seu repo: `https://github.com/suelikeiko69-afk/abrigoqr`
4. Render lê `render.yaml` automaticamente. Confirme.
5. Preencha as env vars que ele perguntar:

   | Variável | Valor |
   |---|---|
   | `ANTHROPIC_API_KEY` | sua chave (`sk-ant-...`) |
   | `ENTIDADE_NOME` | `Casa de Amparo Para Idosos Bom Pastor` |
   | `ENTIDADE_CNPJ` | `05895268000152` |
   | `ENTIDADE_LABEL` | `CASA DE AMPARO PARA IDOSOS BOM PASTOR` |

6. Clique em **Apply**. O Render vai:
   - Criar o banco Postgres (free, 90 dias)
   - Buildar a imagem Python
   - Subir o serviço
   - **Gerar `RPA_TOKEN` automaticamente**

7. Após ~3 min, o serviço está no ar. Pegue:
   - **URL pública** (ex.: `https://abrigoqr-backend-xxxx.onrender.com`)
   - **`RPA_TOKEN`** (Dashboard → Environment → copie o valor)

8. **Teste:**
   ```
   curl https://abrigoqr-backend-xxxx.onrender.com/api/status
   ```
   Deve retornar `{"status":"ok","versao":"4.0.0",...}`.

---

## Passo 3 — Apontar o app pro backend (3 min)

Edite `mobile/www/config.js`:
```js
window.__BUILD_API_BASE__ = "https://abrigoqr-backend-xxxx.onrender.com";
```
(troque pela URL real do Render)

Commit + push:
```bash
git add mobile/www/config.js
git commit -m "chore: apontar app para backend Render"
git push
```

A cada push, o GitHub Actions roda `build-android.yml` e gera o APK. Aguarde ~6 min.

---

## Passo 4 — Baixar e instalar o APK (5 min)

1. Vá em **GitHub → Actions → último run** ([atalho](https://github.com/suelikeiko69-afk/abrigoqr/actions)).
2. Aguarde o checkmark verde.
3. Em **Artifacts**, baixe `abrigoqr-debug-apk`. Vem em ZIP.
4. Extraia e transfira `app-debug.apk` para o celular (cabo USB, Google Drive, WhatsApp pra você mesmo).
5. No celular: abra o APK. Vai pedir pra habilitar **"Instalar de fonte desconhecida"** — autorize só pro app de origem (ex.: Drive).
6. Após instalar, abra o AbrigoQR. Conceda permissão de câmera quando pedir.
7. Tela inicial deve mostrar **"Casa de Amparo Para Idosos Bom Pastor"**. Se mostrar "Sem conexão", veja [TROUBLESHOOTING.md → app não conecta](TROUBLESHOOTING.md).

---

## Passo 5 — Rodar o RPA local (5 min)

O RPA é o que efetivamente lança a nota no portal NFP. Roda no seu PC.

Atualize o `.env` adicionando:
```env
CLOUD_API_URL=https://abrigoqr-backend-xxxx.onrender.com
RPA_TOKEN=cole_o_token_que_o_render_gerou
POLL_INTERVAL=60
```

Rode:
```bash
python local_rpa_worker.py
```

Na primeira execução, o Playwright abre o Chromium e pede pra você fazer login no portal NFP com seu certificado/CPF. Os cookies ficam salvos em `sessao_nfp/` e nas próximas vezes ele entra direto.

Deixe rodando em background. Cada 60s ele verifica se há notas pendentes no Render e processa.

**Dica:** rode dentro de um terminal `tmux`/`screen` ou crie uma tarefa agendada do Windows pra rodar no boot.

---

## Passo 6 — Teste integrado (5 min)

1. No celular, abra o app.
2. Use a aba **Manual** e preencha uma nota fictícia:
   - Chave: `35240101234567000189590010000000011000000099`
   - CNPJ: `01234567000189`
   - Valor: `10,00`
   - Data: hoje
3. Toque em **Enviar**. Status deve mostrar "Recebida".
4. Volte ao seu PC, no terminal do `local_rpa_worker.py`. Em até 60s ele deve logar:
   ```
   📥 1 nota(s) pendente(s)
   ➜ Processando nota id=1...
   ✅ id=1 concluida
   ```
5. No app, na aba **Histórico**, a nota deve aparecer com status ✅.

Se chegou aqui, está no ar. 🎉

---

## Passo 7 (opcional) — iOS via Codemagic

1. Crie conta em https://codemagic.io (login com GitHub).
2. **Add app → Capacitor → seu repo**.
3. Codemagic detecta o `codemagic.yaml`.
4. Pra build de simulador (debug), basta clicar **Start build**.
5. Pra App Store, configure App Store Connect API Key + Apple Developer ($99/ano).

500 min/mês grátis em runners macOS. Suficiente pra builds esporádicos.

---

## Custos mensais

| Item | Custo |
|---|---|
| Render web service | Grátis (free tier dorme após 15 min sem uso, sobe em ~30s no próximo request) |
| Render Postgres | Grátis (90 dias, depois precisa renovar ou migrar) |
| GitHub Actions | Grátis (repo público, 2000 min/mês privado) |
| Codemagic (iOS) | Grátis (500 min/mês) |
| Claude Vision (Anthropic) | ~US$ 0,003 por foto de cupom |
| **Total** | **< US$ 1/mês** com uso moderado |

---

## Próximos passos (opcionais)

- [ ] Customizar logo e cores em `mobile/www/index.html`
- [ ] Configurar nome "AbrigoQR" → nome real da entidade no `mobile/android/app/src/main/res/values/strings.xml`
- [ ] Subir APK assinado de release pra distribuir (vide `mobile/README.md`)
- [ ] Configurar tarefa agendada do Windows pra rodar `local_rpa_worker.py` no boot
- [ ] Backup periódico do Postgres do Render (Render → Database → Backups)

Boa sorte e boas doações. 🌿
