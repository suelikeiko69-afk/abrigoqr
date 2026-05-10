# AbrigoQR Mobile

App Capacitor (Android + iOS) que consome a REST API do backend AbrigoQR.

```
mobile/
├── www/                  ← frontend standalone (HTML/CSS/JS)
│   ├── index.html        ← UI principal
│   ├── config.js         ← URL do backend (substituir antes do build)
│   └── manifest.json     ← PWA manifest
├── android/              ← projeto Android nativo (gerado pelo Capacitor)
├── ios/                  ← projeto iOS nativo (gerado pelo Capacitor)
├── capacitor.config.json ← config do Capacitor
└── package.json
```

---

## 1) Antes de buildar — apontar pro seu backend

Edite `www/config.js` e troque a URL pelo seu backend Render:

```js
window.__BUILD_API_BASE__ = "https://abrigoqr-backend.onrender.com";
```

(O usuário também pode mudar em runtime fazendo **toque longo no card "Entidade beneficiada"**.)

Depois rode `npx cap sync` pra copiar o `www/` pros projetos nativos.

---

## 2) Build do APK — três caminhos

### A) Local com Android Studio (recomendado pra desenvolvimento)

**Pré-requisitos:**
- [Android Studio](https://developer.android.com/studio) (Hedgehog ou superior)
- JDK 21 (Capacitor 8 exige; Android Studio recente já vem com ele)

**Passos:**

```bash
cd mobile
npx cap sync android
npx cap open android      # abre o projeto no Android Studio
```

No Android Studio:
- `Build → Build Bundle(s) / APK(s) → Build APK(s)`
- O APK fica em `mobile/android/app/build/outputs/apk/debug/app-debug.apk`
- Transfira pro celular e instale (precisa habilitar "Fontes desconhecidas")

**Alternativa via terminal** (sem abrir o Android Studio):

```bash
cd mobile/android
./gradlew assembleDebug          # Linux/macOS
gradlew.bat assembleDebug        # Windows
```

### B) GitHub Actions (CI grátis, gera APK automaticamente)

Veja `.github/workflows/build-android.yml` (criado neste repo). A cada push pra `main`, o workflow:
1. Roda `npm install` em `mobile/`
2. Faz `cap sync android`
3. Builda o APK debug
4. Publica como artefato pra download

Após o primeiro push, baixe o APK em **GitHub → Actions → último run → Artifacts**.

### C) APK assinado pra distribuir (release)

```bash
cd mobile/android
./gradlew assembleRelease
```

Mas pra release você precisa de uma keystore. Gere uma vez:

```bash
keytool -genkey -v -keystore abrigoqr.keystore -alias abrigoqr \
  -keyalg RSA -keysize 2048 -validity 10000
```

E configure `mobile/android/app/build.gradle` (bloco `signingConfigs`) ou use [App Signing do Google Play](https://support.google.com/googleplay/android-developer/answer/9842756).

---

## 3) Build iOS — Codemagic (porque você está no Windows)

Você não consegue rodar Xcode no Windows, mas o [Codemagic](https://codemagic.io) oferece 500 minutos/mês grátis em runners macOS.

**Passos:**

1. Empurre o código pra um repositório GitHub (se ainda não fez).
2. Crie conta no Codemagic e conecte o GitHub.
3. Selecione o repositório, app type = **Capacitor**.
4. Em **Build → Workflow**, configure:
   - Platform: iOS
   - Build for: simulator (debug) ou App Store (release)
   - Build script:
     ```bash
     cd mobile
     npm ci
     npx cap sync ios
     cd ios/App
     pod install
     ```
   - Xcode build settings: scheme = `App`, configuration = `Release`
5. Pra publicar na App Store você precisa:
   - Conta Apple Developer ($99/ano)
   - Certificate + Provisioning Profile (Codemagic gerencia se você fornecer App Store Connect API Key)

**Alternativa:** se conhecer alguém com Mac, peça pra rodar:
```bash
cd mobile
npm install
npx cap sync ios
npx cap open ios   # abre Xcode
# No Xcode: Product → Archive → Distribute App
```

---

## 4) Permissões já configuradas

- **Android** (`android/app/src/main/AndroidManifest.xml`): `INTERNET`, `CAMERA`
- **iOS** (`ios/App/App/Info.plist`): `NSCameraUsageDescription`, `NSPhotoLibraryUsageDescription`

A primeira vez que o usuário escanear um QR ou tirar foto, o sistema pede permissão.

---

## 5) Workflow de desenvolvimento

```bash
# Editou algo em mobile/www/?
cd mobile && npx cap sync     # copia mudanças pros projetos nativos

# Vai testar no celular?
cd mobile/android
./gradlew installDebug         # builda + instala no celular conectado via USB
```

Pra debug, ative as DevTools do Chrome:
- Conecte o celular via USB (debug ativado)
- Abra `chrome://inspect` no Chrome do PC
- Inspect WebView do AbrigoQR

---

## 6) Troubleshooting

**Câmera não abre no Android:**
- Verifique se concedeu permissão em **Configurações → Apps → AbrigoQR → Permissões**.
- WebView precisa estar atualizada (Play Store → Android System WebView).

**Backend não conecta:**
- Toque longo no card "Entidade beneficiada" → digite a URL correta.
- Confira que `CORS_ORIGINS` no backend Render inclui `*` ou `capacitor://localhost`.

**Build falha com "SDK location not found":**
- Em `mobile/android/local.properties`, adicione:
  ```
  sdk.dir=C:\\Users\\SEU_USUARIO\\AppData\\Local\\Android\\Sdk
  ```
