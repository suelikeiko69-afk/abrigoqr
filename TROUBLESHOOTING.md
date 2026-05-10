# AbrigoQR — Troubleshooting

Antes de mais nada, rode `python validate_setup.py` — ele detecta a maioria dos problemas.

---

## Erros mais comuns

### 1. `python-dotenv could not parse statement starting at line N`

**Sintoma:** warning ao iniciar `app.py` ou `local_rpa_worker.py`.

**Causa:** linhas no `.env` com caracteres especiais sem aspas (acentos, parênteses, hashtags no meio do valor).

**Fix:** abra `.env` e ponha **valores em aspas duplas**:
```env
ENTIDADE_NOME="Casa de Amparo Para Idosos Bom Pastor"
```

Não impede o app de funcionar, mas é confuso nos logs.

---

### 2. `ANTHROPIC_API_KEY não configurada` ao tirar foto

**Sintoma:** ao usar a aba "IA" e enviar foto, retorna erro 503.

**Fix:** adicione no `.env` (local) ou no Dashboard do Render (cloud):
```env
ANTHROPIC_API_KEY=sk-ant-xxxxxx
```
Restart o serviço (Render → Manual Deploy → Clear Build Cache & Deploy).

---

### 3. App mobile mostra "⚠ Sem conexão com o servidor"

**Causas possíveis:**

a) `mobile/www/config.js` ainda aponta pra `localhost:8000`.
   - Edite com a URL do Render e dê push (Actions rebuilda o APK).

b) Render está em sleep (free tier dorme após 15 min sem uso).
   - Primeiro request demora ~30s pra acordar. Tente de novo.

c) CORS bloqueado.
   - Confira `CORS_ORIGINS=*` no Dashboard do Render.

d) URL errada no app.
   - Toque longo no card "Entidade beneficiada" → digite a URL correta.

---

### 4. APK instala mas câmera não abre / QR não escaneia

**Causas:**

a) Permissão de câmera negada.
   - Configurações → Apps → AbrigoQR → Permissões → Câmera = Permitir.

b) WebView desatualizada (Android < 10).
   - Play Store → busca "Android System WebView" → Atualizar.

c) HTTPS faltando.
   - Câmera só funciona via HTTPS ou via origem `capacitor://localhost`.
     Se você acessa por `http://192.168.x.x:8000`, navegador bloqueia.
     Build APK pra produção (https://) que funciona normal.

---

### 5. GitHub Actions falha em "Build Debug APK"

**Sintoma:** workflow vermelho, log mostra erro Gradle.

**Fix comum:** abra o workflow log → procure por `FAILURE`. Casos típicos:

- **`SDK location not found`** → o setup-android@v3 falhou. Re-rode o workflow.
- **`Could not resolve com.android.tools.build:gradle`** → cache de Gradle corrompido. Adicione `cache: gradle` no setup-java step.
- **`Out of memory`** → adicione no `mobile/android/gradle.properties`:
  ```
  org.gradle.jvmargs=-Xmx4g
  ```

---

### 6. RPA worker retorna `401 Token RPA inválido`

**Causa:** o `RPA_TOKEN` no `.env` local não bate com o do Render.

**Fix:**
1. Render → Service → Environment → copie o valor exato de `RPA_TOKEN`.
2. Cole no seu `.env` local (sem espaços extras, sem aspas).
3. Restart `local_rpa_worker.py`.

---

### 7. RPA worker retorna `503 RPA_TOKEN não configurado no servidor`

**Causa:** Render não tem `RPA_TOKEN` no environment.

**Fix:** Render → Environment → adicione `RPA_TOKEN` (Render gera um quando lê `render.yaml`, mas se você criou o serviço manualmente sem blueprint, talvez não tenha gerado).

---

### 8. Playwright trava em "Aguardando login no portal NFP"

**Sintoma:** primeira execução do `local_rpa_worker.py` ou `rpa_nfp.py` abre Chromium e fica parado.

**Fix:** o portal NFP exige login com certificado digital ou senha gov.br. Ele NÃO vai prosseguir até você logar manualmente. Faça o login no Chromium aberto, aguarde a sessão ser salva em `sessao_nfp/`, fechar o Chromium. Próximas execuções pegam o cookie.

Se a sessão expirou (~30 dias), apague `sessao_nfp/` e logue de novo.

---

### 9. Postgres do Render expirou (após 90 dias)

**Sintoma:** após 90 dias do deploy, todos os requests falham com erro de conexão ao DB.

**Fix:**
- **Opção A:** Render → Database → Renew (free dura mais 90 dias).
- **Opção B:** Migrar pra plano pago ($7/mês) ou outro provedor (Supabase free, Neon free).
- **Opção C:** Exportar dump:
  ```bash
  pg_dump $DATABASE_URL > backup.sql
  ```

---

### 10. iOS build no Codemagic falha em `pod install`

**Causa típica:** versão do CocoaPods desatualizada.

**Fix:** no `codemagic.yaml`, antes do `pod install`:
```yaml
- name: Update CocoaPods
  script: |
    sudo gem install cocoapods
    pod repo update
```

---

## FAQ

### 1. Posso rodar tudo localmente sem o Render?

Sim. Não preencha `CLOUD_API_URL` no `.env`, deixe `DATABASE_URL=sqlite:///./abrigoqr.db`, rode `uvicorn app:app --reload`. O backend serve a UI legada via `/` (Jinja). Mas o app mobile precisa de uma URL acessível na rede — use `http://SEU_IP_LOCAL:8000` no `config.js`.

### 2. O Render free tier dorme. Como evitar?

a) Use um pinger (ex.: https://uptimerobot.com — grátis, ping a cada 5 min).
b) Migre pro plano Starter ($7/mês) que não dorme.
c) Aceite o cold start de ~30s. Pra um app interno do abrigo, é OK.

