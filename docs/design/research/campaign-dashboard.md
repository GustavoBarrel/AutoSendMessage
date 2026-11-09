# Design Research — Painel de Campanha WhatsApp

## Data

27 de setembro de 2026

## Contexto

O **AutoSendMessage** é um sistema Django de envio automático de mensagens WhatsApp para campanhas de envio em massa.

A tela em análise é o **painel de campanha** (vista de detalhe da campanha), que centraliza:

- visão geral da campanha
- métricas de envio (enviados, restantes, falhas)
- fila/envio de mensagens
- gestão de mensagens (textos e variações)
- gestão de chips envolvidos
- público-alvo (contatos selecionados)
- ações de controle (iniciar, pausar, cancelar envio)

O produto já possui estrutura consolidada com models, views, templates Django e integração Evolution API. A pesquisa visa fundamentar melhorias visuais e de UX no painel de campanha.

## Objetivo

Fornecer base de pesquisa para projeto/redesign do **painel de campanha** do AutoSendMessage, com foco em:

1. organização da informação para usuário operacional de campanhas de envio
2. fluxos de controle de envio em massa via WhatsApp
3. apresentação de métricas e status de forma clara
4. densidade informacional adequada para uso frequente
5. responsividade mobile-first para operação em campo

## Usuário

### Perfil primário

- **Função:** Operador(a) de campanha / coordenador(a) de comunicação
- **Contexto de uso:** escritório de campanha, uso contínuo durante dia de trabalho
- **Frequência:** alta — abre o painel múltiplas vezes por dia
- **Expertise:** intermediária — sabe usar a ferramenta, mas não é técnica
- **Objetivo principal:** saber o status da campanha e controlar envios
- **Objetivo secundário:** ajustar mensagens, verificar falhas, monitorar chips
- **Pressão:** operação com prazos, decisões rápidas, necessidade de clareza

### Perfil secundário

- **Função:** Administrador(a) do sistema
- **Contexto:** configuração de chips, permissões, gestão geral
- **Frequência:** baixa/média
- **Objetivo:** configurar e supervisionar, não operar campanhas individualmente

## Problema

O painel atual (baseado no template `detail.html` e estrutura existente) precisa evoluir para:

1. **Saturação informacional:** o usuário operacional precisa ver status, métricas, chips, mensagens e público em uma tela — risco de sobrecarga cognitiva
2. **Controle de envio com risco:** iniciar/pausar/cancelar envios de mensagens WhatsApp é uma ação de impacto; o painel deve comunicar claramente o estado e as consequências
3. **Visibilidade de falhas:** mensagens que falharam precisam ser identificáveis e acionáveis rapidamente
4. **Métricas confusas:** "enviados", "restantes", "falhas" precisam de hierarquia clara
5. **Responsividade operacional:** o operador pode precisar acessar o painel de um celular durante a campanha — mobile não é secundário
6. **Limites de chip:** o sistema possui regras de envio configuráveis por chip que devem ser visíveis e alertadas quando ultrapassadas
7. **Dados reais vs. placeholders:** o painel deve funcionar com conteúdo real (nomes longos, listas extensas, múltiplos status)

## Escopo

### Dentro do escopo

- Layout e estrutura do painel de campanha (detalhe)
- Apresentação de métricas de envio
- Tabela/lista de status de mensagens enviadas
- Controles de envio (iniciar, pausar, cancelar)
- Seção de chips da campanha
- Seção de mensagens/variações
- Seção de público-alvo
- Estados vazios e de erro
- Responsividade mobile/tablet/desktop
- Feedback visual de ações

### Fora do escopo desta pesquisa

- Implementação de código (frontend ou backend)
- Wizard de criação de campanha (já existe)
- CRUD de contatos
- Configuração de chips (tela separada)
- Integração Evolution API (já implementada)
- Sistema de login/autenticação

## Pesquisa de UX

### Padrões encontrados

#### 1. Dashboard operacional com métricas em destaque

**Produtos referência:** Intercom, Zendesk, HubSpot, Mailchimp

Produtos de comunicação em massa utilizam um padrão consolidado:

- **Métricas principais** em cards ou barra de resumo no topo
- Ação primária (enviar/pausar) sempre visível
- Status global da operação comunicado imediatamente

