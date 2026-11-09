from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Project user model. It adds no fields yet; it exists so it can be extended later."""


class Workspace(models.Model):
    """Work profile that groups chips, campaigns and dashboards; contacts are global.

    The interface calls it "Perfil". It is an organizational filter for a trusted team, not an
    access boundary: there is no user<->workspace link, so any authenticated user can pick,
    edit or delete any workspace. Do not host mutually untrusted teams on one installation.
    """

    name = models.CharField(max_length=120, verbose_name="nome")
    slug = models.SlugField(max_length=60, unique=True)
    color = models.CharField(
        max_length=7,
        default="#16a34a",
        help_text="Cor em hex (ex.: #16a34a) para o card na escolha de perfil.",
        verbose_name="cor",
    )

    class Meta:
        verbose_name = "Perfil"
        verbose_name_plural = "Perfis"
        ordering = ["name"]

    def __str__(self):
        return self.name
