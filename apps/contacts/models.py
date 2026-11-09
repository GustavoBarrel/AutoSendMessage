from django.core.validators import RegexValidator
from django.db import models


class ManualTag(models.Model):
    """Manual tag for segmentation (e.g. VIP clients, newsletter)."""

    name = models.CharField(max_length=60, unique=True, verbose_name="nome")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="criado em")

    class Meta:
        verbose_name = "Tag manual"
        verbose_name_plural = "Tags manuais"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Contact(models.Model):
    """Global contact for send campaigns, imported from Excel and identified by mobile phone."""

    code = models.CharField(max_length=60, blank=True, verbose_name="codigo")
    name = models.CharField(max_length=200, verbose_name="nome")
    gender = models.CharField(max_length=20, blank=True, verbose_name="sexo")
    postal_code = models.CharField(max_length=20, blank=True, verbose_name="cep")
    street = models.CharField(max_length=200, blank=True, verbose_name="logradouro")
    street_number = models.CharField(max_length=30, blank=True, verbose_name="numero")
    neighborhood = models.CharField(max_length=100, blank=True, verbose_name="bairro")
    city = models.CharField(max_length=100, blank=True, verbose_name="cidade")
    state = models.CharField(max_length=5, blank=True, verbose_name="uf")
    mobile_phone = models.CharField(
        max_length=20,
        validators=[
            RegexValidator(
                regex=r"^\d{11}$",
                message="Celular deve conter 11 dígitos (DDD + número), ex: 11999999999.",
            )
        ],
        verbose_name="celular",
    )
    has_whatsapp = models.BooleanField(default=False, verbose_name="whatsapp")
    birth_date = models.DateField(null=True, blank=True, verbose_name="data nascimento")
    email = models.EmailField(blank=True)
    category = models.CharField(max_length=100, blank=True, verbose_name="categoria")
    subcategory = models.CharField(max_length=100, blank=True, verbose_name="subcategoria")
    manual_tags = models.ManyToManyField(
        ManualTag,
        blank=True,
        related_name="contacts",
        help_text="Tags manuais para segmentação em campanhas.",
    )
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Preenchido ao excluir (soft delete). Nulo = contato ativo.",
    )

    class Meta:
        verbose_name = "Contato"
        verbose_name_plural = "Contatos"
        ordering = ["name", "mobile_phone"]
        constraints = [
            models.UniqueConstraint(
                fields=["mobile_phone"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_contact_mobile_phone_active",
            )
        ]
        indexes = [
            models.Index(fields=["deleted_at"]),
            models.Index(fields=["mobile_phone", "deleted_at"]),
        ]

    def __str__(self):
        return f"{self.name or self.mobile_phone} ({self.mobile_phone})"
