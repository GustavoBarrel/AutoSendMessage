"""
Help texts of the statistics datasets (UI and charts only).
Does not change campaign sending, chips or contacts.
"""

from __future__ import annotations

from apps.dashboards.models import StatsDashboardWidget

# detail: full explanation; rec: suggested chart type (UI text, kept in Portuguese)
DATASET_HELP: dict[str, dict[str, str]] = {
    "status": {
        "detail": (
            "Conta quantas mensagens da fila estão em cada situação: pendente, em processamento, "
            "enviada, falha permanente, cancelada, ignorada ou aguardando chip offline. "
            "Inclui todos os status; respeita período, campanha, chip e filtro de status da tela."
        ),
        "rec": "pizza ou rosca",
    },
    "chip_total": {
        "detail": (
            "Para cada chip (nome atual ou snapshot se o chip foi excluído), soma todas as "
            "mensagens "
            "no período — qualquer status. Mostra qual chip concentrou mais volume. Até 15 chips."
        ),
        "rec": "barra",
    },
    "chip_sent": {
        "detail": (
            "Por chip, apenas mensagens com status enviado (WhatsApp aceitou o envio). "
            "Útil para comparar produção real entre chips."
        ),
        "rec": "barra",
    },
    "chip_failed": {
        "detail": (
            "Por chip, mensagens com falha permanente (ex.: número inválido, erro definitivo da "
            "API). "
            "Não inclui pendentes nem canceladas."
        ),
        "rec": "barra",
    },
    "chip_pending": {
        "detail": (
            "Por chip, mensagens ainda na fila aguardando envio (status pendente). "
            "Indica backlog por chip."
        ),
        "rec": "barra",
    },
    "chip_waiting": {
        "detail": (
            "Por chip, mensagens pausadas porque o WhatsApp estava desconectado (aguardando chip). "
            "Reconectar o chip ou redistribuir costuma liberar essa fila."
        ),
        "rec": "barra",
    },
    "chip_locked": {
        "detail": (
            "Por chip, mensagens que o worker está processando neste momento (em processamento). "
            "Volume alto e persistente pode indicar fila travada ou worker lento."
        ),
        "rec": "barra",
    },
    "chip_fail_rate": {
        "detail": (
            "Percentual de falha por chip: falhas ÷ (enviados + falhas) × 100. "
            "Só considera envios já finalizados (sucesso ou falha); ignora pendentes. "
            "Ordenado do pior para o melhor."
        ),
        "rec": "barra",
    },
    "chip_success_rate": {
        "detail": (
            "Percentual de sucesso por chip: enviados ÷ (enviados + falhas) × 100. "
            "Complementar à taxa de falha; só envios finalizados entram no cálculo."
        ),
        "rec": "barra",
    },
    "chip_avg_time": {
        "detail": (
            "Tempo médio em minutos entre a criação do job e o momento do envio bem-sucedido, por "
            "chip. "
            "Só mensagens enviadas com data de envio preenchida. Reflete fila + intervalos de "
            "proteção do chip."
        ),
        "rec": "barra",
    },
    "campaign_total": {
        "detail": (
            "Total de mensagens (jobs) por campanha no período, qualquer status. "
            "Campanhas sem nome aparecem como “Sem campanha”. Até 15 campanhas."
        ),
        "rec": "barra",
    },
    "campaign_sent": {
        "detail": ("Por campanha, quantidade de envios concluídos com sucesso (status enviado)."),
        "rec": "barra",
    },
    "campaign_failed": {
        "detail": (
            "Por campanha, quantidade de falhas permanentes. Ajuda a ver campanhas com público ou "
            "mídia problemática."
        ),
        "rec": "barra",
    },
    "campaign_pending": {
        "detail": (
            "Por campanha, mensagens ainda não enviadas (pendentes). Mostra o que falta disparar "
            "em cada campanha."
        ),
        "rec": "barra",
    },
    "campaign_fail_rate": {
        "detail": (
            "Taxa de falha (%) por campanha: falhas ÷ (enviados + falhas). "
            "Só envios finalizados; campanhas sem finalizados não entram no percentual."
        ),
        "rec": "barra",
    },
    "campaign_success_rate": {
        "detail": (
            "Taxa de sucesso (%) por campanha: enviados ÷ (enviados + falhas). "
            "Quanto maior, melhor o resultado da campanha entre os contatos já processados."
        ),
        "rec": "barra",
    },
    "daily_total": {
        "detail": (
            "Evolução no tempo: cada ponto/barra é um dia do calendário (eixo horizontal). "
            "O valor é a quantidade de mensagens naquele dia. "
            "No painel principal, o dia segue o filtro “Base da data” (criação, atualização ou "
            "envio). "
            "Em gráficos personalizados do painel, usa sempre a data de criação do job. "
            "Conta todas as mensagens do dia, independente do status."
        ),
        "rec": "linha",
    },
    "daily_sent": {
        "detail": (
            "Mesma lógica da evolução diária (total), mas conta só mensagens enviadas com sucesso "
            "naquele dia. "
            "Serve para ver em quais dias a campanha mais produziu entregas reais."
        ),
        "rec": "linha",
    },
    "daily_failed": {
        "detail": (
            "Mesma lógica da evolução diária (total), mas conta só falhas permanentes por dia. "
            "Picos indicam dias com problema de chip, mídia ou base de contatos."
        ),
        "rec": "linha",
    },
    "hourly": {
        "detail": (
            "Distribuição por hora do relógio (0h a 23h) usando a hora em que a mensagem foi "
            "enviada (data de envio). "
            "Só entram envios concluídos com sucesso. Mostra em quais horários do dia o sistema "
            "mais envia — "
            "útil para validar horário comercial e picos de uso."
        ),
        "rec": "barra ou linha",
    },
    "top_errors": {
        "detail": (
            "Agrupa as mensagens com falha permanente pelas categorias de erro mais comuns "
            "(número inválido, chip offline, timeout, mídia inacessível, etc.). "
            "Até 12 categorias, da mais frequente para a menos."
        ),
        "rec": "barra",
    },
    "defer_reason": {
        "detail": (
            "Conta quantas vezes uma mensagem foi adiada e por quê: fora do horário comercial, "
            "intervalo mínimo "
            "entre envios, pausa entre lotes, limite por hora/dia do chip, chip offline, etc. "
            "Só jobs que têm motivo de adiamento registrado."
        ),
        "rec": "pizza ou barra",
    },
    "attempts": {
        "detail": (
            "Entre envios já finalizados (sucesso ou falha), mostra quantos precisaram de 1, 2 ou "
            "3+ tentativas "
            "antes de concluir. Muitas mensagens em 3+ tentativas sugerem instabilidade de chip "
            "ou API."
        ),
        "rec": "barra ou pizza",
    },
    "variant_total": {
        "detail": (
            "Quantas mensagens usaram cada variação de texto/mídia da campanha (Variação 1, 2, "
            "3…). "
            "Inclui qualquer status, desde que a variação esteja vinculada ao job."
        ),
        "rec": "barra ou pizza",
    },
    "message_type": {
        "detail": (
            "Dos envios com sucesso, quantos foram texto puro, imagem ou vídeo (tipo da variação "
            "usada). "
            "Ajuda a ver o mix de conteúdo que mais foi disparado."
        ),
        "rec": "pizza",
    },
    "contacts_city": {
        "detail": (
            "Dos envios com sucesso, agrupa pelo campo cidade do cadastro do contato. "
            "Contatos sem cidade preenchida não entram. Até 15 cidades mais frequentes."
        ),
        "rec": "barra",
    },
    "contacts_state": {
        "detail": (
            "Dos envios com sucesso, agrupa pelo UF (estado) do contato. "
            "Contatos sem UF não entram. Até 15 UFs."
        ),
        "rec": "barra",
    },
    "contacts_neighborhood": {
        "detail": (
            "Dos envios com sucesso, agrupa pelo bairro do contato. "
            "Bairro vazio é ignorado. Até 15 bairros."
        ),
        "rec": "barra",
    },
    "contacts_category": {
        "detail": (
            "Dos envios com sucesso, agrupa pela categoria do contato (segmentação da base). "
            "Categoria vazia não entra."
        ),
        "rec": "barra ou pizza",
    },
    "contacts_subcategory": {
        "detail": (
            "Dos envios com sucesso, agrupa pela subcategoria do contato. "
            "Subcategoria vazia não entra."
        ),
        "rec": "barra",
    },
    "contacts_gender": {
        "detail": (
            "Dos envios com sucesso, agrupa pelo sexo informado no cadastro do contato. "
            "Campo vazio não entra."
        ),
        "rec": "pizza ou barra",
    },
}


def dataset_help_for_template() -> dict[str, dict[str, str]]:
    """Model label + help texts as JSON for the template."""
    labels = dict(StatsDashboardWidget.Dataset.choices)
    out: dict[str, dict[str, str]] = {}
    for key, label in labels.items():
        help_row = DATASET_HELP.get(key, {})
        out[key] = {
            "label": label,
            "detail": help_row.get("detail", ""),
            "rec": help_row.get("rec", ""),
        }
    return out
