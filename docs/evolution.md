# Evolution API — passo a passo (rodar local e testar chip)

Use **um terminal só para a Evolution**; o AutoSendMessage roda em outro.

---

## 1. Pré-requisitos

- **Node.js** v20 (em Windows: https://nodejs.org/ — LTS)
- **PostgreSQL** instalado e rodando
- **Redis** instalado e rodando

---

## 2. Clone e instalação (primeira vez)

No terminal (PowerShell ou CMD), em uma pasta qualquer (ex.: sua área de trabalho):

```powershell
git clone https://github.com/EvolutionAPI/evolution-api.git
cd evolution-api
npm install
```

*(Não use mais `-b v2.0.0` — o repositório usa a branch `main`.)*

---

## 3. Configurar o `.env`

```powershell
copy .env.example .env
```

Abra o arquivo `.env` e ajuste:

- **PostgreSQL:** algo como `DATABASE_URL="postgresql://usuario:senha@localhost:5432/evolution"`
- **Redis:** algo como `REDIS_ENABLED=true`, `REDIS_URI="redis://localhost:6379"`
- **Chave da API:** `AUTHENTICATION_API_KEY=evolution-dev-key` (pode ser outra; anote para usar no AutoSendMessage)

**Se o QR aparecer só no terminal da Evolution e não no site:** em algumas versões a API retorna `code: null` na resposta. Adicione no `.env` (e reinicie a Evolution):

```env
SERVER_URL=http://localhost:8080
QRCODE_LIMIT=30
QRCODE_COLOR=#198754
```

*(Ajuste `SERVER_URL` se a Evolution rodar em outra URL.)*

**Se aparecer muita coisa no terminal (logs repetidos):** é normal. Para reduzir, adicione no `.env`:

```env
LOG_LEVEL=ERROR,WARN
```

Reinicie a Evolution depois.

**QR no site via webhook:** ao abrir "Conectar (QR)" no AutoSendMessage, o site configura na Evolution o webhook da instância para a URL do próprio site (ex.: `http://127.0.0.1:8000/webhook/evolution/qrcode/<token>/`; o `<token>` é secreto e derivado da `SECRET_KEY`, e a rota responde 403 sem ele). A Evolution precisa conseguir acessar essa URL (em dev, no mesmo PC, funciona). Quando a Evolution gera o QR, envia um POST para esse endereço; o site guarda o QR e a página exibe em alguns segundos (polling). Não é obrigatório configurar webhook no `.env` da Evolution para isso.

Documentação completa das variáveis: https://doc.evolution-api.com/v2/pt/env

---

## 4. Banco de dados e build

Ainda dentro da pasta `evolution-api`:

**No Linux/macOS/Git Bash:**
```powershell
npm run db:generate
npm run db:deploy
npm run build
```

**No Windows (PowerShell):** se `npm run db:deploy` der erro *'rm' não é reconhecido*, rode:

```powershell
Remove-Item -Recurse -Force .\prisma\migrations -ErrorAction SilentlyContinue
Copy-Item -Recurse -Force .\prisma\postgresql-migrations .\prisma\migrations
npx prisma migrate deploy --schema .\prisma\postgresql-schema.prisma
npm run build
```

---

## 5. Subir a Evolution API

```powershell
npm run start:prod
```

Deixe esse terminal aberto. A API fica em **http://localhost:8080**.

**Teste rápido:** abra no navegador http://localhost:8080 — deve aparecer algo como *"Welcome to the Evolution API, it is working!"*.

---

## 6. Cadastrar o servidor no AutoSendMessage

1. Rode o AutoSendMessage (`python manage.py runserver` em outro terminal).
2. Faça login e escolha um perfil.
3. Vá em **Configuração Evolution API** (ou link equivalente no menu).
4. **Adicionar servidor:**
   - **Nome:** ex. `Local`
   - **URL base:** `http://localhost:8080`
   - **API key:** a mesma do `.env` (ex.: `evolution-dev-key`).

Salve.

---

## 7. Adicionar o chip no AutoSendMessage

1. Vá em **Configuração de chips** (ou **Chips**).
2. **Adicionar chip:**
   - **Servidor:** o que você cadastrou (ex. `Local`).
   - **Nome:** ex. `Chip atendimento 1` (só para exibição no sistema).
   - **Nome da instância:** um identificador único (ex. `chip-atendimento1`). O sistema criará essa instância na Evolution na hora em que você clicar em **Conectar (QR)** — não é preciso criar nada na Evolution antes.
   - Preencha tipo de conta, limites e horários conforme a tela.
3. Salve.

---

## 8. Conectar o chip (QR Code) e testar

1. Na lista de chips, clique em **Conectar (QR)** no chip que você criou.
2. Se a instância ainda não existir na Evolution, o sistema cria automaticamente e em seguida abre a tela com o **QR code** (e o código de pareamento, se a API retornar).
3. No **celular:** WhatsApp → Configurações → Aparelhos conectados → Conectar aparelho.
4. Escaneie o QR code. Quando conectar, pode fechar a página e voltar à lista de chips.

Se a tela do QR não carregar ou der erro, confira:

- A Evolution está rodando? (http://localhost:8080 responde?)
- URL base e API key do servidor no AutoSendMessage estão iguais ao `.env` da Evolution?

---

## Resumo rápido (depois de já configurado)

**Terminal 1 – Evolution:**
```powershell
cd evolution-api
npm run start:prod
```

**Terminal 2 – AutoSendMessage:**
```powershell
cd AutoSendMessage
.venv\Scripts\activate
python manage.py runserver
```

Acesse o app, cadastre o servidor (uma vez), adicione o chip com o nome da instância e use **Conectar (QR)** para testar a conexão do chip.
