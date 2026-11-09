# Plano: Integração da API Oficial do WhatsApp (Meta) como Provider Alternativo

## Objetivo
Permitir enviar mensagens via **API oficial do WhatsApp (Meta Graph API)** além da Evolution API,
com escolha por chip/campanha. Nenhuma funcionalidade existente é quebrada.

## Princípio de design
O sistema atual já separa bem as camadas:
- modelos e regras de campanha são independentes de provedor
- fila, worker, rate limit, retry e auditoria não dependem de Evolution

Vamos introduzir uma **camada de abstração de provedor** entre o núcleo e o envio,
de forma que Evolution e Meta sejam apenas implementações alternativas.

## Providers suportados
1. **Evolution** (mantido como está, comportamento inalterado)
2. **Meta WhatsApp Business API** (Cloud ou on-premises) — novo

---

## Fase 1 — Abstração de provedor (sem quebrar nada existente)

### 1.1 Interface base
Criar `apps/chips/providers/base.py`:
```python
class WhatsAppProvider(ABC):
    @abstractmethod
    def send_text(self, job, variant) -> ProviderSendResult: ...
    @abstractmethod
    def send_media(self, job, variant) -> ProviderSendResult: ...
    @abstractmethod
    def get_connection_state(self) -> str: ...  # "open" | "close"
    @abstractmethod
    def format_number(self, celular: str) -> str: ...
```

`ProviderSendResult` encapsula: `ok`, `error_message`, `http_status`, `external_id`, `payload`, `error_kind`.

### 1.2 Provider Evolution (wrapper)
Criar `apps/chips/providers/evolution.py`:
- Encapsula as funções de `evolution_send.py` e `evolution_whatsapp_errors.py`
- Repassa `send_text` / `send_media` com o formato atual
- Mantém `connection_state` como está
- **Nenhuma mudança no comportamento existente**

### 1.3 Provider Meta
Criar `apps/chips/providers/meta.py`:
- Usa Meta Graph API (`https://graph.facebook.com/vXX.X/<PHONE_NUMBER_ID>/messages`)
- Autenticação: `Authorization: Bearer <access_token>`
- Envia texto e mídia (imagem/vídeo/documento)
- Formata número para padrão internacional (reusa `format_whatsapp_number_br` já existente em `evolution_send.py`)
- Trata erros da Meta (`4xx`, `5xx`, códigos específicos)

### 1.4 Factory
Criar `apps/chips/providers/__init__.py`:
```python
def get_provider_for_chip(chip: Chip) -> WhatsAppProvider:
    if chip.provider == Chip.Provider.EVOLUTION:
        return EvolutionProvider(chip)
    if chip.provider == Chip.Provider.META:
        return MetaProvider(chip)
    raise ValueError(f"Provider desconhecido: {chip.provider}")
```

---

## Fase 2 — Ajustes no modelo Chip

### 2.1 Campo `provider`
Adicionar `provider` em `Chip` (CharField com choices):
- `evolution` (default — mantém compatibilidade)
- `meta`

### 2.2 Campos Meta (nullable)
```python
meta_phone_number_id = CharField(max_length=120, blank=True)
meta_access_token = CharField(max_length=255, blank=True)
meta_waba_id = CharField(max_length=120, blank=True)
meta_api_version = CharField(max_length=10, default="v18.0")
```

### 2.3 Validação
No `clean()` do `Chip`:
- Se `provider == 'meta'`, exigir `meta_phone_number_id` e `meta_access_token`
- Se `provider == 'evolution'`, exigir `instance_name` (já existe)

Migration atômica: adiciona os campos + choices, sem remover nada.

**Migração de dados existente**: chips antigos ficam com `provider='evolution'` automaticamente.

---

## Fase 3 — Atualizar camada de dispatch

### 3.1 Renomear/Adaptar
`apps/campaigns/services/campaign_evolution_dispatch.py` → `apps/campaigns/services/campaign_provider_dispatch.py`
- Usa `get_provider_for_chip(job.chip)` para escolher o provedor
- Delega `send_text` / `send_media` ao provedor
- Erros do provedor são normalizados em `error_kind`, `error_message`, `http_status`

### 3.2 Atualizar `campaign_send_execution.py`
- Importar `campaign_provider_dispatch` ao invés de `campaign_evolution_dispatch`
- Nenhuma mudança na lógica de lock, lease, retry, mark_sent — só muda o dispatch final

### 3.3 Erros específicos
Atualizar `evolution_whatsapp_errors.py` ou criar `provider_errors.py`:
- Adicionar mapeamento de erros da Meta (`400`, `401`, `403`, `500`)
- Classificar em permanente vs temporário
- Detectar erros de número/não Whatsapp (`131051` etc.)

---

## Fase 4 — Connection state

### 4.1 Para Meta
- `connection_state` sempre retorna `"open"` se o chip Meta estiver configurado
- Não há QR code / pairing code
- Remover dependência de `connection_state` para decisões de envio com Meta

### 4.2 Views afetadas
- `apps/dashboards/views_home.py` (`home`) e `apps/chips/views.py` (`chip_connect`, etc.) — apenas para Evolution
- `apps/campaigns/views.py` (`campaign_detail`) — filtrar chips disponíveis para reenvio por estado de conexão
  - Meta: sempre disponível (se configurado)
  - Evolution: apenas `"open"`

