# AutoSendMessage – Contexto para o Agente

## O que é o projeto
Sistema web (Django) para **envio automático de mensagens por WhatsApp**, organizado em **campanhas** e **perfis** (workspaces), com **controle rigoroso do ritmo de envio** para **reduzir o risco de bloqueio dos chips**. O envio usa a **Evolution API** ou a **API oficial da Meta**, com fila no banco, worker, limites por chip e atraso humanizado.

## Convenções (obrigatório)
- **Código em inglês, interface em português (pt-BR).**
  - Em inglês: models, campos, apps, módulos, funções, variáveis, URLs (nomes e caminhos), nomes de templates, variáveis de contexto, valores gravados no banco, chaves internas de JSON, docstrings, comentários e mensagens de log.
  - Em português: tudo que o usuário vê (`verbose_name`, `help_text`, labels, `messages.*`, `ValidationError`, textos de template, labels de choices, mensagens de erro exibidas).
- Os **placeholders de mensagem** digitados pelo usuário continuam em português: `{nome}`, `{primeiro_nome}`, `{nome_completo}`, `{ultimo_nome}`, `{celular}`, `{cidade}`, `{bairro}`, `{uf}`, `{categoria}`, `{subcategoria}`, `{sexo}`, `{email}`.
- Comentário explica o **porquê**, não o quê. Classes CSS e ids de HTML ainda seguem o layout antigo (serão refeitos na fase de layout).

## Boas práticas (obrigatório)
Seguir **ao máximo** as boas práticas de **Python** e **Django** em todo o código:

- **Python:** PEP 8, type hints quando fizer sentido, funções/classes pequenas e com responsabilidade única, `pathlib`, tratamento explícito de exceções, sem código duplicado (DRY). Formatação e lint com **ruff** (`pyproject.toml`).
- **Django:** views enxutas com lógica em **services/forms/models**; **forms** para validação de input; **QuerySet** otimizado (`select_related`/`prefetch_related`); **models** com `Meta` (ordering, verbose_name) e métodos no model em vez de lógica solta; **URLs** nomeadas, com namespace por app, e `reverse`/`redirect('app:nome')`; **templates** sem lógica pesada; **settings** e secrets via variáveis de ambiente; migrations atômicas e reversíveis quando possível.
- **Segurança:** CSRF em formulários, validação no backend, não confiar em dados do cliente, permissões por view/model quando necessário.

## Stack
- **Python** 3.12, **Django** 5.2 LTS
- **Evolution API** e **API oficial da Meta** para o envio (provedores em `apps/chips/providers/`)
- **Frontend:** monolítico (templates Django), sem SPA
- **Banco:** SQLite em dev; PostgreSQL configurável por variáveis `DB_*` (`.env.example`)

## Estrutura
```
config/        settings, urls, wsgi, asgi
apps/
  common/      EncryptedField, utils/safe.py (decorators de view), templatetags compartilhadas
  accounts/    User (customizado), Workspace, login/logout, escolha de perfil, context processor
  contacts/    Contact, ManualTag, importação (Excel/CSV) e telas de contatos e tags
  chips/       Chip, provedores (Evolution e Meta), cliente da Evolution, webhook do QR, conexão
  campaigns/   Campaign*, serviços campaign_*, fila/worker, wizard, comando run_campaign_send_worker
  dashboards/  home, StatsDashboard*, datasets e telas de estatísticas
templates/     base.html, registration/login.html (o resto fica em apps/<app>/templates/<app>/)
static/css/    app.css, login.css, tokens.css, workspace_select.css
docs/          documentação (Evolution, plano do provedor Meta, pesquisas de design)
```
- Cada app tem `urls.py` com `app_name` (namespace), por exemplo `chips:chip_list`, `campaigns:campaign_detail`.
- Os testes ficam em `apps/<app>/tests/test_*.py`; o teste de fumaça das páginas está em `apps/common/tests/test_smoke.py`.
- Dependências entre apps (de acordo com os imports; `A -> B` quer dizer que A importa B):
  - `accounts -> common`.
  - `contacts -> campaigns` (as views e os serviços `contact_archive` e `contact_filters`, que usam `CampaignSendJob` e os filtros do público da campanha) e `common`.
  - `chips -> accounts`, `common` e `campaigns` (`chips/views.py` e os provedores usam `campaigns.services`; `campaigns.models` só entra sob `TYPE_CHECKING`).
  - `campaigns -> accounts`, `chips`, `contacts` e `common`.
  - `dashboards -> accounts`, `chips`, `contacts`, `campaigns` e `common`.
  - Os models seguem uma ordem sem ciclo: `accounts` <- `chips`, `contacts` <- `campaigns` <- `dashboards`. Nos serviços e views há dois pares que se importam nos dois sentidos: `chips` <-> `campaigns` e `contacts` <-> `campaigns`. Não há ciclo de import hoje; evite criar novos acoplamentos.

## Como rodar
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
python manage.py run_campaign_send_worker   # em outro terminal
```

## Validação
```bash
ruff check .
ruff format --check .
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

## Domínio
- **Contatos:** base global (sem FK de perfil), importada por Excel/CSV com wizard (upload → mapeamento → prévia → duplicatas → resumo → execução).
- **Campanhas:** pertencem a um perfil; o público é escolhido por filtros (todos, aniversariantes, cidade, UF, bairro, categoria, tags manuais, inclusões e exclusões); cada campanha tem exatamente 3 variações de mensagem e controle de enviados (quem recebeu, quando, status, tentativas).
- **Chips:** conexões de envio, configuradas **pela interface** (não em código), com limites por chip e janela de horário.
- **Autenticação:** login obrigatório; usuários criados pelo administrador (sem auto-cadastro).

## Limites de envio (evitar bloqueio dos chips)
Estratégia a ser **respeitada pelo sistema** (não obrigatório a risca, mas a interface deve **exibir aviso** nas telas de campanha/envio quando os valores estiverem acima do recomendado):

**Limites por chip (configuráveis por chip; sugestão de padrão seguro):**
- **Conta nova (&lt;7 dias):** até 20–30 msg/dia, máx. 5 msg/hora
- **Conta normal (1–3 meses):** até 50–100 msg/dia, máx. 10 msg/hora
- **Conta antiga (&gt;3 meses):** até 100–200 msg/dia, máx. 20 msg/hora

**Intervalos entre envios:**
- Mínimo **30 segundos** entre uma mensagem e outra (recomendado 20–35 s aleatórios para parecer humano)
- A cada **~25 mensagens**, pausa de **2–3 minutos**
- Evitar rajada: **máx. 5 mensagens por minuto** por chip

**Boas práticas (refletir em avisos/UI):**
- Enviar em horário comercial (ex.: 8h–18h)
- Personalizar mensagens (ex.: uso do nome do contato) reduz bloqueios
- Números novos têm tolerância menor; contas “aquecidas” (uso normal antes) performam melhor

**Onde avisar:** nas telas em que o usuário **cria/edita campanha**, **configura chip** ou **dispara envio**, exibir aviso quando:
- volume da campanha ou velocidade configurada ultrapassar os limites recomendados para o(s) chip(s) usados, ou
- lembrete de manter intervalo entre envios e limites por hora/dia.

Em qualquer implementação, **priorizar código limpo e aderente às boas práticas de Python e Django** descritas acima.