**Base:** NN/g — "Dashboard Design" (https://www.nngroup.com/articles/dashboard-design/)

**Aplicabilidade:** O painel de campanha é um dashboard operacional. O usuário abre a tela querendo saber "o que está acontecendo com minha campanha?" — métricas de resumo no topo respondem isso em segundos.

#### 2. Tabela densa com ações inline para dados operacionais

**Produtos referência:** SendGrid, Twilio Console, Meta Business Suite

Para listas de mensagens enviadas com status:

- Tabelas densas são apropriadas para usuários especialistas/operacionais
- Ações contextuais (reenviar, ver detalhe) inline reduzem cliques
- Filtros e busca acima da tabela
- Paginação para listas extensas

**Base:** NN/g — "Tables for Complex Data" (https://www.nngroup.com/articles/tables-complex-data/)

**Aplicabilidade:** Mensagens enviadas de uma campanha podem ser centenas ou milhares. Tabela densa com filtros é o padrão correto. O usuário operacional prefere densidade informacional a espaçamento generoso.

#### 3. Stepper/wizard visual para fluxos multi-etapas

**Produtos referência:** Typeform, Shopify setup

O wizard de criação de campanha (público → chips → mensagens) já utiliza stepper visual. Este padrão é adequado porque:

- Divide tarefa complexa em passos claros
- Mostra progresso (etapas concluídas)
- Reduz ansiedade do usuário

**Base:** Material Design — Stepper (https://m3.material.io/components/steppers/overview)

**Aplicabilidade:** O wizard existente está correto. No painel, o stepper não se aplica — o fluxo é de consulta e controle, não de criação.

#### 4. Estados de envio com feedback claro

**Produtos referência:** Mailchimp campaign status, Brevo campaign dashboard

Estados de campanha de envio visualizados como badges/pills coloridos:

- **Rascunho:** cinza
- **Agendada:** azul/info
- **Enviando:** âmbar com spinner
- **Concluída:** verde
- **Pausada:** laranja
- **Cancelada:** vermelho
- **Falha:** vermelho

Ação primária muda conforme o estado:
- Rascunho → "Iniciar envio"
- Enviando → "Pausar"
- Pausada → "Retomar"
- Concluída → "Nova campanha"

**Base:** Material Design — Status indicators (https://m3.material.io/components/badges/overview)

**Aplicabilidade:** O estado da campanha deve ser o elemento mais visível do painel. O botão de ação principal deve ser dinâmico e contextual.

#### 5. Alerta inline para condições de risco

**Produtos referência:** Meta Business Suite, Google Ads

Avisos sobre limites de chip devem aparecer:

- No momento de configuração do chip (já existe na tela de chips)
- No momento de iniciar envio — alerta inline antes da ação
- No painel, como indicador visual quando limites estão sendo atingidos

**Base:** NN/g — "Error Message Guidelines" (https://www.nngroup.com/articles/error-message-guidelines/)

**Aplicabilidade:** O sistema já possui as regras de limite configuradas. O painel deve refletir visualmente quando uma campanha está próxima ou acima dos limites recomendados, sem bloquear a ação (apenas alertar).

### Fluxos

#### Fluxo primário: Operador abre o painel

1. Acessa lista de campanhas
2. Clica em uma campanha
3. Vê status global + métricas + botão de ação principal
4. Decide: iniciar, pausar, ou apenas monitorar
5. Se iniciar: confirmação com aviso de limites
6. Monitora progresso em tempo real (ou por refresh)
7. Se houver falhas: acessa lista de falhas, decide reenviar

#### Fluxo secundário: Operador ajusta a campanha

1. No painel, acessa seção de mensagens
2. Edita ou adiciona variação de texto
3. Salva — feedback de confirmação
4. Retorna ao painel, verifica métricas atualizadas

#### Fluxo de recuperação: Falhas de envio

1. Operador vê número de falhas no resumo
2. Clica em ver detalhes
3. Vê lista filtrada de mensagens falhadas
4. Ações: reenviar (individual ou em massa), descartar, exportar

### Arquitetura da informação

Proposta de estrutura para o painel:

```
Painel de Campanha
├── Cabeçalho
│   ├── Nome da campanha
│   ├── Status (badge)
│   └── Ação principal (botão contextual)
├── Alertas
│   └── Avisos de limite de chip (condicional)
├── Métricas (cards ou barra horizontal)
│   ├── Enviados
│   ├── Restantes
│   ├── Falhas
│   └── Taxa de sucesso
├── Conteúdo principal (abas ou seções)
│   ├── Envios (tabela densa)
│   ├── Mensagens (gerenciamento)
│   ├── Chips (vinculados)
│   └── Público (contatos selecionados)
└── Rodapé (metadados: criado em, última atualização)
```

### Interações

| Ação | Comportamento esperado |
|------|------------------------|
| Iniciar envio | Modal de confirmação com aviso de limites → feedback de início |
| Pausar envio | Confirmação → feedback de pausa |
| Cancelar envio | Confirmação forte (ação destrutiva) → feedback |
| Reenviar falha | Ação inline na tabela → feedback individual |
| Editar mensagem | Navegação para edição → retorno ao painel |
| Filtrar envios | Filtros aplicam instantaneamente, mantêm estado na sessão |
| Refresh manual | Botão de atualizar métricas |

### Estados

| Estado | Comunicação visual |
|--------|-------------------|
| Campanha enviando | Badge âmbar, spinner no botão "Pausar", métricas atualizando |
| Campanha pausada | Badge laranja, botão "Retomar" |
| Campanha com falhas | Badge vermelho, contador de falhas destacado, link para detalhes |
| Campanha concluída | Badge verde, métricas finais, botão "Nova campanha" |
| Sem envios ainda | Estado vazio com explicação e ação "Iniciar envio" |
| Erro de conexão (chip) | Badge de erro no chip, aviso no painel |

### Acessibilidade

- Navegação por teclado em toda a interface
- Focus visible em todos os elementos interativos
- ARIA labels em botões de ação (especialmente ícone-only)
- Status de campanha comunicado via texto + cor (não apenas cor)
- Mensagens de erro associadas ao campo/formulário correspondente
- Ordem de leitura lógica para leitores de tela
- Touch targets mínimos de 44×44px (WCAG 2.5.5)
- Respeito a `prefers-reduced-motion`

## Pesquisa Visual

### Referências de produtos

| Produto | Contexto | Padrão observado | Relevância |
|---------|----------|------------------|------------|
| **Meta Business Suite** | Gerenciamento de campanhas WhatsApp/Instagram | Status visual forte, métricas claras, alertas inline para limites de orçamento | Alta — mesmo canal (WhatsApp), mesmo domínio (campanha) |
| **SendGrid** | Envio de emails em massa | Dashboard com métricas, tabela de status, ações inline, filtros | Alta — padrão consolidado de envio em massa |
| **Twilio Console** | API de comunicação | Seções claras por recurso, status de mensagem detalhado, debug de falhas | Alta — modelo de operação técnica/operacional |
| **Mailchimp** | Campanhas de email | Stepper de criação, dashboard de campanha, métricas visuais | Alta — fluxo similar (criar → enviar → monitorar) |
| **Intercom** | Comunicação com clientes | Dashboard operacional, métricas em tempo real, ações contextuais | Média — padrão de dashboard SaaS operacional |
| **Notion** | Gerenciamento de conteúdo | Navegação por abas, estado vazio bem elaborado | Média — padrão de estado vazio e navegação |

### Composição

**Recomendação:**

- Layout em **coluna única** no mobile, **máximo 2 colunas** no desktop (não usar grid de 3+ colunas)
- Cabeçalho fixo com nome, status e ação principal
- Métricas em **barra horizontal** (mobile) ou **cards horizontais** (desktop) — não usar grid 2×2
- Conteúdo em **abas verticais** (lado) no desktop, **abas horizontais** (topo) no mobile
- Tabela de envios como elemento principal, com outras seções acessíveis por abas

**Por quê:** O usuário precisa de acesso rápido a múltiplas seções sem perder contexto. Abas reduzem scroll e mantêm navegação previsível. Grid de 3+ colunas fragmenta atenção em um contexto de uso intensivo.

### Tipografia

**Recomendação:**

- Fonte do sistema ou fonte web performática (Inter, system-ui)
- Escala limitada: 3 tamanhos principais (título, corpo, metadado)
- Título da campanha: semibold, 18–20px
- Métricas: tabular números, bold, 14–16px
- Corpo/células de tabela: regular, 14px
- Metadados (datas, labels secundários): regular, 12px, cor secundária

**Por quê:** Tipografia deve estabelecer hierarquia sem ruído. Interface operacional não precisa de variedade tipográfica — precisamos de legibilidade e escaneabilidade.

### Cores

**Paleta recomendada (semântica, não decorativa):**

| Papel | Cor | Uso |
|-------|-----|-----|
| Primária | Verde (#15803d ou similar) | Ação principal, sucesso, taxa de sucesso |
| Informação | Azul (#2563eb) | Status "enviando", links |
| Alerta | Âmbar (#d97706) | Avisos de limite, pausa |
| Erro | Vermelho (#dc2626) | Falhas, ações destrutivas |
| Neutro escuro | Cinza 800 | Texto principal |
| Neutro médio | Cinza 500 | Texto secundário |
| Neutro claro | Cinza 100 | Fundo de seção |
| Superfície | Branco | Cards, tabela |

**Por quê:** Paleta funcional, sem gradientes ou cores decorativas. Cada cor comunica um significado semântico. Verde como primária remete a confirmação e sucesso, e a identidade visual pode ser adaptada.

### Espaçamento

**Recomendação:**

- Escala: 4px base (4, 8, 12, 16, 24, 32)
- Espaçamento entre seções: 24px
- Espaçamento interno de cards: 16px
- Espaçamento entre linhas de tabela: denso (8px vertical)
- Padding de botões: 8px horizontal, 10px vertical

**Por quê:** Espaçamento deve comunicar agrupamento. Interface operacional com dados densos não precisa de espaçamento generoso — mas deve ser consistente.

### Componentes

**Primitivos necessários (já existentes ou a criar):**

- **Badge/Pill:** status de campanha e de mensagens individuais
- **Button:** primário, secundário, destrutivo — hierarquia clara
- **DataTable:** densa, com ordenação, filtros, ações inline
- **Alert/Banner:** avisos de limite e erros não-bloqueantes
- **Tabs:** navegação entre seções do painel
- **Modal:** confirmação de ações destrutivas/início de envio
- **Progress indicator:** barra ou contador para envio em andamento
- **Empty state:** quando não há dados para exibir
- **Search/Filter:** filtros na tabela de envios
- **MetricCard:** exibição de contadores (enviados, restantes, falhas)

### Densidade

**Recomendação: densidade alta (operacional)**

- Tabela: linhas compactas, ~40px de altura
- Espaçamento entre elementos: mínimo necessário
- Metadados em linha (não em linhas separadas)
- Ícones pequenos para ações inline
- Scroll vertical permitido para conteúdo extenso

**Por quê:** O usuário é operacional, usa a interface múltiplas vezes por dia, precisa escanear informações rapidamente. Densidade baixa exigiria mais scroll e mais cliques.

## Responsividade

### Breakpoints

| Ponto | Largura | Comportamento |
|-------|---------|---------------|
| Mobile | 320–480px | Coluna única, abas no topo, tabela com scroll horizontal se necessário, métricas empilhadas |
| Tablet | 481–768px | Coluna única, abas no topo, tabela com colunas essenciais |
| Desktop | 769–1024px | Layout amplo, abas laterais, tabela completa |
| Wide | 1025–1440px | Layout com espaço para filtros laterais |
| Max-width | 1280px | Conteúdo centralizado, não esticar além |

### Comportamentos específicos

**Mobile (320–480px):**

- Cabeçalho compacto: nome + status + botão de ação
- Métricas empilhadas verticalmente (2 por linha)
- Abas no topo, scroll horizontal se >4 abas
- Tabela: colunas essenciais visíveis, scroll horizontal para resto
- Filtros em overlay/drawer
- Botões de ação com largura total

**Tablet (481–768px):**

- Layout similar a desktop mas sem filtros laterais permanentes
- Métricas em linha (4 colunas)
- Tabela completa com scroll horizontal limitado
- Abas no topo

**Desktop (769px+):**

- Layout amplo com abas laterais ou topo
- Filtros laterais permanentes
- Tabela completa sem scroll horizontal
- Métricas em linha

### Tabela responsiva

Estratégia para tabela de envios em mobile:

1. **Colunas essenciais** sempre visíveis: contato, status, data
2. **Colunas secundárias** acessíveis por expansão de linha (accordion) ou scroll horizontal
3. **Ações** como ícone de menu (⋮) em cada linha, abrindo ações em dropdown
4. **Cards alternativos** para telas muito pequenas (cada envio vira um card compacto)

## Design System

### Estado atual

O projeto possui estrutura de templates Django com:

- Templates base (`base.html`) com blocos reutilizáveis
- Classes CSS utilitárias e componentes próprios
- Sem biblioteca de componentes externa (sem shadcn, sem Bootstrap)
- Favicon e identidade visual própria em desenvolvimento

### Recomendações para evolução

1. **Manter abordagem atual** (CSS próprio) — não introduzir biblioteca externa sem necessidade clara
2. **Documentar componentes existentes** — criar inventário de classes e blocos reutilizáveis
3. **Tokens CSS** — definir variáveis CSS para cores, espaçamento, tipografia (semantic tokens)
4. **Componentes a consolidar:**
   - `.btn-primary`, `.btn-secondary`, `.btn-link`
   - `.chip-pick`, `.chip-pill`, `.chip-provider-badge`
   - `.section`, `.section-header`, `.section-body`
   - Tabelas (criar componente consistente)
   - Badges de status (reutilizar em campanha, envios, chips)
   - Alertas/banners (novo)
   - Modais (novo)
   - Tabs (novo)

### Não recomendado

- Introduzir Bootstrap, Tailwind ou similar sem necessidade
- Criar componentes genéricos antes de confirmar reutilização
- Implementar dark mode sem requisito claro

## Padrões Relevantes

### 1. Badge de status como elemento primário de comunicação

Todo status (campanha, envio, chip) deve ter badge visual consistente. Cor + texto + ícone quando necessário.

### 2. Ação primária contextual

O botão principal do painel deve mudar conforme o estado:
- "Iniciar envio" (rascunho/pausada)
- "Pausar" (enviando)
- "Nova campanha" (concluída)

### 3. Métricas com números tabulares

Contadores devem usar fontes tabulares para evitar layout shift quando números mudam.

### 4. Feedback imediato para ações

Toda ação (iniciar, pausar, editar) deve ter feedback visual instantâneo — toast, atualização de estado, ou transição suave.

### 5. Estados vazios com ação

Nunca mostrar "nenhum dado" sem explicar por quê e oferecer ação relevante.

## Anti-patterns a Evitar

| Anti-pattern | Por que evitar | Como evitar |
|--------------|----------------|-------------|
| Dashboard genérico (sidebar + 4 KPIs + tabela) | Não reflete o fluxo real; desperdiça espaço | Layout focado na tarefa, não em template |
| Carditis | Cada seção em card separado aumenta ruído | Agrupar logicamente; usar espaçamento e tipografia para separar |
| Gradiente roxo/azul | Sem identidade de campanha; tendência genérica | Paleta funcional baseada no verde (identidade campanha) |
| Bento grid | Não se aplica a interface operacional densa | Layout por seções, não por grid decorativo |
| Mobile como afterthought | Operador pode usar celular em campo | Projetar mobile-first |
| Ícones decorativos | Aumentam ruído sem informação | Apenas ícones com significado claro |
| Estados apenas visuais | Status comunicado só por cor | Sempre cor + texto + ícone |
| Dados falsos em preview | Desenha interface para dados ideais | Testar com nomes longos, listas extensas, múltiplos status |

## Evidências

- Templates Django existentes analisados: `wizard_chips.html`, `wizard_steps.html`, `import_upload.html`
- Stack confirmada: Django 5.2, templates monolíticos, Evolution API
- Regras de limite de envio documentadas no AGENTS.md (configuráveis por chip)
- Estrutura de apps: `apps/<app>/` com models, views, urls, templates organizados
- Conceito de "campanha" como unidade central confirmado nos models
- Chips configurados via interface (não em código) — confirmado em AGENTS.md e template `wizard_chips.html`
- Suporte a Meta WhatsApp Business API provider — confirmado em template com badges "Meta"

## Hipóteses

- O usuário opera principalmente em desktop durante horário comercial, mas precisa de acesso mobile para monitoramento em campo (não confirmado por dados de uso)
- A taxa de reenvio de mensagens falhadas é baixa (<5%) — padrão observado em produtos similares, mas não medido no projeto
- O usuário prefere densidade informacional a espaçamento generoso — inferido do perfil operacional, mas não testado
- O fluxo de "editar mensagem e voltar ao painel" é mais comum que "navegar para outra seção" — inferido, não medido

## Recomendações

### 1. Estrutura do painel

- **Cabeçalho fixo** com nome da campanha, status (badge) e ação principal (botão contextual)
- **Barra de métricas** horizontal no topo (4 métricas: enviados, restantes, falhas, taxa)
- **Abas verticais** (desktop) ou **horizontais** (mobile): Envios | Mensagens | Chips | Público
- **Seção "Envios"** como aba padrão (é a mais consultada)

### 2. Tabela de envios

- Tabela densa com ordenação e filtros
- Ações inline (ver detalhe, reenviar)
- Paginação
- Badge de status por linha
- Scroll horizontal no mobile (colunas essenciais sempre visíveis)

### 3. Comunicação de status

- Status de campanha como elemento mais visível
- Badge + texto + cor (não apenas cor)
- Ação principal sempre presente e contextual

### 4. Alertas de limite

- Banner alerta no painel quando campanha ultrapassa limites do(s) chip(s)
- Banner na confirmação de início de envio
- Indicador visual no chip quando próximo do limite

### 5. Responsividade

- Mobile-first: projetar para 375px primeiro
- Métricas empilhadas no mobile
- Abas adaptativas (topo mobile, lado desktop)
- Tabela com scroll horizontal e colunas essenciais fixas

### 6. Estados

- Estado vazio bem elaborado (explicação + ação)
- Loading state com skeleton ou spinner apropriado
- Error state com mensagem clara + ação de recuperação
- Estado de envio em andamento com indicador de progresso

### 7. Acessibilidade

- Navegação completa por teclado
- Focus visible
- ARIA labels em ações
- Contraste mínimo 4.5:1 (WCAG AA)
- Touch targets ≥44×44px

## Decisões

| Decisão | Justificativa |
|---------|---------------|
| Layout em coluna com abas, não sidebar | Fluxo do painel tem poucas áreas principais; sidebar consumiria espaço sem benefício |
| Métricas em barra horizontal, não cards 2×2 | Escaneabilidade rápida; evita carditis; comunica relação entre métricas |
| Tabela densa para envios | Usuário operacional, dados frequentes, densidade apropriada para o domínio |
| Abas laterais (desktop) / topo (mobile) | Navegação previsível, poucas seções, funciona em todos os tamanhos |
| Paleta funcional sem gradientes | Identidade de campanha (verde), acessibilidade, sem tendências genéricas |
| Badges de status cor + texto + ícone | Nunca depender apenas de cor (WCAG + acessibilidade) |
| Alertas inline, não modais | Alerta deve ser visível mas não bloquear fluxo operacional |

## Decisões Rejeitadas

| Alternativa | Motivo da rejeição |
|-------------|-------------------|
| Sidebar permanente de navegação | O painel tem poucas seções (4 abas); sidebar consumiria ~200px de largura sem benefício proporcional, especialmente em mobile |
| Grid de KPIs 2×2 com cards grandes | Cria carditis; métricas são complementares e devem ser escaneadas em linha, não como entidades separadas |
| Cards de resumo por seção (chip, mensagens, público) no topo | Adiciona ruído visual; o usuário já acessa essas seções por abas |
| Modal de confirmação para toda ação | Apenas ações destrutivas (cancelar envio) ou de alto impacto (iniciar envio) merecem modal; editar mensagem não precisa |
| Dark mode | Sem requisito claro; contexto de uso em horário comercial (luz natural); adiciona complexidade sem benefício comprovado |
| Gráficos de progresso | Dados são contagens simples (enviados, restantes); barra de progresso ou percentual já comunica o necessário sem gráfico |

## Fontes

| Fonte | Tipo | Relevância |
|-------|------|------------|
| Nielsen Norman Group | UX / Usability | Alta — referência consolidada para dashboards, tabelas, formulários |
| W3C WCAG 2.2 | Acessibilidade | Alta — conformidade obrigatória |
| Material Design 3 | Design System | Alta — padrões de componente, status, interação |
| Apple HIG | UI / Responsivo | Média — padrões de hierarquia e adaptação |
| SendGrid Dashboard | Produto real | Alta — padrão de envio em massa |
| Twilio Console | Produto real | Alta — padrão operacional de comunicação |
| Meta Business Suite | Produto real | Alta — mesmo canal (WhatsApp), mesmo domínio |
| Mailchimp | Produto real | Média — padrão de campanha |

## URLs

- https://www.nngroup.com/articles/dashboard-design/
- https://www.nngroup.com/articles/tables-complex-data/
- https://www.nngroup.com/articles/error-message-guidelines/
- https://m3.material.io/components/steppers/overview
- https://m3.material.io/components/badges/overview
- https://www.w3.org/TR/WCAG22/
- https://developer.apple.com/design/human-interface-guidelines/
- https://m3.material.io/

## Limitações da Pesquisa

- Não foi possível analisar dados de uso reais do AutoSendMessage (sem analytics)
- Pesquisa visual baseada em produtos conhecidos, não em sessões de análise de telas específicas (sem acesso a Mobbin/Refero no momento)
- Padrões de comportamento do usuário final (operadores de campanha) não foram validados por pesquisa direta
- Limites de chip recomendados são sugestões documentadas — valores reais dependem de configuração por chip ainda não especificada
- Acessibilidade validada por referência (WCAG), não por teste com usuários reais