---

## Fase 5 — UI (templates e forms)

### 5.1 Form de Chip (`apps/chips/forms.py`)
- Campo `provider` (radio/select)
- Conditional fields: mostra campos Evolution ou Meta conforme seleção
- Validação condicional no `clean()`

### 5.2 Templates de Chip
- `apps/chips/templates/chips/form.html` — mostra/hide campos por provider
- `apps/chips/templates/chips/connect.html` — apenas para Evolution
- `apps/dashboards/templates/dashboards/home.html` — mostra provider no card do chip

### 5.3 Wizard de campanha (chips)
- `wizard_chips.html` — lista chips com provider e estado
- Para Meta, estado é sempre "conectado"
- Permite misturar providers na mesma campanha (cada chip usa seu próprio provedor)

---

## Fase 6 — Rate limit e limites

### 6.1 Rate limit do projeto (mantido)
O `campaign_rate_limit.py` continua funcionando igual:
- horário comercial
- delay entre mensagens
- batch pause
- limites hora/dia

### 6.2 Rate limit da Meta
Adicionar aviso na UI quando usar Meta:
- Cloud API: ~1 msg/segundo por número (depende do tier)
- On-premises: depende da infraestrutura
- O rate limiter do projeto atua como camada extra de proteção

### 6.3 Avisos no wizard
- Se campanha tem chips Meta: avisar sobre necessidade de templates para janela 24h
- Se volume ultrapassa limites recomendados: aviso existente continua funcionando

---

## Fase 7 — Testes e documentação

### 7.1 Testes unitários
- `test_meta_provider.py` — mock da Meta API, testa send_text, send_media, erros
- `test_provider_factory.py` — testa seleção por chip
- Atualizar testes existentes que usam `evolution_send` diretamente

### 7.2 Testes de integração
- Testar campanha completa com chip Meta
- Testar campanha com chips mistos (Evolution + Meta)
- Testar rate limit, retry, backoff com Meta provider

### 7.3 Documentação
- `META_SETUP.md` — guia passo a passo para configurar Meta API
- `README.md` — atualizar com nova opção de provider

---

## Decisões pendentes (decidir antes de implementar)

### D1 — Templates obrigatórios?
**Contexto**: Meta bloqueia envio livre após 24h da última interação do contato.
Contatos que nunca escreveram exigem message template pré-aprovado.

**Opções**:
1. **Exigir templates sempre** (mais simples, compatível com Meta 100%)
   - Wizard de mensagens vira "templates" ao invés de texto livre
   - Cada template tem: nome, variáveis (ex.: `{{nome}}`), corpo, mídia opcional
   - Variáveis são substituídas no envio

2. **Híbrido** (envio livre + templates)
   - Envio livre funciona dentro de 24h
   - Fora de 24h, usa template
   - Mais complexo: rastrear última interação por contato

3. **Manter texto livre, avisar no dispatch** (mínimo)
   - Envia como texto livre
   - Se falhar por 24h, classifica como falha temporária
   - Usuário decide manualmente

**Recomendação**: Opção 1 (templates) — mais alinhado com Meta, evita surpresas.

### D2 — Armazenamento de credenciais Meta
**Opções**:
1. No próprio modelo `Chip` (simples, mas token em texto plano)
2. Model separado `ProviderCredential` com criptografia (mais seguro)
3. Variáveis de ambiente globais + Phone Number ID no chip (menos flexível)

**Recomendação**: Opção 1 para começar (simples), com aviso para usar variáveis de ambiente em produção.

### D3 — Mistura de providers na mesma campanha
**Decisão**: Permitir. Cada chip usa seu próprio provider. O worker já resolve o provedor por job.

---

## Ordem de implementação sugerida

1. **Fase 1** — Abstração + providers (base, Evolution wrapper, Meta client)
2. **Fase 2** — Modelo Chip (provider + campos Meta + migration)
3. **Fase 3** — Dispatch (campaign_provider_dispatch + atualizar execution)
4. **Fase 4** — Connection state + views
5. **Fase 5** — UI (forms + templates)
6. **Fase 6** — Avisos de rate limit Meta
7. **Fase 7** — Testes + documentação

Cada fase pode ser commit separado. Nenhuma fase quebra o que veio antes (Evolution continua funcionando do início ao fim).

---

## Como configurar e testar (visão geral)

### Configuração
1. Criar WhatsApp Business Account no Meta Business Manager
2. Criar WhatsApp Business Phone Number
3. Gerar token de acesso (permanente ou temporário)
4. Anotar Phone Number ID e WABA ID
5. (Opcional) Subir on-premises API ao invés de usar Cloud

### Teste local
1. Configurar `.env` com `CHIP_PROVIDER=meta` (ou via UI)
2. Cadastrar chip Meta com credenciais
3. Criar campanha com template de mensagem
4. Ativar campanha com chip Meta
5. Rodar worker: `python manage.py run_campaign_send_worker`
6. Verificar envios no Meta Manager e no painel da campanha
