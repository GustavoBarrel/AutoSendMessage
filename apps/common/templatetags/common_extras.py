from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Allow dict[key] access in templates: {{ my_dict|get_item:key }}"""
    if isinstance(dictionary, dict):
        return dictionary.get(key, 0)
    return 0