### 3. Como atualizar o app mobile sem republicar?

Mude `mobile/www/`, dê push. Actions rebuilda. Baixe o novo APK. Usuários precisam reinstalar (ou usa Capacitor Live Updates pra hot-reload, mas é pago).

### 4. Posso colocar o app na Play Store?

Sim. Você precisa:
- Conta de desenvolvedor Google ($25 único)
- APK de release assinado (vide `mobile/README.md`)
- Política de privacidade pública
- Screenshots e descrição

### 5. Quantas notas o sistema aguenta?

Backend FastAPI: dezenas de milhares de notas/dia tranquilamente.
Postgres free: 1 GB de armazenamento (~3M de registros de nota).
Gargalo real: o RPA local tem que processar uma a uma (~30s/nota), então ~120/hora.

### 6. Preciso de certificado digital pra rodar o RPA?

Sim, o portal NFP exige. Use seu certificado A1 (arquivo) ou A3 (token), ou login gov.br.

### 7. Como adicionar mais entidades beneficiadas?

A versão atual suporta UMA entidade por deploy (define em `ENTIDADE_*`). Pra multi-entidade, seria preciso refatorar `Nota` pra ter `entidade_id` e duplicar `lancar_nota_nfp()`. Não está no escopo atual.

### 8. Posso usar outro modelo que não Claude pra ler cupons?

Sim. Em `app.py`, função `analisar_imagem()`, troque a chamada `client.messages.create(...)` pela API que preferir (OpenAI Vision, Gemini Vision, Tesseract OCR, etc). Adapte o parsing do retorno.

### 9. Os colaboradores precisam criar conta?

Não. Por padrão, o app só guarda o nome do colaborador localmente (campo "colaborador"). Não há autenticação. Se quiser controle, adicione `APP_TOKEN` em `app.py` e exija no header de `/lancar-nota`.

### 10. Como faço backup do banco?

**Render Postgres:** vai em Dashboard → Database → Backups → Create. Ou via CLI:
```bash
pg_dump $DATABASE_URL > backup_$(date +%Y%m%d).sql
```

**SQLite local:** apenas copie o arquivo `abrigoqr.db`.

### 11. Como dou Ctrl+C no `local_rpa_worker.py` sem corromper estado?

O worker já trata `SIGINT` — termina o lote atual e sai limpo. Pode dar Ctrl+C tranquilo.

### 12. O app funciona offline?

Parcialmente. A interface carrega offline (PWA), mas:
- `/lancar-nota` precisa de internet (envia pro backend).
- `/analisar-imagem` precisa de internet (chama Claude).
- Histórico local funciona offline (localStorage do navegador).

Se o colaborador escaneia um QR offline, o app guarda em "fila local" e tenta reenviar quando voltar online (ver `historico` no localStorage).

---

## Diagnóstico rápido

Cheat sheet de comandos:

```bash
# Tudo OK?
python validate_setup.py

# Backend local responde?
curl http://localhost:8000/api/status

# Backend Render responde?
curl https://abrigoqr-backend-xxxx.onrender.com/api/status

# Tem notas pendentes?
curl -H "x-rpa-token: SEU_TOKEN" \
  https://abrigoqr-backend-xxxx.onrender.com/api/notas/pendentes

# RPA local consegue se conectar?
python local_rpa_worker.py
# (deve mostrar "Backend status: ok v4.0.0")

# Capacitor sincronizado?
cd mobile && npx cap sync && cd ..
```

Se nada disso resolver, abra uma issue em https://github.com/suelikeiko69-afk/abrigoqr/issues com o output de `validate_setup.py` e a mensagem de erro completa.
