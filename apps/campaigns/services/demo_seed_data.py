"""Static pools and specs used by the demo seed (``manage.py seed_demo``).

Everything here is fictional and deliberately neutral: made-up businesses, common Brazilian
names and cities from several states. Phone numbers are generated with area code 00, which does
not exist in Brazil, so no demo number can belong to a real person or receive a message.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Fixed seed: same data on every run, on every machine.
SEED = 20240601

# Area code 00 does not exist. Prefix + "9" + 8 digits = 11 digits, as the contact validator needs.
DEMO_AREA_CODE = "00"
CONTACT_COUNT = 80
# Contacts whose birth date is rewritten to "today" on every run (birthday filter demo).
BIRTHDAY_TODAY_COUNT = 6

FEMALE_FIRST_NAMES = (
    "Ana",
    "Beatriz",
    "Camila",
    "Carla",
    "Daniela",
    "Fernanda",
    "Gabriela",
    "Helena",
    "Isabela",
    "Juliana",
    "Larissa",
    "Letícia",
    "Mariana",
    "Natália",
    "Patrícia",
    "Priscila",
    "Renata",
    "Sandra",
    "Tatiana",
    "Vanessa",
)
MALE_FIRST_NAMES = (
    "André",
    "Bruno",
    "Carlos",
    "Daniel",
    "Eduardo",
    "Felipe",
    "Gabriel",
    "Gustavo",
    "Henrique",
    "João",
    "Leonardo",
    "Lucas",
    "Marcos",
    "Paulo",
    "Pedro",
    "Rafael",
    "Ricardo",
    "Rodrigo",
    "Thiago",
    "Vinícius",
)
LAST_NAMES = (
    "Almeida",
    "Alves",
    "Barbosa",
    "Cardoso",
    "Carvalho",
    "Costa",
    "Dias",
    "Ferreira",
    "Gomes",
    "Lima",
    "Martins",
    "Melo",
    "Moreira",
    "Nascimento",
    "Oliveira",
    "Pereira",
    "Ribeiro",
    "Rocha",
    "Santos",
    "Silva",
    "Souza",
    "Teixeira",
    "Vieira",
    "Araújo",
)

# (city, state, relative weight): São Paulo weighs more so city filters have a meaningful group.
CITIES = (
    ("São Paulo", "SP", 4),
    ("Rio de Janeiro", "RJ", 2),
    ("Belo Horizonte", "MG", 2),
    ("Curitiba", "PR", 1),
    ("Porto Alegre", "RS", 1),
    ("Salvador", "BA", 1),
    ("Fortaleza", "CE", 1),
    ("Goiânia", "GO", 1),
    ("Florianópolis", "SC", 1),
    ("Brasília", "DF", 1),
)
NEIGHBORHOODS = (
    "Centro",
    "Jardim das Flores",
    "Vila Nova",
    "Bela Vista",
    "Boa Vista",
    "São José",
    "Santa Luzia",
    "Parque Industrial",
)

# (category, relative weight, subcategories)
CATEGORIES = (
    ("Cliente", 5, ("Varejo", "Atacado")),
    ("Lead", 3, ("Indicação", "Site")),
    ("Fornecedor", 1, ("Matéria-prima", "Serviços")),
    ("Parceiro", 1, ("Revenda", "Divulgação")),
)

MANUAL_TAGS = ("Cliente VIP", "Newsletter", "Atendimento prioritário", "Novidades")

FAILURE_NOT_ON_WHATSAPP = 'Número não existe no WhatsApp (simulado): {"exists": false}'
FAILURE_TIMEOUT = "Timeout ao consultar o provedor (simulado)."


@dataclass(frozen=True)
class ChipSpec:
    name: str
    instance_name: str
    account_type: str


@dataclass(frozen=True)
class CampaignSpec:
    name: str
    status: str
    audience: str
    audience_filters: dict[str, object]
    variants: tuple[str, str, str]
    # Window (in days before today) where the history of sends is spread; ignored for drafts.
    oldest_day_ago: int = 0
    newest_day_ago: int = 0
    # Share of the audience already processed (1.0 = the whole audience).
    processed_ratio: float = 1.0
    failure_rate: float = 0.07


@dataclass(frozen=True)
class WorkspaceSpec:
    name: str
    slug: str
    color: str
    chips: tuple[ChipSpec, ...]
    campaigns: tuple[CampaignSpec, ...] = field(default_factory=tuple)


# Status/audience literals mirror Campaign.Status / Campaign.Audience (kept as plain strings so
# this module stays free of model imports).
WORKSPACES = (
    WorkspaceSpec(
        name="Loja Aurora",
        slug="demo-loja-aurora",
        color="#16a34a",
        chips=(
            ChipSpec("Chip Atendimento", "demo-aurora-atendimento", "old"),
            ChipSpec("Chip Vendas", "demo-aurora-vendas", "normal"),
        ),
        campaigns=(
            CampaignSpec(
                name="Promoção de fim de mês",
                status="finished",
                audience="all",
                audience_filters={"category": "Cliente", "whatsapp_only": True},
                variants=(
                    "Olá, {primeiro_nome}! Neste fim de mês temos condições especiais para "
                    "clientes de {cidade}. Responda esta mensagem e confira.",
                    "{primeiro_nome}, a promoção de fim de mês da Loja Aurora começou. "
                    "Fale com a gente para saber mais.",
                    "Oi, {primeiro_nome}! Separamos ofertas pensadas para você. "
                    "Quer receber a lista completa?",
                ),
                oldest_day_ago=6,
                newest_day_ago=3,
            ),
            CampaignSpec(
                name="Novidades da semana",
                status="active",
                audience="all",
                audience_filters={"whatsapp_only": True},
                variants=(
                    "Olá, {primeiro_nome}! Chegaram novidades na Loja Aurora. "
                    "Passe por aqui para conhecer.",
                    "{primeiro_nome}, preparamos uma seleção nova de produtos. Quer ver as opções?",
                    "Oi, {primeiro_nome}! Temos novidades em estoque. "
                    "Posso te mostrar os destaques?",
                ),
                oldest_day_ago=3,
                newest_day_ago=0,
                processed_ratio=0.5,
            ),
            CampaignSpec(
                name="Boas-vindas aos novos contatos",
                status="draft",
                audience="all",
                audience_filters={"category": "Lead", "whatsapp_only": True},
                variants=(
                    "Olá, {primeiro_nome}! Seja bem-vindo(a) à Loja Aurora.",
                    "{primeiro_nome}, é um prazer ter você por aqui. "
                    "Conte com a gente para o que precisar.",
                    "Oi, {primeiro_nome}! Obrigado pelo contato. Como podemos ajudar?",
                ),
            ),
        ),
    ),
    WorkspaceSpec(
        name="Estúdio Horizonte",
        slug="demo-estudio-horizonte",
        color="#2563eb",
        chips=(
            ChipSpec("Chip Agenda", "demo-horizonte-agenda", "normal"),
            ChipSpec("Chip Suporte", "demo-horizonte-suporte", "old"),
        ),
        campaigns=(
            CampaignSpec(
                name="Lembrete de agendamentos",
                status="finished",
                audience="all",
                audience_filters={"city": "São Paulo", "whatsapp_only": True},
                variants=(
                    "Olá, {primeiro_nome}! Lembrando que você pode agendar seu horário "
                    "no Estúdio Horizonte.",
                    "{primeiro_nome}, temos horários livres esta semana. "
                    "Quer que eu reserve um para você?",
                    "Oi, {primeiro_nome}! Seu próximo atendimento pode ser marcado por aqui.",
                ),
                oldest_day_ago=5,
                newest_day_ago=2,
                failure_rate=0.1,
            ),
            CampaignSpec(
                name="Pesquisa de satisfação",
                status="active",
                audience="all",
                audience_filters={"category": "Cliente", "whatsapp_only": True},
                variants=(
                    "Olá, {primeiro_nome}! Como foi sua experiência com o Estúdio Horizonte? "
                    "Sua opinião é importante.",
                    "{primeiro_nome}, pode nos contar em uma frase o que achou do atendimento?",
                    "Oi, {primeiro_nome}! Queremos melhorar. Que nota de 0 a 10 você daria?",
                ),
                oldest_day_ago=2,
                newest_day_ago=0,
                processed_ratio=0.45,
            ),
            CampaignSpec(
                name="Parabéns aos aniversariantes",
                status="draft",
                audience="birthday_today",
                audience_filters={"whatsapp_only": True},
                variants=(
                    "Parabéns, {primeiro_nome}! O Estúdio Horizonte deseja um ótimo dia.",
                    "{primeiro_nome}, feliz aniversário! Muita saúde e alegria.",
                    "Hoje é seu dia, {primeiro_nome}! Um abraço de toda a equipe.",
                ),
            ),
        ),
    ),
)

DASHBOARD_NAME = "Visão geral"
# (title, dataset, chart type, size): values of StatsDashboardWidget.Dataset / ChartType / Size.
DASHBOARD_WIDGETS = (
    ("Situação dos envios", "status", "doughnut", "small"),
    ("Evolução diária de envios", "daily_sent", "line", "medium"),
    ("Envios por chip", "chip_sent", "bar", "small"),
    ("Principais erros", "top_errors", "bar", "medium"),
)
