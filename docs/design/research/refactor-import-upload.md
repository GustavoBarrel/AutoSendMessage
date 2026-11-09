# Refatoração da tela de importação de Excel

**Data:** 2026-09-28
**Tarefa:** Refatorar UI/UX da tela `http://localhost:8000/contatos/importar/`
**Nível:** FOCUSED

## Problema identificado

A tela atual funciona, mas apresenta oportunidades de melhoria em:
- Hierarquia visual do wizard de importação
- Clareza das informações do guia de formato
- Feedback visual da área de upload
- Responsividade em mobile
- Espaçamento e alinhamento geral

## Pesquisa realizada

### Padrões relevantes
- Wizard steps com indicadores visuais claros
- File drop com feedback de drag-and-drop
- Guias de formato com hierarquia clara (obrigatório vs opcional)
- Layout responsivo com breakpoints para mobile/tablet

### Direção escolhida
- Manter estrutura de 2 colunas no desktop (upload + guia)
- Melhorar wizard steps com animações sutis e melhor contraste
- Aumentar área de upload e melhorar feedback visual
- Reorganizar guia com melhor separação visual entre obrigatório e opcional
- Adicionar ícones/emoji para melhor reconhecimento visual
- Melhorar tipografia e espaçamento seguindo o design system existente

## Decisões

1. **Wizard steps:** manter estrutura atual, mas melhorar contraste e adicionar tooltips
2. **Área de upload:** aumentar padding, melhorar estados de hover/drag
3. **Guia:** separar visualmente obrigatório de opcionais com cards distintos
4. **Mobile:** manter layout empilhado, mas melhorar espaçamento e touch targets
5. **Cores:** usar tokens do design system existente (cor primária, azul secundário)

## Arquivos alterados

- `apps/contacts/templates/contacts/import_upload.html` - template principal
- `static/css/app.css` - estilos globais (novas classes)

## Validação

- Screenshot desktop: ✓
- Screenshot mobile: ✓
- Funcionalidades preservadas: ✓
- Regras de negócio mantidas: ✓
